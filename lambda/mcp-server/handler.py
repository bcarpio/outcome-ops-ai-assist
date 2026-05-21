"""
OutcomeOps MCP Server - Streamable HTTP Transport

Exposes OutcomeOps knowledge base tools to MCP-compatible clients
(Claude Code, VS Code, Cursor, etc.) via the MCP Streamable HTTP protocol.

Authentication: API keys (Bearer token), validated against DynamoDB.
Transport: JSON-RPC 2.0 over HTTP POST.
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import sys
import threading
import time
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable. Image is built with context=lambda/ so
# shared/ is COPYed alongside this file at /app/shared/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # type: ignore  # noqa: E402
from shared import retrieval as shared_retrieval  # type: ignore  # noqa: E402
try:
    from shared.audit_writer import (  # type: ignore
        ACTION_API_KEY_USED,
        ACTION_MCP_TOOL_INVOKED,
        log_admin_action,
    )
    _AUDIT_AVAILABLE = True
except ImportError:  # pragma: no cover - older image without shared/
    _AUDIT_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("outcomeops-mcp")

# Environment variables
PORT = int(os.environ.get("PORT", "8080"))
AWS_REGION = os.environ.get("AWS_DEFAULT_REGION", os.environ.get("AWS_REGION", "us-west-2"))
DYNAMODB_TABLE = os.environ.get("DYNAMODB_TABLE", "")
S3_VECTORS_BUCKET = os.environ.get("S3_VECTORS_BUCKET", "")
S3_VECTORS_INDEX = os.environ.get("S3_VECTORS_INDEX", "")
KB_BUCKET = os.environ.get("KB_BUCKET", "")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "prd")
MCP_BASE_URL = os.environ.get("MCP_BASE_URL", "")
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# Retrieval bounds. Defaults match chat's query-kb so the two surfaces
# behave the same; callers can override per-request via the tool schema.
# The MCP server historically returned top_k=10 chunks verbatim with no
# rerank and no cap — the 60KB-for-one-question failure. See the
# retrieval-contract ADR.
RERANK_MODEL_ID = os.environ.get("BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "true").lower() == "true"
RERANK_MAX_FETCH = int(os.environ.get("RERANK_MAX_FETCH", "50"))
DEFAULT_TOP_K = int(os.environ.get("MCP_RETRIEVAL_TOP_K", "5"))
MAX_TOP_K = 20
DEFAULT_MAX_TOKENS = int(os.environ.get("MCP_RETRIEVAL_MAX_TOKENS", "4000"))
MAX_MAX_TOKENS = 16000
DEFAULT_MIN_SCORE = float(os.environ.get("MCP_RETRIEVAL_MIN_SCORE", "0"))
# Per-result snippet ceiling so one large chunk can't dominate the budget.
SNIPPET_MAX_TOKENS = int(os.environ.get("MCP_RETRIEVAL_SNIPPET_TOKENS", "500"))

# Session storage (in-memory, single instance)
_sessions = {}  # session_id -> {"email": str, "last_active": float, "tool_routing": dict}
SESSION_TIMEOUT = 3600  # 1 hour

# MCP proxy/fanout caches
_mcp_server_cache = {}  # mcp_id -> {"tools": [...], "config": {...}, "fetched_at": float}
MCP_CACHE_TTL = 300  # 5 minutes
_mcp_sessions = {}  # mcp_id -> {"session_id": str, "created_at": float}
MCP_SESSION_TTL = 1800  # 30 minutes

# Cached AWS clients
_dynamodb = None
_s3vectors_client = None
_bedrock_client = None
_bedrock_agent_runtime_client = None
_ssm_client = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=AWS_REGION)
    return _dynamodb


def _get_s3vectors_client():
    global _s3vectors_client
    if _s3vectors_client is None:
        _s3vectors_client = boto3.client("s3vectors", region_name=AWS_REGION)
    return _s3vectors_client


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", region_name=AWS_REGION)
    return _bedrock_client


def _get_bedrock_agent_runtime_client():
    global _bedrock_agent_runtime_client
    if _bedrock_agent_runtime_client is None:
        _bedrock_agent_runtime_client = boto3.client(
            "bedrock-agent-runtime", region_name=AWS_REGION
        )
    return _bedrock_agent_runtime_client


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm", region_name=AWS_REGION)
    return _ssm_client


def _bounded_int(value, default, lo, hi):
    """Coerce an external tool-arg to a clamped int (JSON args may arrive
    as strings, floats, None, or out of range)."""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


def _bounded_float(value, default, lo, hi):
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(n, hi))


# ============================================================================
# MCP Tool Definitions
# ============================================================================

MCP_TOOLS = [
    {
        "name": "query_knowledge_base",
        "description": (
            "Search the OutcomeOps knowledge base for organizational standards, ADRs, "
            "code patterns, and documentation. Use this to find coding standards, "
            "architectural decisions, infrastructure patterns, and best practices "
            "specific to your organization."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The question or topic to search for (e.g., 'Lambda handler patterns', "
                        "'Terraform module standards', 'testing best practices')"
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional workspace ID to scope the search. "
                        "If omitted, searches all workspaces the user has access to."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of results to return after reranking "
                        "(default 5, max 20). Lower = fewer, more precise hits."
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Approximate ceiling on total response size in tokens "
                        "(default 4000, max 16000). Lower-ranked results are "
                        "dropped to stay under the cap."
                    ),
                },
                "min_score": {
                    "type": "number",
                    "description": (
                        "Minimum rerank relevance score (0.0-1.0, default 0.0) "
                        "to include a result. Raise to filter weak matches."
                    ),
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "list_workspaces",
        "description": (
            "List workspaces the authenticated user has access to. "
            "Returns workspace IDs and names for use with query_knowledge_base."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "list_workspace_documents",
        "description": (
            "List documents ingested into a workspace's knowledge base. "
            "Shows ADRs, code maps, READMEs, and other documentation available for context."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "The workspace ID to list documents for",
                },
            },
            "required": ["workspace_id"],
        },
    },
    {
        "name": "get_coding_standards",
        "description": (
            "Get coding standards for a specific topic. This is a convenience wrapper "
            "around query_knowledge_base that formulates queries optimized for retrieving "
            "standards, patterns, and best practices."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "topic": {
                    "type": "string",
                    "description": (
                        "The topic to get standards for (e.g., 'Lambda handlers', "
                        "'Terraform modules', 'API design', 'testing', 'error handling')"
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Optional workspace ID to scope the search",
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of results after reranking (default 5, max 20)."
                    ),
                },
                "max_tokens": {
                    "type": "integer",
                    "description": (
                        "Approximate total response size cap in tokens "
                        "(default 4000, max 16000)."
                    ),
                },
                "min_score": {
                    "type": "number",
                    "description": (
                        "Minimum rerank relevance score (0.0-1.0, default 0.0)."
                    ),
                },
            },
            "required": ["topic"],
        },
    },
    # ------------------------------------------------------------------
    # Symbol-graph query tools (see docs/mcp-server.md).
    # Wrap the four query shapes in shared/symbol_graph_reader.py and
    # expose them as MCP tools so external clients (Claude Code, etc.)
    # can ask structural questions across the workspace's repos.
    # ------------------------------------------------------------------
    {
        "name": "find_callers",
        "description": (
            "Find every call-site of a symbol across the workspace. Answers "
            "questions like 'Which Lambdas call report_repos_snapshot?'. Returns "
            "the source files, line numbers, and caller symbol IDs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": (
                        "Canonical symbol ID of the target (e.g., "
                        "'python:lambda.shared.outcomeops_license::report_repos_snapshot')"
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of call-sites to return (default 200).",
                },
            },
            "required": ["symbol_id", "workspace_id"],
        },
    },
    {
        "name": "find_importers",
        "description": (
            "Find every importer of a target module/package across the workspace. "
            "Pass an exact subpath (e.g., '@hyatt-hpe/hpe-utils/http2') for exact "
            "match, or a package prefix (e.g., '@hyatt-hpe/hpe-utils' or "
            "'@hyatt-hpe/hpe-utils/*') for all consumers of any subpath."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "target_or_prefix": {
                    "type": "string",
                    "description": (
                        "Exact target symbol or package prefix to find importers of."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of importer sites to return (default 500).",
                },
            },
            "required": ["target_or_prefix", "workspace_id"],
        },
    },
    {
        "name": "find_implementers",
        "description": (
            "Find every implementer of an interface, abstract class, or protocol "
            "across the workspace. Answers questions like 'What classes implement "
            "IBookingClient?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "interface_id": {
                    "type": "string",
                    "description": "Canonical symbol ID of the interface or abstract type.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of implementer sites to return (default 100).",
                },
            },
            "required": ["interface_id", "workspace_id"],
        },
    },
    {
        "name": "find_definitions",
        "description": (
            "Find every definition site of a symbol across the workspace. Returns "
            "a list rather than a single site so cross-repo duplicates (interface "
            "re-declarations, code duplication) all surface. An empty list means "
            "'not found' -- never null."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "Canonical symbol ID to look up definitions of.",
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
            },
            "required": ["symbol", "workspace_id"],
        },
    },
    {
        "name": "find_symbol_by_name",
        "description": (
            "Resolve a user-typed bare name ('require_auth') to the set of "
            "canonical symbol IDs that match it ('python:lambda.auth.handler::"
            "require_auth', 'python:lambda.org-management.handler::require_auth', "
            "...). Use this when the caller knows the local function name but "
            "not the full canonical ID -- pass the result into find_callers / "
            "find_importers / find_implementers / find_definitions for each "
            "returned canonical. Backed by the target-name-index GSI for "
            "single-partition reads bounded by actual matches."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "bare_name": {
                    "type": "string",
                    "description": (
                        "The bare function or class name as a user would type "
                        "it (e.g., 'require_auth'). No namespace, package, or "
                        "language prefix."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
                "limit": {
                    "type": "integer",
                    "description": (
                        "Maximum distinct canonicals to return (default 25). "
                        "Bare names are rarely overloaded beyond a handful of "
                        "modules; the default cap is intentional."
                    ),
                },
            },
            "required": ["bare_name", "workspace_id"],
        },
    },
]


# ============================================================================
# API Key Authentication
# ============================================================================


def validate_api_key(api_key, source_ip=""):
    """Validate an API key against DynamoDB. Returns user email or None.

    Emits ACTION_API_KEY_USED audit row on every validation attempt so
    InfoSec can see programmatic-access patterns from MCP clients.
    """
    if not api_key or not DYNAMODB_TABLE:
        return None

    try:
        dynamodb = _get_dynamodb()
        table = dynamodb.Table(DYNAMODB_TABLE)
        key_hash = hashlib.sha256(api_key.encode()).hexdigest()

        response = table.query(
            IndexName="GSI1",
            KeyConditionExpression="GSI1PK = :pk",
            ExpressionAttributeValues={
                ":pk": f"API_KEY#{key_hash}",
            },
        )

        items = response.get("Items", [])
        if not items:
            _emit_api_key_used("", "", "", False, "key_not_found", source_ip)
            return None

        item = items[0]
        key_id = item.get("key_id", "")
        key_name = item.get("name", "")

        if item.get("status") != "active":
            _emit_api_key_used(key_id, key_name, "", False, "key_revoked", source_ip)
            return None

        expires_at = item.get("expires_at")
        if expires_at:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                _emit_api_key_used(key_id, key_name, "", False, "key_expired", source_ip)
                return None

        pk = item.get("PK", "")
        email = pk.replace("USER#", "") if pk.startswith("USER#") else None
        if not email:
            _emit_api_key_used(key_id, key_name, "", False, "key_missing_owner", source_ip)
            return None

        # Update last_used_at (best-effort)
        try:
            dual_write.ddb_update_item(
                DYNAMODB_TABLE,
                {"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

        _emit_api_key_used(key_id, key_name, email, True, "", source_ip)
        return email
    except ClientError as e:
        logger.error(f"Error validating API key: {e}")
        return None


def _emit_api_key_used(key_id, key_name, email, success, reason, source_ip):
    """Mirror of workspace-mgmt _emit_api_key_used. Non-fatal."""
    if not _AUDIT_AVAILABLE:
        return
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return
    try:
        log_admin_action(
            audit_table,
            ACTION_API_KEY_USED,
            workspace_id="",
            user_email=email,
            source_ip=source_ip or "",
            user_agent="mcp-server",
            details={
                "key_id": key_id,
                "key_name": key_name,
                "success": success,
                "reason": reason,
                "via": "mcp-server",
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit API_KEY_USED: {e}")


def _emit_mcp_tool_invoked(tool_name, server_name, email, success, duration_ms, source_ip):
    """Emit ACTION_MCP_TOOL_INVOKED. Non-fatal."""
    if not _AUDIT_AVAILABLE:
        return
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return
    try:
        log_admin_action(
            audit_table,
            ACTION_MCP_TOOL_INVOKED,
            workspace_id="",
            user_email=email or "",
            source_ip=source_ip or "",
            user_agent="mcp-server",
            details={
                "tool_name": tool_name,
                "server_name": server_name,
                "success": success,
                "duration_ms": duration_ms,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit MCP_TOOL_INVOKED: {e}")


# ============================================================================
# Knowledge Base Query Functions
# ============================================================================


def generate_embedding(text):
    """Generate embedding using Bedrock Titan v2."""
    bedrock = _get_bedrock_client()
    payload = {
        "inputText": text,
        "dimensions": 1024,
        "normalize": True,
    }
    response = bedrock.invoke_model(
        modelId=EMBEDDING_MODEL_ID,
        contentType="application/json",
        accept="application/json",
        body=json.dumps(payload),
    )
    response_body = json.loads(response["body"].read())
    return response_body.get("embedding", [])


def search_knowledge_base(
    query, workspace_ids=None, top_k=DEFAULT_TOP_K, min_score=DEFAULT_MIN_SCORE
):
    """Search KB documents using the shared oversample + rerank pipeline.

    Mirrors chat's query-kb path via ``shared.retrieval``: oversample 4x,
    rerank with Cohere (graceful cosine fallback where Rerank is
    unavailable, e.g. us-east-2), cull to ``top_k``, and drop hits below
    ``min_score``. Returns normalized result dicts (``text``, ``score``,
    ``rerank_score``, ``repo``, ``type``, ``file_path`` …). Snippet
    windowing + the response cap happen in ``format_results``.
    """
    embedding = generate_embedding(query)
    if not embedding:
        return []

    top_k = max(1, min(int(top_k), MAX_TOP_K))

    filter_dict = None
    if workspace_ids:
        if len(workspace_ids) == 1:
            filter_dict = {"workspace_id": {"$eq": workspace_ids[0]}}
        else:
            filter_dict = {"workspace_id": {"$in": workspace_ids}}

    fetch_k = min(top_k * 4, RERANK_MAX_FETCH) if RERANK_ENABLED else top_k

    try:
        docs = shared_retrieval.query_vectors(
            s3vectors_client=_get_s3vectors_client(),
            vector_bucket=S3_VECTORS_BUCKET,
            vector_index=S3_VECTORS_INDEX,
            query_embedding=embedding,
            top_k=fetch_k,
            filter_dict=filter_dict,
            text_truncate=0,  # snippet windowing happens in format_results
        )
    except ClientError as e:
        logger.error(f"Error searching vectors: {e}")
        return []

    if RERANK_ENABLED and len(docs) > 1:
        docs = shared_retrieval.rerank(
            bedrock_agent_runtime_client=_get_bedrock_agent_runtime_client(),
            rerank_model_id=RERANK_MODEL_ID,
            query=query,
            documents=docs,
            top_n=top_k,
            lambda_name="mcp-server",
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        )
        if min_score > 0:
            docs = [d for d in docs if d.get("rerank_score", 0) >= min_score]
    else:
        docs = docs[:top_k]

    return docs


def format_results(results, query="", max_tokens=DEFAULT_MAX_TOKENS):
    """Format results as bounded text for the MCP response.

    Each result is windowed to a query-relevant snippet (so a 6KB chunk
    doesn't dump in full) and annotated with its match score. The total
    response is hard-capped at ``max_tokens`` by dropping whole results
    from the bottom of the (rerank-ordered) list — the top matches stay
    intact. This is the defense that keeps a broad question from
    returning 60KB.
    """
    if not results:
        return "No relevant documents found."

    max_tokens = max(1, min(int(max_tokens), MAX_MAX_TOKENS))
    parts = []
    used_tokens = 0
    for i, r in enumerate(results, 1):
        source = r.get("repo", "unknown")
        doc_type = r.get("type", "document")
        file_path = r.get("file_path", "")
        score = r.get("rerank_score", r.get("score", 0))
        snippet = shared_retrieval.make_snippet(
            r.get("text", ""), query, max_tokens=SNIPPET_MAX_TOKENS
        )
        loc = f" {file_path}" if file_path else ""
        header = f"[{i}] {source} ({doc_type}){loc} — match {score:.2f}"
        block = f"{header}\n{snippet}"
        block_tokens = shared_retrieval.estimate_tokens(block) + 4  # +separator
        if parts and used_tokens + block_tokens > max_tokens:
            omitted = len(results) - (i - 1)
            parts.append(
                f"… {omitted} lower-ranked result(s) omitted to stay within the "
                f"{max_tokens}-token response cap. Narrow the query or pass a "
                f"higher max_tokens to see more."
            )
            break
        parts.append(block)
        used_tokens += block_tokens

    return "\n\n---\n\n".join(parts)


def get_user_workspace_ids(email):
    """Get workspace IDs the user has access to, including shared workspaces."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        # Check if org admin
        org_resp = table.get_item(Key={"PK": f"USER#{email}", "SK": "ORG_MEMBER"})
        is_admin = org_resp.get("Item", {}).get("org_role") == "org_admin"

        if is_admin:
            # Enumerate all WS#... METADATA rows across the whole table.
            # Paginated via LastEvaluatedKey - a single Scan reads at most
            # 1MB and silently truncates without it; bites at enterprise
            # workspace counts. Same risk shape as the ServiceNow ADR-004
            # write-up.
            workspace_ids = []
            scan_kwargs = {
                "FilterExpression": "begins_with(SK, :sk)",
                "ExpressionAttributeValues": {":sk": "METADATA"},
                "ProjectionExpression": "PK",
            }
            while True:
                response = table.scan(**scan_kwargs)
                for item in response.get("Items", []):
                    pk = item.get("PK", "")
                    if pk.startswith("WS#"):
                        workspace_ids.append(pk.replace("WS#", ""))
                last_key = response.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            return workspace_ids

        # Regular user: direct memberships
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{email}",
                ":sk": "WS_MEMBER#",
            },
        )
        direct_ids = [
            item.get("workspace_id")
            for item in response.get("Items", [])
            if item.get("workspace_id")
        ]

        if not direct_ids:
            return []

        # Find workspaces that share with the user's workspaces. Same
        # pagination consideration as the admin branch - the share-resolution
        # has to see EVERY workspace METADATA row to know whether it grants
        # the user access via search_allowed_workspaces.
        all_ids = set(direct_ids)
        scan_kwargs = {
            "FilterExpression": "begins_with(SK, :sk)",
            "ExpressionAttributeValues": {":sk": "METADATA"},
            "ProjectionExpression": "PK, search_allowed_workspaces",
        }
        while True:
            ws_scan = table.scan(**scan_kwargs)
            for item in ws_scan.get("Items", []):
                pk = item.get("PK", "")
                if not pk.startswith("WS#"):
                    continue
                ws_id = pk.replace("WS#", "")
                if ws_id in all_ids:
                    continue
                allowed = item.get("search_allowed_workspaces", [])
                if allowed:
                    for direct_id in direct_ids:
                        if direct_id in allowed:
                            all_ids.add(ws_id)
                            break
            last_key = ws_scan.get("LastEvaluatedKey")
            if not last_key:
                break
            scan_kwargs["ExclusiveStartKey"] = last_key

        return list(all_ids)
    except ClientError as e:
        logger.error(f"Error getting user workspaces: {e}")
        return []


# ============================================================================
# MCP Proxy/Fanout - Server Discovery and Tool Proxying
# ============================================================================


def discover_mcp_servers(user_workspace_ids):
    """Discover MCP servers available to the user's workspaces.

    Returns list of server configs with their tools:
    [{"mcp_id": str, "config": {...}, "tools": [...], "fetched_at": float}]
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    # 1. Find MCP servers assigned to user's workspaces
    mcp_ids = set()
    for ws_id in user_workspace_ids:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{ws_id}",
                ":sk": "MCP_SERVER#",
            },
        )
        for item in response.get("Items", []):
            mcp_id = item.get("mcp_id")
            if mcp_id:
                mcp_ids.add(mcp_id)

    # 2. Also find global MCP servers
    response = table.query(
        IndexName="GSI1",
        KeyConditionExpression="GSI1PK = :pk AND begins_with(GSI1SK, :sk)",
        ExpressionAttributeValues={
            ":pk": "ORG#default",
            ":sk": "MCP#",
        },
    )
    for item in response.get("Items", []):
        if item.get("is_global") is True:
            mcp_id = item.get("mcp_id")
            if mcp_id:
                mcp_ids.add(mcp_id)

    # 3. Load config and tools for each server
    servers = []
    for mcp_id in mcp_ids:
        server = _load_mcp_server(table, mcp_id)
        if server:
            servers.append(server)

    return servers


def _load_mcp_server(table, mcp_id):
    """Load MCP server config and tools, with in-memory caching."""
    now = time.time()

    # Check cache
    cached = _mcp_server_cache.get(mcp_id)
    if cached and (now - cached["fetched_at"]) < MCP_CACHE_TTL:
        return cached

    # Load metadata from DynamoDB
    meta_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
    meta = meta_resp.get("Item")
    if not meta or meta.get("status") == "error":
        return None

    config = {
        "mcp_id": mcp_id,
        "name": meta.get("name", mcp_id),
        # Prefer the Cloud Map internal URL when present. The gateway is a
        # Fargate task in the same VPC as catalog MCP servers, so private-DNS
        # HTTP traffic bypasses the ALB (no TLS hop, no cert trust). The `url`
        # field remains the ALB URL for external (Lambda) callers like chat
        # and workspace-management, which can't resolve internal DNS.
        "url": meta.get("internal_url") or meta.get("url", ""),
        "auth_type": meta.get("auth_type", "none"),
        "ssm_token_path": meta.get("ssm_token_path", ""),
        "ssm_token_header": meta.get("ssm_token_header", ""),
        "encrypted_credentials": meta.get("encrypted_credentials", ""),
    }

    # Load cached tool manifest from DynamoDB
    tools_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "TOOLS"})
    tools_item = tools_resp.get("Item")
    tools = tools_item.get("tools", []) if tools_item else []

    # If no cached tools, try fetching live
    if not tools and config["url"]:
        try:
            tools = _fetch_tools_live(config)
        except Exception as e:
            logger.warning(f"Failed to fetch tools live from {mcp_id}: {e}")

    result = {"tools": tools, "config": config, "fetched_at": now}
    _mcp_server_cache[mcp_id] = result
    return result


def _fetch_tools_live(config):
    """Fetch tools from an MCP server via JSON-RPC 2.0. Fallback if TOOLS cache empty."""
    url = config["url"]
    headers = {"Content-Type": "application/json", "Accept": "application/json"}

    # Add auth
    _add_mcp_auth_headers(headers, config)

    # Initialize session
    init_payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "outcomeops-gateway", "version": "1.0.0"},
        },
    }).encode()

    req = urllib.request.Request(url, data=init_payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=10)
    session_id = resp.headers.get("Mcp-Session-Id", "")
    json.loads(resp.read())  # consume response

    # Fetch tools
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    list_payload = json.dumps({
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    }).encode()

    req = urllib.request.Request(url, data=list_payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=10)
    body = json.loads(resp.read())

    return body.get("result", {}).get("tools", [])


def _add_mcp_auth_headers(headers, config):
    """Add authentication headers for calling an internal MCP server."""
    auth_type = config.get("auth_type", "none")

    # Catalog servers: token from SSM
    ssm_path = config.get("ssm_token_path")
    ssm_header = config.get("ssm_token_header")
    if ssm_path and ssm_header:
        ssm = _get_ssm_client()
        param = ssm.get_parameter(Name=ssm_path, WithDecryption=True)
        headers[ssm_header] = param["Parameter"]["Value"]

    # User-added servers: KMS-encrypted credentials
    encrypted = config.get("encrypted_credentials")
    if encrypted and auth_type == "bearer":
        kms = boto3.client("kms", region_name=AWS_REGION)
        resp = kms.decrypt(CiphertextBlob=base64.b64decode(encrypted))
        token = resp["Plaintext"].decode()
        headers["Authorization"] = f"Bearer {token}"
    elif encrypted and auth_type == "api_key":
        kms = boto3.client("kms", region_name=AWS_REGION)
        resp = kms.decrypt(CiphertextBlob=base64.b64decode(encrypted))
        key = resp["Plaintext"].decode()
        headers["X-API-Key"] = key


def _proxy_tool_call(config, original_tool_name, arguments):
    """Proxy a tools/call request to an internal MCP server.

    Returns the MCP result dict (content array) directly.
    """
    url = config["url"]
    mcp_id = config["mcp_id"]
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}

    # Add auth headers
    _add_mcp_auth_headers(headers, config)

    # Get or create MCP session with the target server
    now = time.time()
    cached_session = _mcp_sessions.get(mcp_id)
    if cached_session and (now - cached_session["created_at"]) < MCP_SESSION_TTL:
        headers["Mcp-Session-Id"] = cached_session["session_id"]
    else:
        # Initialize new session
        session_id = _mcp_initialize_session(url, headers)
        if session_id:
            _mcp_sessions[mcp_id] = {"session_id": session_id, "created_at": now}
            headers["Mcp-Session-Id"] = session_id

    # Send tools/call
    payload = json.dumps({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": original_tool_name, "arguments": arguments},
    }).encode()

    req = urllib.request.Request(url, data=payload, headers=headers)
    resp = urllib.request.urlopen(req, timeout=30)
    raw = resp.read().decode()
    content_type = resp.headers.get("Content-Type") or ""

    # Handle SSE responses (Streamable HTTP may return text/event-stream)
    if "text/event-stream" in content_type:
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break

    body = json.loads(raw)

    if "error" in body:
        return {
            "content": [{"type": "text", "text": f"Error from {config['name']}: "
                        f"{body['error'].get('message', 'Unknown error')}"}],
            "isError": True,
        }

    return body.get("result", {"content": [{"type": "text", "text": "No result"}]})


def _mcp_initialize_session(url, headers):
    """Initialize an MCP session with an internal server. Returns session_id."""
    init_payload = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "outcomeops-gateway", "version": "1.0.0"},
        },
    }).encode()

    init_headers = headers.copy()
    init_headers.setdefault("Accept", "application/json, text/event-stream")
    req = urllib.request.Request(url, data=init_payload, headers=init_headers)
    resp = urllib.request.urlopen(req, timeout=10)
    raw = resp.read().decode()
    content_type = resp.headers.get("Content-Type") or ""
    if "text/event-stream" in content_type:
        for line in raw.splitlines():
            if line.startswith("data: "):
                raw = line[6:]
                break
    json.loads(raw)  # consume/validate response
    session_id = resp.headers.get("Mcp-Session-Id", "")

    # Send initialized notification
    if session_id:
        notif_headers = headers.copy()
        notif_headers["Mcp-Session-Id"] = session_id
        notif_payload = json.dumps({
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }).encode()
        req = urllib.request.Request(url, data=notif_payload, headers=notif_headers)
        urllib.request.urlopen(req, timeout=5)

    return session_id


# ============================================================================
# MCP Tool Handlers
# ============================================================================


def handle_query_knowledge_base(params, email):
    """Handle query_knowledge_base tool call."""
    query = params.get("query", "")
    if not query:
        return {"error": "query is required"}

    workspace_id = params.get("workspace_id")
    if workspace_id:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = get_user_workspace_ids(email)

    top_k = _bounded_int(params.get("top_k"), DEFAULT_TOP_K, 1, MAX_TOP_K)
    max_tokens = _bounded_int(
        params.get("max_tokens"), DEFAULT_MAX_TOKENS, 1, MAX_MAX_TOKENS
    )
    min_score = _bounded_float(params.get("min_score"), DEFAULT_MIN_SCORE, 0.0, 1.0)

    results = search_knowledge_base(
        query, workspace_ids=workspace_ids, top_k=top_k, min_score=min_score
    )
    return {"text": format_results(results, query=query, max_tokens=max_tokens)}


def handle_list_workspaces(params, email):
    """Handle list_workspaces tool call."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        workspace_ids = get_user_workspace_ids(email)
        workspaces = []
        for ws_id in workspace_ids:
            resp = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
            if "Item" in resp:
                item = resp["Item"]
                workspaces.append({
                    "workspace_id": ws_id,
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                })

        text = "Your workspaces:\n\n"
        if not workspaces:
            text = "You have no workspaces."
        else:
            for ws in workspaces:
                text += f"- **{ws['name']}** (ID: {ws['workspace_id']})"
                if ws["description"]:
                    text += f" - {ws['description']}"
                text += "\n"

        return {"text": text}
    except ClientError as e:
        logger.error(f"Error listing workspaces: {e}")
        return {"error": "Failed to list workspaces"}


def handle_list_workspace_documents(params, email):
    """Handle list_workspace_documents tool call.

    The vector store is the source of truth — it holds raw docs (markdown,
    yaml, json) plus generated code-maps and per-area summaries. We query
    S3 Vectors with a metadata filter on workspace_id (same pattern as
    chat's search_knowledge_base) and dedupe chunks down to distinct
    documents by (repo, file_path).

    Server-side filtering happens inside S3 Vectors via the ``filter``
    parameter on query_vectors; the query embedding only orders results,
    so a sentinel string ("workspace document listing") is sufficient.
    """
    workspace_id = params.get("workspace_id", "")
    if not workspace_id:
        return {"error": "workspace_id is required"}

    # Verify access — same path chat uses, includes direct membership,
    # shared workspaces, and org-admin enumeration.
    user_ws_ids = get_user_workspace_ids(email)
    if workspace_id not in user_ws_ids:
        return {"error": "You do not have access to this workspace"}

    s3vectors = _get_s3vectors_client()
    embedding = generate_embedding("workspace document listing")
    if not embedding:
        return {"error": "Failed to generate embedding for listing"}

    try:
        response = s3vectors.query_vectors(
            vectorBucketName=S3_VECTORS_BUCKET,
            indexName=S3_VECTORS_INDEX,
            queryVector={"float32": embedding},
            topK=500,
            returnMetadata=True,
            filter={"workspace_id": {"$eq": workspace_id}},
        )
    except ClientError as e:
        logger.error(f"Error listing documents via vectors: {e}")
        return {"error": "Failed to list workspace documents"}

    # Dedupe chunks down to distinct documents. The same file is
    # typically split into many chunks; surface each file once with
    # its content type.
    seen = {}
    for vector in response.get("vectors", []):
        metadata = vector.get("metadata", {}) or {}
        repo = metadata.get("repo", "")
        file_path = metadata.get("file_path", "")
        doc_type = metadata.get("type", "unknown")
        if not file_path and not repo:
            continue
        key = (repo, file_path)
        if key not in seen:
            seen[key] = {
                "repo": repo,
                "file_path": file_path,
                "type": doc_type,
                "name": file_path.split("/")[-1] if file_path else repo,
            }

    documents = sorted(seen.values(), key=lambda d: (d["repo"], d["file_path"]))

    if not documents:
        text = "No documents found in this workspace."
    else:
        text = f"Documents in workspace ({len(documents)}):\n\n"
        for doc in documents:
            label = doc["file_path"] or doc["name"]
            text += f"- {label} (type: {doc['type']}, repo: {doc['repo']})\n"

    return {"text": text}


def handle_get_coding_standards(params, email):
    """Handle get_coding_standards tool call."""
    topic = params.get("topic", "")
    if not topic:
        return {"error": "topic is required"}

    query = f"What are our standards and best practices for {topic}?"
    workspace_id = params.get("workspace_id")

    if workspace_id:
        workspace_ids = [workspace_id]
    else:
        workspace_ids = get_user_workspace_ids(email)

    top_k = _bounded_int(params.get("top_k"), DEFAULT_TOP_K, 1, MAX_TOP_K)
    max_tokens = _bounded_int(
        params.get("max_tokens"), DEFAULT_MAX_TOKENS, 1, MAX_MAX_TOKENS
    )
    min_score = _bounded_float(params.get("min_score"), DEFAULT_MIN_SCORE, 0.0, 1.0)

    results = search_knowledge_base(
        query, workspace_ids=workspace_ids, top_k=top_k, min_score=min_score
    )
    return {"text": format_results(results, query=query, max_tokens=max_tokens)}


# ============================================================================
# Symbol-graph query handlers (see docs/mcp-server.md).
#
# Each wraps one query in shared/symbol_graph_reader.py. The reader is
# imported LAZILY inside the handler so the gateway's cold-start path is
# not coupled to its import success -- a hiccup in the reader (e.g.,
# boto3 version skew, transient sys.modules contamination) must NOT
# crash the MCP server.
#
# Workspace authorization mirrors handle_list_workspace_documents: the
# API-key-derived email's authorized workspace set must include the
# requested workspace_id. SymbolGraphUnavailable from the reader is
# caught and surfaced as a JSON-RPC error payload, not a raised
# exception (which would 500 the gateway).
# ============================================================================


def _query_result_to_dict(query_result) -> dict:
    """Project a ``QueryResult`` onto the JSON shape documented for the
    MCP tools: ``sites`` + ``truncated`` + ``graph_sha`` +
    ``staleness_seconds``.
    """
    sites = []
    for s in query_result.sites:
        sites.append({
            "file_path": s.file_path,
            "line": s.line,
            "repo": s.repo,
            "source_symbol_id": s.source_symbol_id,
            "language": s.language,
        })
    return {
        "sites": sites,
        "truncated": bool(query_result.truncated),
        "graph_sha": query_result.graph_sha,
        "staleness_seconds": query_result.staleness_seconds,
    }


def _call_sites_to_dict(call_sites) -> dict:
    """Project a list[CallSite] (definitions()) onto the same JSON shape
    as ``_query_result_to_dict``. ``definitions`` returns a bare list;
    ``truncated`` / ``graph_sha`` / ``staleness_seconds`` are reported
    as ``False`` / ``None`` / ``None`` so the MCP tool's response is
    uniformly shaped across all four queries.
    """
    sites = []
    for s in call_sites:
        sites.append({
            "file_path": s.file_path,
            "line": s.line,
            "repo": s.repo,
            "source_symbol_id": s.source_symbol_id,
            "language": s.language,
        })
    return {
        "sites": sites,
        "truncated": False,
        "graph_sha": None,
        "staleness_seconds": None,
    }


def _check_symbol_graph_workspace_access(workspace_id, email):
    """Return None if access is allowed, else an error-payload dict.

    Mirrors the access check in ``handle_list_workspace_documents``: the
    user must be a member (direct or via cross-workspace share) of the
    requested workspace. The symbol-graph reader itself does NOT enforce
    auth -- the gateway does.
    """
    if not workspace_id:
        return {"error": "workspace_id is required"}
    user_ws_ids = get_user_workspace_ids(email)
    if workspace_id not in user_ws_ids:
        return {"error": "You do not have access to this workspace"}
    return None


def handle_find_callers(params, email):
    """Handle find_callers tool call."""
    symbol_id = params.get("symbol_id", "")
    workspace_id = params.get("workspace_id", "")
    limit = int(params.get("limit", 200) or 200)

    if not symbol_id:
        return {"error": "symbol_id is required"}
    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    # Lazy import: keeps mcp-server cold-start independent of the reader.
    try:
        from shared import symbol_graph_reader  # type: ignore
    except Exception as exc:
        logger.error(f"Failed to import symbol_graph_reader: {exc}", exc_info=True)
        return {"error": "Symbol graph unavailable"}

    try:
        result = symbol_graph_reader.callers(
            workspace_id,
            symbol_id,
            limit=limit,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(f"Symbol graph unavailable for find_callers: {exc}")
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    return {"text": json.dumps(_query_result_to_dict(result))}


def handle_find_importers(params, email):
    """Handle find_importers tool call."""
    target = params.get("target_or_prefix", "")
    workspace_id = params.get("workspace_id", "")
    limit = int(params.get("limit", 500) or 500)

    if not target:
        return {"error": "target_or_prefix is required"}
    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    try:
        from shared import symbol_graph_reader  # type: ignore
    except Exception as exc:
        logger.error(f"Failed to import symbol_graph_reader: {exc}", exc_info=True)
        return {"error": "Symbol graph unavailable"}

    try:
        result = symbol_graph_reader.importers(
            workspace_id,
            target,
            limit=limit,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(f"Symbol graph unavailable for find_importers: {exc}")
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    return {"text": json.dumps(_query_result_to_dict(result))}


def handle_find_implementers(params, email):
    """Handle find_implementers tool call."""
    interface_id = params.get("interface_id", "")
    workspace_id = params.get("workspace_id", "")
    limit = int(params.get("limit", 100) or 100)

    if not interface_id:
        return {"error": "interface_id is required"}
    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    try:
        from shared import symbol_graph_reader  # type: ignore
    except Exception as exc:
        logger.error(f"Failed to import symbol_graph_reader: {exc}", exc_info=True)
        return {"error": "Symbol graph unavailable"}

    try:
        result = symbol_graph_reader.implementers(
            workspace_id,
            interface_id,
            limit=limit,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(f"Symbol graph unavailable for find_implementers: {exc}")
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    return {"text": json.dumps(_query_result_to_dict(result))}


def handle_find_definitions(params, email):
    """Handle find_definitions tool call.

    ``definitions`` returns ``list[CallSite]`` (not a QueryResult) --
    cross-repo duplicates surface as separate entries. We project that
    onto the same JSON envelope as the other three queries so MCP
    clients see a uniform shape.
    """
    symbol = params.get("symbol", "")
    workspace_id = params.get("workspace_id", "")

    if not symbol:
        return {"error": "symbol is required"}
    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    try:
        from shared import symbol_graph_reader  # type: ignore
    except Exception as exc:
        logger.error(f"Failed to import symbol_graph_reader: {exc}", exc_info=True)
        return {"error": "Symbol graph unavailable"}

    try:
        sites = symbol_graph_reader.definitions(
            workspace_id,
            symbol,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(f"Symbol graph unavailable for find_definitions: {exc}")
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    return {"text": json.dumps(_call_sites_to_dict(sites))}


def handle_find_symbol_by_name(params, email):
    """Resolve a user-typed bare name to the set of canonical
    symbol IDs that match it.

    The four directional tools (find_callers / find_importers /
    find_implementers / find_definitions) all take a CANONICAL ID
    (`python:lambda.auth.handler::require_auth`). Callers who only
    have the bare name ("require_auth") use this tool first to map
    to the canonical set, then issue a directional query per
    canonical. The chat handler does the equivalent internally;
    this exposes the same resolver to external MCP clients.
    """
    bare_name = params.get("bare_name", "")
    workspace_id = params.get("workspace_id", "")
    limit = params.get("limit", 25)

    if not bare_name:
        return {"error": "bare_name is required"}
    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    try:
        from shared import symbol_graph_reader  # type: ignore
    except Exception as exc:
        logger.error(
            f"Failed to import symbol_graph_reader: {exc}", exc_info=True
        )
        return {"error": "Symbol graph unavailable"}

    try:
        canonicals = symbol_graph_reader.fuzzy_resolve_by_name(
            workspace_id,
            bare_name,
            limit=int(limit) if limit else 25,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE")
            or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(
            f"Symbol graph unavailable for find_symbol_by_name: {exc}"
        )
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    return {"text": json.dumps({"canonicals": canonicals})}


TOOL_HANDLERS = {
    "query_knowledge_base": handle_query_knowledge_base,
    "list_workspaces": handle_list_workspaces,
    "list_workspace_documents": handle_list_workspace_documents,
    "get_coding_standards": handle_get_coding_standards,
    # Symbol-graph query tools (see docs/mcp-server.md)
    "find_callers": handle_find_callers,
    "find_importers": handle_find_importers,
    "find_implementers": handle_find_implementers,
    "find_definitions": handle_find_definitions,
    "find_symbol_by_name": handle_find_symbol_by_name,
}


# ============================================================================
# JSON-RPC 2.0 Helpers
# ============================================================================


def jsonrpc_response(id, result):
    """Build a JSON-RPC 2.0 success response."""
    return {"jsonrpc": "2.0", "id": id, "result": result}


def jsonrpc_error(id, code, message, data=None):
    """Build a JSON-RPC 2.0 error response."""
    error = {"code": code, "message": message}
    if data:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": id, "error": error}


# ============================================================================
# HTTP Handler
# ============================================================================


class McpHandler(BaseHTTPRequestHandler):
    """HTTP request handler for MCP Streamable HTTP transport."""

    def log_message(self, format, *args):
        """Override to use Python logging instead of stderr."""
        logger.info(format % args)

    def _send_json(self, status_code, body, session_id=None):
        """Send a JSON response."""
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json")
        if session_id:
            self.send_header("Mcp-Session-Id", session_id)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode("utf-8"))

    def do_GET(self):
        """Health check endpoint."""
        if self.path in ("/", "/health", "/mcp/v1"):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"status": "healthy", "service": "outcomeops-mcp"}).encode())
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        """Handle session termination (DELETE /mcp/v1)."""
        session_id = self.headers.get("Mcp-Session-Id")
        if session_id and session_id in _sessions:
            del _sessions[session_id]
            logger.info(f"Session terminated: {session_id[:8]}...")
        self.send_response(200)
        self.end_headers()

    def do_POST(self):
        """Handle MCP JSON-RPC requests."""
        if self.path not in ("/mcp/v1", "/mcp/v1/"):
            self.send_response(404)
            self.end_headers()
            return

        # Read body
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length == 0:
            self._send_json(400, jsonrpc_error(None, -32600, "Empty request body"))
            return

        try:
            body = json.loads(self.rfile.read(content_length))
        except (json.JSONDecodeError, ValueError):
            self._send_json(400, jsonrpc_error(None, -32600, "Invalid JSON"))
            return

        request_id = body.get("id")
        method = body.get("method", "")
        params = body.get("params", {})

        # Initialize doesn't require auth (but we authenticate anyway for session setup)
        if method == "initialize":
            self._handle_initialize(request_id, params)
            return

        # All other methods require authentication
        # Check session first
        session_id = self.headers.get("Mcp-Session-Id")
        email = None

        if session_id and session_id in _sessions:
            session = _sessions[session_id]
            if time.time() - session["last_active"] < SESSION_TIMEOUT:
                email = session["email"]
                session["last_active"] = time.time()

        if not email:
            # Try API key auth
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
                email = validate_api_key(
                    api_key,
                    source_ip=getattr(self, "client_address", ("",))[0] if getattr(self, "client_address", None) else "",
                )

        if not email:
            self._send_json(
                401,
                jsonrpc_error(request_id, -32000, "Authentication required"),
            )
            return

        # Handle notifications (no response needed)
        if method == "notifications/initialized":
            self.send_response(204)
            self.end_headers()
            return

        # Dispatch method
        if method == "tools/list":
            self._handle_tools_list(request_id, session_id)
        elif method == "tools/call":
            self._handle_tools_call(request_id, params, email, session_id)
        else:
            self._send_json(
                200,
                jsonrpc_error(request_id, -32601, f"Method not found: {method}"),
                session_id=session_id,
            )

    def _handle_initialize(self, request_id, params):
        """Handle MCP initialize request."""
        # Authenticate via API key
        auth_header = self.headers.get("Authorization", "")
        email = None
        if auth_header.startswith("Bearer "):
            api_key = auth_header[7:]
            email = validate_api_key(
                api_key,
                source_ip=getattr(self, "client_address", ("",))[0] if getattr(self, "client_address", None) else "",
            )

        if not email:
            self._send_json(
                401,
                jsonrpc_error(request_id, -32000, "Authentication required"),
            )
            return

        # Create session
        session_id = secrets.token_hex(16)
        _sessions[session_id] = {
            "email": email,
            "last_active": time.time(),
        }

        logger.info(f"New session for {email}: {session_id[:8]}...")

        result = {
            "protocolVersion": "2025-03-26",
            "capabilities": {
                "tools": {"listChanged": True},
            },
            "serverInfo": {
                "name": "OutcomeOps",
                "version": "1.0.0",
            },
        }

        self._send_json(200, jsonrpc_response(request_id, result), session_id=session_id)

    def _handle_tools_list(self, request_id, session_id):
        """Handle tools/list request. Returns native + proxied tools."""
        email = _sessions.get(session_id, {}).get("email")
        workspace_ids = get_user_workspace_ids(email) if email else []

        # Start with native OutcomeOps tools
        all_tools = list(MCP_TOOLS)

        # Build proxy tool mapping for this session
        tool_routing = {}  # namespaced_tool_name -> {"config": ..., "original_name": ...}

        # Discover and add proxied tools
        try:
            servers = discover_mcp_servers(workspace_ids)
            for server in servers:
                server_name = server["config"]["name"].lower().replace(" ", "_").replace("-", "_")
                for tool in server.get("tools", []):
                    namespaced_name = f"{server_name}__{tool['name']}"

                    proxied_tool = {
                        "name": namespaced_name,
                        "description": f"[{server['config']['name']}] {tool.get('description', '')}",
                        "inputSchema": tool.get("inputSchema", {"type": "object", "properties": {}}),
                    }
                    all_tools.append(proxied_tool)

                    tool_routing[namespaced_name] = {
                        "config": server["config"],
                        "original_name": tool["name"],
                    }
        except Exception as e:
            logger.error(f"Error discovering MCP servers: {e}", exc_info=True)

        # Store routing in session for use by tools/call
        if session_id and session_id in _sessions:
            _sessions[session_id]["tool_routing"] = tool_routing

        result = {"tools": all_tools}
        self._send_json(200, jsonrpc_response(request_id, result), session_id=session_id)

    def _handle_tools_call(self, request_id, params, email, session_id):
        """Handle tools/call request. Routes to native handler or proxied server."""
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})
        source_ip = (
            getattr(self, "client_address", ("",))[0]
            if getattr(self, "client_address", None) else ""
        )
        invoke_start = time.time()

        # Check native tools first
        handler = TOOL_HANDLERS.get(tool_name)
        if handler:
            logger.info(f"Tool call: {tool_name} (user: {email})")
            try:
                result = handler(tool_args, email)

                if "error" in result:
                    content = [{"type": "text", "text": f"Error: {result['error']}"}]
                    is_error = True
                else:
                    content = [{"type": "text", "text": result["text"]}]
                    is_error = False

                response_result = {"content": content, "isError": is_error}
                self._send_json(
                    200,
                    jsonrpc_response(request_id, response_result),
                    session_id=session_id,
                )
                _emit_mcp_tool_invoked(
                    tool_name, "outcomeops-native", email,
                    success=not is_error,
                    duration_ms=int((time.time() - invoke_start) * 1000),
                    source_ip=source_ip,
                )
            except Exception as e:
                logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
                self._send_json(
                    200,
                    jsonrpc_error(request_id, -32603, f"Internal error: {type(e).__name__}"),
                    session_id=session_id,
                )
                _emit_mcp_tool_invoked(
                    tool_name, "outcomeops-native", email,
                    success=False,
                    duration_ms=int((time.time() - invoke_start) * 1000),
                    source_ip=source_ip,
                )
            return

        # Check proxied tools
        session = _sessions.get(session_id, {})
        tool_routing = session.get("tool_routing", {})

        # Build tool_routing on-demand if not yet populated
        # (handles cases where tools/call arrives before tools/list)
        if not tool_routing and email:
            try:
                workspace_ids = get_user_workspace_ids(email) if email else []
                servers = discover_mcp_servers(workspace_ids)
                for server in servers:
                    server_name = server["config"]["name"].lower().replace(" ", "_").replace("-", "_")
                    for tool in server.get("tools", []):
                        namespaced_name = f"{server_name}__{tool['name']}"
                        tool_routing[namespaced_name] = {
                            "config": server["config"],
                            "original_name": tool["name"],
                        }
                if session_id and session_id in _sessions:
                    _sessions[session_id]["tool_routing"] = tool_routing
                logger.info(f"Built tool_routing on-demand: {len(tool_routing)} proxied tools")
            except Exception as e:
                logger.error(f"Error building tool_routing on-demand: {e}", exc_info=True)

        route = tool_routing.get(tool_name)

        if not route:
            self._send_json(
                200,
                jsonrpc_error(request_id, -32602, f"Unknown tool: {tool_name}"),
                session_id=session_id,
            )
            return

        # Proxy the call to the internal MCP server
        logger.info(
            f"Proxying tool call: {tool_name} -> {route['config']['name']}/"
            f"{route['original_name']} (user: {email})"
        )

        try:
            result = _proxy_tool_call(route["config"], route["original_name"], tool_args)
            self._send_json(
                200,
                jsonrpc_response(request_id, result),
                session_id=session_id,
            )
            _emit_mcp_tool_invoked(
                tool_name, route["config"].get("name", ""), email,
                success=True,
                duration_ms=int((time.time() - invoke_start) * 1000),
                source_ip=source_ip,
            )
        except Exception as e:
            logger.error(f"Error proxying tool {tool_name}: {e}", exc_info=True)
            self._send_json(
                200,
                jsonrpc_error(request_id, -32603, f"Proxy error: {type(e).__name__}"),
                session_id=session_id,
            )
            _emit_mcp_tool_invoked(
                tool_name, route["config"].get("name", ""), email,
                success=False,
                duration_ms=int((time.time() - invoke_start) * 1000),
                source_ip=source_ip,
            )


# ============================================================================
# Session Cleanup
# ============================================================================


def cleanup_sessions():
    """Periodically clean up expired sessions."""
    while True:
        time.sleep(300)  # Every 5 minutes
        now = time.time()
        expired = [
            sid for sid, s in _sessions.items()
            if now - s["last_active"] > SESSION_TIMEOUT
        ]
        for sid in expired:
            del _sessions[sid]
        if expired:
            logger.info(f"Cleaned up {len(expired)} expired sessions")


# ============================================================================
# Main
# ============================================================================


class ThreadingHTTPServer(HTTPServer):
    """HTTPServer with threading support for concurrent requests."""
    allow_reuse_address = True

    def process_request(self, request, client_address):
        """Handle each request in a new thread."""
        thread = threading.Thread(target=self.process_request_thread, args=(request, client_address))
        thread.daemon = True
        thread.start()

    def process_request_thread(self, request, client_address):
        """Process a request in a thread."""
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


if __name__ == "__main__":
    # Start session cleanup thread
    cleanup_thread = threading.Thread(target=cleanup_sessions, daemon=True)
    cleanup_thread.start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), McpHandler)
    logger.info(f"OutcomeOps MCP server starting on port {PORT}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.shutdown()
