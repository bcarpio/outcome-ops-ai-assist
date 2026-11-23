# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler
from typing import List

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable. Image is built with context=lambda/ so
# shared/ is COPYed alongside this file at /app/shared/.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # type: ignore  # noqa: E402
from shared import retrieval as shared_retrieval  # type: ignore  # noqa: E402
from shared.model_client import resolve_profile_arn  # type: ignore  # noqa: E402
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
# Azure AD OAuth (JWT) auth for the MCP server. AZURE_AD_TENANT_ID +
# MCP_JWT_AUDIENCE are required for the JWT path; when either is unset the JWT
# path is disabled and only static keys work. MCP_STATIC_KEYS_ENABLED gates the
# legacy static-key path (default true for back-compat; false in the F500
# profile once every client has moved to OAuth).
AZURE_AD_TENANT_ID = os.environ.get("AZURE_AD_TENANT_ID", "")
MCP_JWT_AUDIENCE = os.environ.get("MCP_JWT_AUDIENCE", "")
MCP_STATIC_KEYS_ENABLED = (
    os.environ.get("MCP_STATIC_KEYS_ENABLED", "true").lower() == "true"
)
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

# Session storage.
#
# session state lives in an external SessionStore
# (DDB in prod, in-memory for tests) instead of a per-container dict.
# Externalizing was the design constraint that had prevented horizontal
# scaling of the MCP Fargate service — a second task couldn't share
# authenticated sessions.
#
# The `_sessions` and `_mcp_sessions` module-level dicts are retained as
# BACKING STORAGE for the in-memory store. In memory mode, the store
# reads/writes through them, so legacy unit tests that seed these dicts
# directly (mcp_module._sessions[sid] = {...}) still work end-to-end.
# In DDB mode, these dicts are unused — the store hits DynamoDB.
#
# _mcp_server_cache remains an in-memory per-task cache regardless: it
# holds proxied-server descriptors (tools list + config), which are cheap
# to regenerate on cache miss.
_sessions = {}  # session_id -> {"email": str, "last_active": float, "tool_routing": dict}
_mcp_sessions = {}  # mcp_id -> {"session_id": str, "created_at": float}
_session_store = None  # lazy-initialised via _get_session_store()
SESSION_TIMEOUT = 3600  # 1 hour — application-side authoritative expiry

# MCP proxy/fanout cache (per-task, safe to lose on restart).
_mcp_server_cache = {}  # mcp_id -> {"tools": [...], "config": {...}, "fetched_at": float}
MCP_CACHE_TTL = 300  # 5 minutes
MCP_SESSION_TTL = 1800  # 30 minutes — TTL on upstream MCP proxy sessions

# Store keys for upstream MCP proxy sessions are prefixed so they don't
# collide with real user session_ids (32-hex-char secrets). The store
# strips the prefix and routes upstream entries to `_mcp_sessions`;
# user entries go to `_sessions`.
_UPSTREAM_PREFIX = "upstream#"


def _github_api() -> str:
    """GHES-aware REST base (shared.github_base). SaaS default is
    byte-identical to the old hardcoded api.github.com. Deferred import
    keeps module load order safe."""
    from shared.github_base import github_api_base
    return github_api_base()


def _get_session_store():
    """Lazy import + init so unit tests can override MCP_SESSION_STORE=memory
    without triggering a boto3 client build at import time. Passes the
    module-level dicts as backing storage for the in-memory store."""
    global _session_store
    if _session_store is None:
        from session_store import build_session_store  # local import: same package
        _session_store = build_session_store(
            user_backing=_sessions,
            upstream_backing=_mcp_sessions,
        )
    return _session_store


def _reset_session_store_for_test():
    """Test helper: drop the current store so the next _get_session_store()
    call rebuilds it (picking up any env or backing-dict changes)."""
    global _session_store
    _session_store = None

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
    """ADR-035: this client is embeddings-only on Titan (invoke_model
    at line ~708). The mcp-server doesn't make LLM-completion calls
    itself; downstream consumers (chat synthesis, codegen, etc.) route
    through shared.model_client per ADR-035."""
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
        "name": "search_hybrid",
        "description": (
            "Hybrid retrieval in ONE call: returns BOTH narrative/code-map "
            "snippets from the knowledge base AND structural facts from the "
            "symbol graph (callers / importers / implementers / definitions) for "
            "the symbol the question is about. Prefer this over calling "
            "query_knowledge_base and the find_* tools separately. The "
            "symbol_graph facts are structural and citable at file_path:line -- "
            "cite them there; do not attribute a structural claim to a prose/doc "
            "chunk. The symbol-graph facts are returned regardless of the RAG "
            "relevance score, so a pinpoint structural question still gets them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "The question to answer. Structural phrasing (e.g. 'who "
                        "calls validate_input', 'what implements IBookingClient') "
                        "anchors the symbol-graph facts."
                    ),
                },
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Optional workspace ID to scope the search. If omitted, "
                        "searches all workspaces the user has access to."
                    ),
                },
                "top_k": {
                    "type": "integer",
                    "description": (
                        "Number of RAG results to return after reranking "
                        "(default 5, max 20)."
                    ),
                },
                "min_score": {
                    "type": "number",
                    "description": (
                        "Minimum rerank relevance score (0.0-1.0, default 0.0) for "
                        "the RAG lane only. Does NOT affect the symbol-graph facts, "
                        "which are always returned."
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
            "questions like 'Which Lambdas call log_audit_entry?'. Returns "
            "the source files, line numbers, and caller symbol IDs."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "symbol_id": {
                    "type": "string",
                    "description": (
                        "Canonical symbol ID of the target (e.g., "
                        "'python:lambda.shared.audit::log_audit_entry')"
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
            "Pass an exact subpath (e.g., '@acme-corp/shared-utils/http2') for exact "
            "match, or a package prefix (e.g., '@acme-corp/shared-utils' or "
            "'@acme-corp/shared-utils/*') for all consumers of any subpath."
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
    {
        "name": "find_symbol_at_line",
        "description": (
            "Resolve a (file_path, line) to the innermost defining symbol so "
            "a chat citation like 'called from foo (line 544)' can be "
            "verified as graph-grounded before being emitted. Use this for "
            "EVERY '(name, file, line)' citation: pass the file+line you "
            "intend to cite, and use the returned target_symbol_id as the "
            "name in your prose. If the result is null (no defining symbol "
            "covers that line -- e.g. module-level code, blank line between "
            "functions), do NOT invent or guess an enclosing function name; "
            "cite only file:line. v1 uses start-line only and may "
            "false-positive when the cited line falls between two defines; "
            "the line-citation grounding rule in the chat prompt tells you "
            "to drop the name claim rather than cite a function that almost "
            "contains the line."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": "Workspace ID to scope the query to.",
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "Repository in 'owner/name' OR bare 'name' form. "
                        "The handler normalizes to bare for the graph PK lookup."
                    ),
                },
                "file_path": {
                    "type": "string",
                    "description": (
                        "Repo-relative file path (e.g., "
                        "'lambda/auth/handler.py')."
                    ),
                },
                "line": {
                    "type": "integer",
                    "description": "1-indexed line number to resolve.",
                },
            },
            "required": ["workspace_id", "repo", "file_path", "line"],
        },
    },
    # ------------------------------------------------------------------
    # Read-source adjudicator (plan-read-source-adjudicator ).
    # Bounded source-read for verifying exact code values that the
    # code-map deliberately omits (literals, key prefixes, signature
    # arguments per ADR-027) and that the symbol graph stores anchors
    # for but not values. The graph tells you WHERE; this tool tells
    # you WHAT'S ACTUALLY ON THAT LINE.
    # ------------------------------------------------------------------
    {
        "name": "read_source",
        "description": (
            "Fetch a bounded window of source code from GitHub, Azure "
            "DevOps, or GitLab anchored at file:line. The platform is "
            "resolved by looking up the workspace's enrolled repositories "
            "(NOT by counting slashes): `owner/name` is usually GitHub "
            "and `org/project/repo` usually Azure DevOps, but GitLab "
            "supports nested subgroups so its slug is "
            "`group/{...subgroups}/project` with a variable number of "
            "slashes (>=2) and is disambiguated by the enrolled-repo "
            "lookup, not its shape. The slash-count hint above is "
            "advisory only. Use this when an exact code value is being "
            "claimed (a literal, a key prefix like 'MAGIC_TOKEN#', a "
            "specific algorithm choice, a signature parameter) and the "
            "code-map summary plus the symbol graph are not enough to "
            "verify it. The code-map deliberately omits literals "
            "(ADR-027) and the symbol graph stores anchors but not "
            "values -- read_source supplies the actual line. Reads HEAD "
            "of the default branch by default; pass `ref` to pin to a "
            "specific SHA (recommended when the file:line came from the "
            "symbol graph, since the graph's anchor is pinned to the "
            "graph_sha and HEAD may have drifted). The response payload's "
            "`platform` field is 'github', 'azure_devops', or 'gitlab' so "
            "the caller can log which side served the read."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "workspace_id": {
                    "type": "string",
                    "description": (
                        "Workspace ID scoping auth + code-hosting "
                        "connection lookup (GitHub App installation or "
                        "Azure DevOps OAuth connection)."
                    ),
                },
                "repo": {
                    "type": "string",
                    "description": (
                        "Repository slug. For GitHub: 'owner/name' "
                        "(e.g. 'bcarpio/eShop'). For Azure DevOps: "
                        "'org/project/repo' "
                        "(e.g. 'contoso/Platform/payments-api'). "
                        "For GitLab: the full project path "
                        "'group/{...subgroups}/project' "
                        "(e.g. 'acme/platform/backend/api') -- nested "
                        "subgroups mean a variable number of slashes. The "
                        "platform is determined by looking up the "
                        "workspace's enrolled repositories, not by slash "
                        "count; the router rejects a repo that doesn't "
                        "match the workspace's actual connection."
                    ),
                },
                "provider": {
                    "type": "string",
                    "enum": ["github", "azure_devops", "gitlab"],
                    "description": (
                        "Optional explicit code-hosting platform hint. "
                        "When omitted the platform is resolved from the "
                        "workspace's enrolled repositories (GitHub -> "
                        "Azure DevOps -> GitLab). Supply this to pin "
                        "resolution to one provider -- useful for a "
                        "GitLab slug whose shape is ambiguous with an "
                        "Azure DevOps triplet."
                    ),
                },
                "file_path": {
                    "type": "string",
                    "description": "Repo-relative file path (e.g. 'src/eShop.ServiceDefaults/SignaturesDemo.cs').",
                },
                "line": {
                    "type": "integer",
                    "description": "1-indexed line number to anchor the read window on.",
                },
                "context_lines": {
                    "type": "integer",
                    "description": (
                        "Lines of context above + below the anchor "
                        "(default 20, max 200). The total window "
                        "returned is up to 2*context_lines+1 lines."
                    ),
                },
                "ref": {
                    "type": "string",
                    "description": (
                        "Optional Git ref (commit SHA, branch, or tag). "
                        "Default: the repository's default branch HEAD. "
                        "Pass the graph_sha when reading a line resolved "
                        "via the symbol graph to avoid SHA-skew."
                    ),
                },
                "symbol": {
                    "type": "string",
                    "description": (
                        "Optional canonical symbol id (e.g. "
                        "'python:lambda.auth.handler::require_auth'). "
                        "When supplied, the response includes "
                        "`relocated_to_line` -- the actual line within "
                        "the returned window where this symbol's bare "
                        "name appears, so a stale anchor self-corrects. "
                        "Required when `at_graph_sha` is true."
                    ),
                },
                "at_graph_sha": {
                    "type": "boolean",
                    "description": (
                        "When true, resolve `repo` + `file_path` + `line` "
                        "from `symbol` via find_definitions and pin `ref` "
                        "to the workspace's current graph_sha. Eliminates "
                        "SHA-skew between the graph-pinned anchor and a "
                        "HEAD read. The caller's `repo`/`file_path`/`line`"
                        "/`ref` are overridden when this is set."
                    ),
                },
            },
            "required": ["workspace_id"],
        },
    },
]


# ============================================================================
# API Key Authentication
# ============================================================================


# per-instance validate_api_key cache. Every MCP
# request currently does 3 DDB ops on the key path (GSI1 Query + last_used_at
# UpdateItem + audit PutItem). Under 2000 engineers running Claude Code
# fleet-wide that's thousands of DDB ops/hour just for auth. Cache the
# positive validation result for MCP_API_KEY_CACHE_TTL_SECONDS (default 60s)
# — a revoked key still fails on next TTL boundary. last_used_at gets
# throttled to once per TTL window per key. The audit PutItem stays
# per-request — auditability trumps cost per [[project_moat_audit]].
_MCP_API_KEY_CACHE_TTL = int(os.environ.get("MCP_API_KEY_CACHE_TTL_SECONDS", "60"))
_api_key_cache: dict[str, tuple[dict, float]] = {}
_api_key_cache_lock = threading.Lock()


def validate_api_key(api_key, source_ip=""):
    """Validate an API key against DynamoDB. Returns user email or None.

    Emits ACTION_API_KEY_USED audit row on every validation attempt so
    InfoSec can see programmatic-access patterns from MCP clients.
    """
    if not api_key or not DYNAMODB_TABLE:
        return None

    key_hash = hashlib.sha256(api_key.encode()).hexdigest()
    now = time.time()

    # Cache lookup. Only positive results are cached (see write below) —
    # negative results always go back to DDB so a rotated-in key is
    # honored immediately.
    with _api_key_cache_lock:
        cached = _api_key_cache.get(key_hash)
    if cached is not None and cached[1] > now:
        info = cached[0]
        # Positive-cache hits still emit an audit row (auditability
        # invariant), but skip the last_used_at write — it's already been
        # written on the previous TTL boundary and would be a wasted
        # RCU/WCU pair per MCP call.
        _emit_api_key_used(
            info["key_id"], info["key_name"], info["email"], True, "", source_ip,
        )
        return info["email"]

    try:
        dynamodb = _get_dynamodb()
        table = dynamodb.Table(DYNAMODB_TABLE)

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

        # Update last_used_at (best-effort). Only on cache MISS — the cache
        # window bounds how often we touch the row.
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

        # Cache the positive result. Negative results are NOT cached so
        # a newly-provisioned or rotated key isn't shadowed by a stale
        # not-found. Revoked keys are handled by the TTL bound.
        with _api_key_cache_lock:
            _api_key_cache[key_hash] = (
                {"email": email, "key_id": key_id, "key_name": key_name},
                now + _MCP_API_KEY_CACHE_TTL,
            )

        _emit_api_key_used(key_id, key_name, email, True, "", source_ip)
        return email
    except ClientError as e:
        logger.error(f"Error validating API key: {e}")
        return None


# ---------------------------------------------------------------------------
# Azure AD OAuth (JWT) auth
# ---------------------------------------------------------------------------

_jwks_client = None
_jwks_client_lock = threading.Lock()


def _get_jwks_client():
    """Return a cached PyJWKClient for the tenant's signing keys (per task).

    PyJWKClient fetches the JWKS from Azure AD's discovery endpoint and caches
    the signing keys in-process (1h lifespan) -- same cadence as the DDB-session
    and api-key caches.
    """
    global _jwks_client
    if not AZURE_AD_TENANT_ID:
        return None
    with _jwks_client_lock:
        if _jwks_client is None:
            import jwt  # PyJWT; deferred like the read_source path
            jwks_uri = (
                f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}"
                "/discovery/v2.0/keys"
            )
            _jwks_client = jwt.PyJWKClient(
                jwks_uri, cache_keys=True, lifespan=3600)
        return _jwks_client


def _validate_azure_ad_jwt(bearer, source_ip=""):
    """Validate an Azure AD access token (JWT). Returns user email or None.

    Verifies the RS256 signature against the tenant JWKS, plus iss / aud / exp /
    nbf, then maps preferred_username | upn | email to the user identity.
    """
    if not AZURE_AD_TENANT_ID or not MCP_JWT_AUDIENCE:
        logger.warning(
            "[auth] JWT presented but AZURE_AD_TENANT_ID/MCP_JWT_AUDIENCE unset")
        return None
    client = _get_jwks_client()
    if client is None:
        return None
    try:
        import jwt  # PyJWT
        signing_key = client.get_signing_key_from_jwt(bearer)
        issuer = f"https://login.microsoftonline.com/{AZURE_AD_TENANT_ID}/v2.0"
        claims = jwt.decode(
            bearer,
            signing_key.key,
            algorithms=["RS256"],
            audience=MCP_JWT_AUDIENCE,
            issuer=issuer,
            options={"require": ["exp", "iss", "aud"]},
        )
    except Exception as e:  # jwt.* errors + JWKS fetch/parse
        logger.warning(f"[auth] Azure AD JWT rejected: {e}")
        _emit_api_key_used("", "azure-ad-jwt", "", False, "jwt_invalid", source_ip)
        return None

    email = (
        claims.get("preferred_username")
        or claims.get("upn")
        or claims.get("email")
    )
    if not email:
        logger.warning(
            "[auth] Azure AD JWT valid but no preferred_username/upn/email claim")
        _emit_api_key_used("", "azure-ad-jwt", "", False, "jwt_no_email", source_ip)
        return None
    email = email.lower()
    _emit_api_key_used("", "azure-ad-jwt", email, True, "", source_ip)
    return email


def validate_bearer(bearer, source_ip=""):
    """Validate an Authorization bearer, dispatching by token shape.

    A JWT (compact JWS -- its base64url header object starts ``eyJ``) is
    validated against Azure AD (OAuth). Anything else is a legacy static API
    key, honored only while ``MCP_STATIC_KEYS_ENABLED`` is true and logged
    deprecated. Returns the user email or None.
    """
    if not bearer:
        return None
    if bearer.startswith("eyJ"):
        return _validate_azure_ad_jwt(bearer, source_ip)
    if not MCP_STATIC_KEYS_ENABLED:
        logger.warning(
            "[auth] static API key presented but MCP_STATIC_KEYS_ENABLED=false; "
            "rejecting (client must use an Azure AD OAuth token)")
        _emit_api_key_used("", "", "", False, "static_keys_disabled", source_ip)
        return None
    email = validate_api_key(bearer, source_ip)
    if email:
        logger.info(
            f"[auth] static API key used (deprecated=true) for {email}; "
            "migrate this client to Azure AD OAuth")
    return email


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
    # ADR-016 v1.2 -- MCP retrieval attributes under chat workload.
    embed_model_id = (
        resolve_profile_arn("chat", "titan-embed") or EMBEDDING_MODEL_ID
    )
    response = bedrock.invoke_model(
        modelId=embed_model_id,
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

    # Without rerank, fetch 2x so each lane has a full top_k of candidates
    # and the boosted merge (not the 50/50 fetch) decides the mix.
    fetch_k = min(top_k * 4, RERANK_MAX_FETCH) if RERANK_ENABLED else top_k * 2

    try:
        # Dual-channel (ADR-033): the engineer surface (Claude Code / Cursor)
        # is the population most likely to ask the structural questions where
        # a single lane starves the code_map facts. Two lanes always fire, the
        # workspace scope ANDed with each lane's type taxonomy, results stamped
        # with their lane for the per-lane rerank below.
        docs = shared_retrieval.search_kb_dual_channel(
            query,
            fetch_k,
            s3vectors_client=_get_s3vectors_client(),
            vector_bucket=S3_VECTORS_BUCKET,
            vector_index=S3_VECTORS_INDEX,
            query_embedding=embedding,
            workspace_ids=workspace_ids,
            text_truncate=0,  # snippet windowing happens in format_results
        )
    except ClientError as e:
        logger.error(f"Error searching vectors: {e}")
        return []

    if RERANK_ENABLED and len(docs) > 1:
        # Per-lane rerank so the code_map lane keeps its slots instead of
        # losing them to doc-lane prose on a combined-pool rerank -- the fix
        # this plan ships for MCP. NOTE: Bedrock Rerank only accepts a
        # FOUNDATION-MODEL ARN (rejects application inference profile ARNs), so
        # unlike InvokeModel it can't attribute to the ADR-016 v1.2 workload
        # profile; use the bare model id. Rerank spend is negligible vs LLM
        # synthesis ([[project_retrieval_cost_model]]).
        docs = shared_retrieval.rerank_dual_channel(
            bedrock_agent_runtime_client=_get_bedrock_agent_runtime_client(),
            rerank_model_id=RERANK_MODEL_ID,
            query=query,
            documents=docs,
            top_k=top_k,
            lambda_name="mcp-server",
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        )
        if min_score > 0:
            docs = [d for d in docs if d.get("rerank_score", 0) >= min_score]
    else:
        # No rerank: boosted merge on cosine scores so code-maps still
        # outrank docs (ADR-033 v1.1) instead of a code-first truncation.
        docs = shared_retrieval.merge_dual_channel(docs, top_k)

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
    """Get workspace IDs the user has access to, including shared workspaces.

    both the admin-enumerate-all branch and the
    regular-user share-resolution branch cache their DDB scans per-instance
    for WORKSPACE_CACHE_TTL_SECONDS (default 60s). At enterprise scale each
    MCP tool call was doing a full workspaces-table Scan (2000+ rows);
    steady-state cost drops ~99% while a new workspace still appears within
    the TTL window. Per-user direct-memberships are cached under a
    user-keyed entry.
    """
    from shared import ws_cache  # local import: shared layer

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        # Check if org admin. Not cached — this Item is small and the
        # is_admin flag is what forks the branch below; caching would
        # tangle the two branches.
        org_resp = table.get_item(Key={"PK": f"USER#{email}", "SK": "ORG_MEMBER"})
        is_admin = org_resp.get("Item", {}).get("org_role") == "org_admin"

        if is_admin:
            def _compute_all_ids() -> List[str]:
                # Enumerate all WS#... METADATA rows. Paginated per
                # ADR-013 / [[feedback_enterprise_scale_pagination]].
                out: List[str] = []
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
                            out.append(pk.replace("WS#", ""))
                    last_key = response.get("LastEvaluatedKey")
                    if not last_key:
                        break
                    scan_kwargs["ExclusiveStartKey"] = last_key
                return out

            return ws_cache.get_or_compute(
                (DYNAMODB_TABLE, "all_workspace_ids"),
                _compute_all_ids,
            )

        # Regular user branch. Direct memberships cached per-user; the
        # share-resolution METADATA scan is cached once for the whole
        # instance.
        def _compute_direct_ids() -> List[str]:
            out: List[str] = []
            query_kwargs = {
                "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
                "ExpressionAttributeValues": {
                    ":pk": f"USER#{email}",
                    ":sk": "WS_MEMBER#",
                },
            }
            while True:
                response = table.query(**query_kwargs)
                for item in response.get("Items", []) or []:
                    ws = item.get("workspace_id")
                    if ws:
                        out.append(ws)
                if "LastEvaluatedKey" not in response:
                    break
                query_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
            return out

        direct_ids: List[str] = ws_cache.get_or_compute(
            (DYNAMODB_TABLE, "user_memberships", email),
            _compute_direct_ids,
        )

        if not direct_ids:
            return []

        def _compute_share_rows() -> List[dict]:
            # (PK, search_allowed_workspaces) tuples for every WS# METADATA
            # row. Small payload per row; the enterprise cost is the row
            # count, which caching absorbs.
            out: List[dict] = []
            scan_kwargs = {
                "FilterExpression": "begins_with(SK, :sk)",
                "ExpressionAttributeValues": {":sk": "METADATA"},
                "ProjectionExpression": "PK, search_allowed_workspaces",
            }
            while True:
                ws_scan = table.scan(**scan_kwargs)
                out.extend(ws_scan.get("Items", []))
                last_key = ws_scan.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
            return out

        share_rows = ws_cache.get_or_compute(
            (DYNAMODB_TABLE, "share_metadata_rows"),
            _compute_share_rows,
        )

        all_ids = set(direct_ids)
        for item in share_rows:
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


def _ddb_normalize_tools(value):
    """Recursively convert ``decimal.Decimal`` -> int/float in a DDB-
    fetched tools blob so downstream JSON serialization + strict SDK
    type validators (Bedrock Converse's ``document`` param) don't
    reject numeric ``default`` / ``minimum`` / ``maximum`` fields on
    a proxied MCP server's inputSchema. Mirror of ``_ddb_normalize``
    in ``lambda/chat/handler.py``."""
    from decimal import Decimal
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_ddb_normalize_tools(v) for v in value]
    if isinstance(value, dict):
        return {k: _ddb_normalize_tools(v) for k, v in value.items()}
    return value


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

    # Load cached tool manifest from DynamoDB. Normalize any Decimal
    # values in the schema (e.g. numeric ``default`` / ``minimum`` /
    # ``maximum`` fields from an MCP server's inputSchema) to native
    # int/float so downstream JSON serialization + strict Bedrock
    # SDK type validation succeed. Mirror of ``_ddb_normalize`` in
    # the chat handler.
    tools_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "TOOLS"})
    tools_item = tools_resp.get("Item")
    tools = _ddb_normalize_tools(tools_item.get("tools", [])) if tools_item else []

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

    # Get or create MCP session with the target server. Cached in the shared
    # session store under the upstream# prefix so the same negotiated session
    # can be reused across the Fargate task pool.
    now = time.time()
    upstream_key = f"{_UPSTREAM_PREFIX}{mcp_id}"
    cached_session = _get_session_store().get(upstream_key)
    if cached_session and (now - cached_session.get("created_at", 0)) < MCP_SESSION_TTL:
        headers["Mcp-Session-Id"] = cached_session["session_id"]
    else:
        # Initialize new session
        session_id = _mcp_initialize_session(url, headers)
        if session_id:
            _get_session_store().put(upstream_key, {
                "session_id": session_id,
                "created_at": now,
                "last_active": now,
            })
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


def handle_search_hybrid(params, email):
    """Handle search_hybrid: dual-channel RAG + symbol-graph facts in one
    ADR-058-grounded response.

    Returns both the narrative/code-map retrieval and the structural graph
    facts for the symbol the question is about, so a client that does not
    orchestrate two tool calls still gets the hybrid. The ``symbol_graph``
    section always carries the grounding envelope and is returned regardless
    of the RAG rerank ``min_score`` gate -- that gate can evict a code-map on
    pinpoint structural phrasing, which is exactly when the graph facts matter
    most. Fail-soft: if one lane errors, the other is still returned.
    """
    query = params.get("query", "")
    if not query:
        return {"error": "query is required"}

    workspace_id = params.get("workspace_id")
    if workspace_id:
        # Explicit scope -> enforce the same per-workspace access gate the
        # symbol-graph tools use, so the fused graph facts cannot leak across
        # a workspace the caller may not read.
        deny = _check_symbol_graph_workspace_access(workspace_id, email)
        if deny is not None:
            return deny
        workspace_ids = [workspace_id]
    else:
        workspace_ids = get_user_workspace_ids(email)

    top_k = _bounded_int(params.get("top_k"), DEFAULT_TOP_K, 1, MAX_TOP_K)
    max_tokens = _bounded_int(
        params.get("max_tokens"), DEFAULT_MAX_TOKENS, 1, MAX_MAX_TOKENS
    )
    min_score = _bounded_float(params.get("min_score"), DEFAULT_MIN_SCORE, 0.0, 1.0)

    # --- RAG lane (fail-soft: a failure here must not drop the graph facts) ---
    rag_sources = []
    rag_text = ""
    try:
        rag_results = search_knowledge_base(
            query, workspace_ids=workspace_ids, top_k=top_k, min_score=min_score
        )
        rag_text = format_results(rag_results, query=query, max_tokens=max_tokens)
        # Lightweight per-source metadata (ADR-058: the lane + type annotation
        # conveys retrieval origin so a client can apply code-over-docs). No full
        # text here -- the bounded content lives in ``rag_text`` to stay budgeted.
        rag_sources = [
            {
                "repo": d.get("repo", ""),
                "type": d.get("type", ""),
                "lane": d.get("lane", ""),
                "file_path": d.get("file_path", ""),
                "score": d.get("score"),
                "rerank_score": d.get("rerank_score"),
            }
            for d in rag_results
        ]
    except Exception as exc:  # pragma: no cover - defensive; keep the graph lane
        logger.warning(f"[search_hybrid] RAG lane failed: {exc}")
        rag_text = ""
        rag_sources = []

    # --- Symbol-graph lane (fail-soft: keep RAG if the graph errors) ---
    symbol_graph = {"grounding": _SYMBOL_GRAPH_GROUNDING}
    try:
        from shared.hybrid_augment import augment_with_symbol_graph  # lazy import

        aug = augment_with_symbol_graph(
            query,
            workspace_ids,
            user_id=email or "",
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE") or None,
            code_maps_table_name=os.environ.get("CODE_MAPS_TABLE") or None,
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        )
        decision = aug.get("decision")
        symbol_graph.update(
            {
                "intent": getattr(decision, "intent", None),
                "symbol": getattr(decision, "symbol_candidate", None),
                "relation": aug.get("graph_relation"),
                "sites": aug.get("graph_sites") or [],
                "result_count": aug.get("graph_result_count"),
                "graph_sha": aug.get("graph_sha"),
                "staleness_seconds": aug.get("graph_staleness_seconds"),
                "canonicals_truncated": aug.get("graph_canonicals_truncated"),
                "skip_reason": aug.get("graph_skip_reason"),
            }
        )
    except Exception as exc:  # pragma: no cover - defensive; keep the RAG lane
        logger.warning(f"[search_hybrid] symbol-graph lane failed: {exc}")
        symbol_graph["skip_reason"] = "augment_error"

    return {
        "text": json.dumps(
            {"rag": rag_sources, "rag_text": rag_text, "symbol_graph": symbol_graph}
        )
    }


def handle_list_workspaces(params, email):
    """Handle list_workspaces tool call.

    Returns the caller's own workspaces plus every ``is_global=true``
    workspace they can read but aren't a member of (mirror of the
    web-app ``list_workspaces`` behavior). External agents can then
    reason about scope: a ``Global`` badge on a workspace tells the
    agent its content applies to every retrieval across the deploy.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        workspace_ids = get_user_workspace_ids(email)
        seen: set = set()
        workspaces = []
        for ws_id in workspace_ids:
            resp = table.get_item(Key={"PK": f"WS#{ws_id}", "SK": "METADATA"})
            if "Item" in resp:
                item = resp["Item"]
                workspaces.append({
                    "workspace_id": ws_id,
                    "name": item.get("name", ""),
                    "description": item.get("description", ""),
                    "is_global": bool(item.get("is_global", False)),
                })
                seen.add(ws_id)

        # Append every is_global workspace the caller can't already see.
        # Paginated scan filtered to METADATA rows with is_global=true.
        # Caller has to be authenticated (validated earlier in the
        # request path) but doesn't need membership to enumerate
        # globals -- global content is by definition non-confidential.
        try:
            scan_kwargs = {
                "FilterExpression": "begins_with(SK, :sk) AND is_global = :ig",
                "ExpressionAttributeValues": {
                    ":sk": "METADATA", ":ig": True,
                },
            }
            while True:
                ws_scan = table.scan(**scan_kwargs)
                for item in ws_scan.get("Items", []):
                    ws_id = item.get("workspace_id")
                    if not ws_id or ws_id in seen:
                        continue
                    workspaces.append({
                        "workspace_id": ws_id,
                        "name": item.get("name", ""),
                        "description": item.get("description", ""),
                        "is_global": True,
                    })
                    seen.add(ws_id)
                last_key = ws_scan.get("LastEvaluatedKey")
                if not last_key:
                    break
                scan_kwargs["ExclusiveStartKey"] = last_key
        except Exception as exc:
            # If the global-scan fails, degrade gracefully to
            # membership-only listing. External agents can't tell the
            # difference from an owner-with-no-globals case.
            logger.warning(f"list_workspaces global-scan failed: {exc}")

        text = "Your workspaces:\n\n"
        if not workspaces:
            text = "You have no workspaces."
        else:
            for ws in workspaces:
                marker = " [Global]" if ws.get("is_global") else ""
                text += f"- **{ws['name']}**{marker} (ID: {ws['workspace_id']})"
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
    # shared workspaces, and org-admin enumeration. ADR-037: is_global
    # workspaces are readable by any authenticated caller regardless of
    # membership.
    user_ws_ids = get_user_workspace_ids(email)
    if workspace_id not in user_ws_ids and not _workspace_is_global(workspace_id):
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


# MCP has no synthesis-prompt seam (ADR-058): the client model (Claude Code /
# Cursor) synthesizes under its OWN system prompt, so the eight chat/RAG prose
# grounding rules cannot be injected server-side. The discipline MCP CAN carry
# is data on the tool response. This marks graph-tool facts as structural,
# graph-sourced, and citable at file_path:line -- the ADR-031 line-citation and
# the magic_link structural-attribution rule expressed as data a client model
# can act on. Three short fields, by design (client token budget).
_SYMBOL_GRAPH_GROUNDING = {
    "source": "symbol-graph",
    "cite_as": "file_path:line",
    "rule": (
        "structural fact; cite the site's file_path:line and do not "
        "attribute it to prose/doc chunks"
    ),
}


def _query_result_to_dict(query_result) -> dict:
    """Project a ``QueryResult`` onto the JSON shape documented for the
    MCP tools: ``sites`` + ``truncated`` + ``complete`` + ``total_known``
    + ``graph_sha`` + ``staleness_seconds`` + ``grounding``.

    ``complete`` and ``total_known`` (ADR-032 list-completeness
    contract) are forwarded so chat synthesis can detect a clipped
    list and either disclose or refuse rather than ship a confidently-
    incomplete enumeration. ``truncated`` remains for back-compat
    with consumers that already read it; ``complete = not truncated``.
    ``grounding`` carries the structural-attribution provenance (ADR-058).
    """
    sites = []
    for s in query_result.sites:
        entry = {
            "file_path": s.file_path,
            "line": s.line,
            "repo": s.repo,
            "source_symbol_id": s.source_symbol_id,
            "language": s.language,
        }
        sig = getattr(s, "signature", None)
        if sig:
            entry["signature"] = sig
        sites.append(entry)
    return {
        "sites": sites,
        "truncated": bool(query_result.truncated),
        "complete": bool(query_result.complete),
        "total_known": query_result.total_known,
        "graph_sha": query_result.graph_sha,
        "staleness_seconds": query_result.staleness_seconds,
        "grounding": _SYMBOL_GRAPH_GROUNDING,
    }


def _resolution_result_to_dict(resolution_result) -> dict:
    """Project a ``ResolutionResult`` (fuzzy_resolve_by_name) onto the
    JSON shape for the find_symbol_by_name MCP tool. Carries the same
    list-completeness fields as ``_query_result_to_dict`` (ADR-032) plus
    the structural-attribution ``grounding`` (ADR-058).
    """
    return {
        "canonicals": list(resolution_result.canonicals),
        "truncated": bool(resolution_result.truncated),
        "complete": bool(resolution_result.complete),
        "total_known": resolution_result.total_known,
        "grounding": _SYMBOL_GRAPH_GROUNDING,
    }


def _workspace_is_global(workspace_id):
    """Return True if the workspace has ``is_global=true`` on its METADATA row.

    ADR-037: is_global workspaces are readable by any authenticated caller
    in the deploy regardless of membership. This helper is the pass-through
    for per-tool workspace-access gates on the MCP server. Fail-soft: on
    any DDB error, return False so callers collapse to the membership-only
    behavior rather than silently opening access.
    """
    if not workspace_id:
        return False
    try:
        table = _get_dynamodb().Table(DYNAMODB_TABLE)
        resp = table.get_item(Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"})
        return bool(resp.get("Item", {}).get("is_global", False))
    except ClientError:
        return False


def _check_symbol_graph_workspace_access(workspace_id, email):
    """Return None if access is allowed, else an error-payload dict.

    Access is granted when the caller is a member (direct or via
    cross-workspace share) of the requested workspace OR when the
    workspace is flagged ``is_global=true`` (ADR-037: globals are readable
    by any authenticated caller). The symbol-graph reader itself does NOT
    enforce auth -- the gateway does.
    """
    if not workspace_id:
        return {"error": "workspace_id is required"}
    user_ws_ids = get_user_workspace_ids(email)
    if workspace_id in user_ws_ids:
        return None
    if _workspace_is_global(workspace_id):
        return None
    return {"error": "You do not have access to this workspace"}


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

    ``definitions`` returns ``QueryResult`` -- cross-repo duplicates
    surface inside ``result.sites``. The MCP envelope carries the
    standard list-completeness fields (``truncated`` + ``complete`` +
    ``total_known``) so a clipped result is detectable by chat
    synthesis (ADR-032).
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
        result = symbol_graph_reader.definitions(
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

    return {"text": json.dumps(_query_result_to_dict(result))}


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
        resolution = symbol_graph_reader.fuzzy_resolve_by_name(
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

    return {"text": json.dumps(_resolution_result_to_dict(resolution))}


def handle_find_symbol_at_line(params, email):
    """Resolve a (file_path, line) to the innermost defining symbol.

    Enforces the line-citation grounding contract (ADR-031): a chat
    answer that writes "name (file:line)" MUST verify the enclosing
    name via this tool rather than infer it from surrounding text.
    """
    workspace_id = params.get("workspace_id", "")
    repo = params.get("repo", "")
    file_path = params.get("file_path", "")
    line = params.get("line")

    if not workspace_id:
        return {"error": "workspace_id is required"}
    if not repo:
        return {"error": "repo is required"}
    if not file_path:
        return {"error": "file_path is required"}
    if line is None:
        return {"error": "line is required"}
    try:
        line_int = int(line)
    except (TypeError, ValueError):
        return {"error": "line must be an integer"}

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
        enclosing = symbol_graph_reader.symbol_at_line(
            workspace_id,
            repo,
            file_path,
            line_int,
            symbol_graph_table_name=os.environ.get("SYMBOL_GRAPH_TABLE")
            or None,
        )
    except symbol_graph_reader.SymbolGraphUnavailable as exc:
        logger.warning(
            f"Symbol graph unavailable for find_symbol_at_line: {exc}"
        )
        return {"error": "Symbol graph unavailable"}
    except ValueError as exc:
        return {"error": str(exc)}

    if enclosing is None:
        return {
            "text": json.dumps(
                {
                    "symbol": None,
                    "queried_file_path": file_path,
                    "queried_line": line_int,
                }
            )
        }

    return {
        "text": json.dumps(
            {
                "symbol": {
                    "target_symbol_id": enclosing.target_symbol_id,
                    "file_path": enclosing.file_path,
                    "line": enclosing.line,
                    "repo": enclosing.repo,
                    "language": enclosing.language,
                },
                "queried_file_path": file_path,
                "queried_line": line_int,
                "grounding": _SYMBOL_GRAPH_GROUNDING,
            }
        )
    }


# ============================================================================
# Read-source adjudicator (ADR-030)
#
# Bounded source read for verifying exact code values that the code-map
# deliberately omits (ADR-027). The platform resolution, token minting and
# fetch live in shared.source_reader, which chat also uses to attach source
# windows server-side.
# ============================================================================

from shared import source_reader as _source_reader  # type: ignore  # noqa: E402

_READ_SOURCE_DEFAULT_CONTEXT = _source_reader.DEFAULT_CONTEXT_LINES
_READ_SOURCE_MAX_CONTEXT = _source_reader.MAX_CONTEXT_LINES


def handle_read_source(params, email):
    """Bounded source-read for verifying exact values.

    Workflow:
    1. Validate inputs.
    2. Check workspace access (same gate as the symbol-graph tools).
    3. If ``at_graph_sha`` is set, resolve ``repo`` + ``file_path`` +
       ``line`` from ``symbol`` via the symbol graph and pin ``ref`` to
       the workspace's current graph_sha. Otherwise read the
       caller-specified file:line at the caller-specified ref
       (default HEAD).
    4. Resolve the workspace's code-hosting platform (ADR-030 v1.1:
       GitHub OR Azure DevOps).
    5. Mint a platform-specific token + fetch the file (GitHub Contents
       API or ADO items API).
    6. Slice a window of size 2*context+1 around the anchor line.
    7. If a ``symbol`` was supplied, search the window for its bare
       name and report ``relocated_to_line`` so a stale anchor
       self-corrects against the actual symbol position. The original
       anchor is preserved as ``anchor_line``.
    """
    workspace_id = params.get("workspace_id", "")
    repo = params.get("repo", "")
    file_path = params.get("file_path", "")
    line_raw = params.get("line")
    ref = params.get("ref") or None
    symbol = params.get("symbol", "")
    at_graph_sha = bool(params.get("at_graph_sha"))

    if not workspace_id:
        return {"error": "workspace_id is required"}
    if at_graph_sha and not symbol:
        return {"error": "at_graph_sha requires a symbol to resolve"}

    # In the explicit-anchor mode (no graph resolution), the caller
    # must supply repo/file_path/line up front. In graph-resolution
    # mode they are filled in below from the symbol-graph lookup.
    if not at_graph_sha:
        if not repo or not file_path or line_raw in (None, ""):
            return {"error": "workspace_id, repo, file_path, and line are required"}
        try:
            int(line_raw)
        except (TypeError, ValueError):
            return {"error": "line must be an integer"}
        if int(line_raw) < 1:
            return {"error": "line must be 1-indexed (>=1)"}
        ctx_raw = params.get("context_lines", _READ_SOURCE_DEFAULT_CONTEXT)
        try:
            ctx = int(ctx_raw) if ctx_raw not in (None, "") else _READ_SOURCE_DEFAULT_CONTEXT
        except (TypeError, ValueError):
            return {"error": "context_lines must be an integer"}
        if ctx < 0 or ctx > _READ_SOURCE_MAX_CONTEXT:
            return {"error": f"context_lines must be 0-{_READ_SOURCE_MAX_CONTEXT}"}
        # ADR-050 Decision 6: the platform is resolved from the enrolled
        # *_REPO# rows below, NOT from slash count -- GitLab nested-
        # subgroup slugs have a variable number of slashes and would
        # collide with the ADO 3-segment shape. Reject only the clearly
        # invalid no-slash / empty-segment shapes here; the platform-
        # specific cross-check runs after resolution.
        if "/" not in repo or not all(repo.split("/")):
            return {
                "error": (
                    "repo must be a 'group/.../project' (GitLab), "
                    "'owner/name' (GitHub), or 'org/project/repo' "
                    "(Azure DevOps) slug"
                )
            }

    deny = _check_symbol_graph_workspace_access(workspace_id, email)
    if deny is not None:
        return deny

    # Resolve the workspace's connected platform BEFORE symbol-graph
    # resolution: the graph resolver needs to know which full-name
    # resolver to use when the graph row was bare. ADR-050 Decision 6:
    # resolution is a DDB lookup of the enrolled *_REPO# rows (GitHub ->
    # ADO -> GitLab), honoring an explicit `provider` hint when supplied.
    provider_hint = params.get("provider") or None
    repo_hint = repo if not at_graph_sha else None
    platform_info = _source_reader.resolve_platform(
        workspace_id, repo_hint=repo_hint, provider=provider_hint,
    )
    if platform_info is None:
        return {
            "error": (
                f"no code-hosting integration linked to workspace "
                f"{workspace_id}; connect a repo via the UI to enable "
                "read_source"
            )
        }
    platform = platform_info["platform"]

    resolved_graph_sha = None
    if at_graph_sha:
        resolved = _source_reader.resolve_via_graph(
            workspace_id, symbol, platform=platform,
        )
        if resolved.get("error") == "missing":
            return {"error": f"no definition found for {symbol!r} in workspace"}
        if resolved.get("error") == "ambiguous":
            return {
                "error": (
                    f"{symbol!r} has multiple definitions; pass explicit "
                    "repo+file_path+line to disambiguate"
                ),
                "sites": resolved.get("sites", []),
            }
        if resolved.get("error") == "graph_unavailable":
            return {"error": "symbol graph unavailable"}
        if "error" in resolved:
            return {"error": resolved["error"]}
        repo = resolved["repo"]
        file_path = resolved["file_path"]
        line_raw = resolved["line"]
        resolved_graph_sha = resolved.get("graph_sha")
        # Pin ref to graph_sha when we have one. A workspace without a
        # pointer still gets the resolved file:line but reads HEAD;
        # response's ``graph_sha`` field is None to signal that.
        ref = resolved_graph_sha or ref

    if not repo or not file_path or line_raw in (None, ""):
        return {"error": "repo, file_path, and line are required"}
    try:
        line = int(line_raw)
    except (TypeError, ValueError):
        return {"error": "line must be an integer"}
    if line < 1:
        return {"error": "line must be 1-indexed (>=1)"}

    ctx_raw = params.get("context_lines", _READ_SOURCE_DEFAULT_CONTEXT)
    try:
        context_lines = int(ctx_raw) if ctx_raw not in (None, "") else _READ_SOURCE_DEFAULT_CONTEXT
    except (TypeError, ValueError):
        return {"error": "context_lines must be an integer"}
    if context_lines < 0 or context_lines > _READ_SOURCE_MAX_CONTEXT:
        return {"error": f"context_lines must be 0-{_READ_SOURCE_MAX_CONTEXT}"}

    # ADR-050 Decision 6: `platform` came from the enrolled-repo DDB
    # lookup, not slash count. GitLab nested-subgroup slugs have a
    # variable slash count and MUST NOT be shape-checked (a 3-segment
    # GitLab slug would look like ADO; a deeper one like nothing). For
    # GitHub/ADO keep the cheap slash-count cross-check so a caller-sent
    # repo that contradicts the resolved connection is rejected rather
    # than fetched from the wrong host.
    if platform in ("github", "azure_devops"):
        detected_platform = _source_reader.platform_from_repo(repo)
        if detected_platform is None:
            return {
                "error": (
                    "repo must be in 'owner/name' (GitHub) or "
                    "'org/project/repo' (Azure DevOps) form"
                )
            }
        if detected_platform != platform:
            return {
                "error": (
                    f"workspace {workspace_id} is connected via {platform} but "
                    f"repo shape indicates {detected_platform}"
                )
            }

    result = _source_reader.read_window(
        platform_info, repo, file_path, line,
        ref=ref, context_lines=context_lines, symbol=symbol,
    )
    if "error" in result:
        return {"error": result["error"]}

    return {
        "text": json.dumps({
            "repo": result["repo"],
            "file_path": result["file_path"],
            "ref": result["ref"],
            "anchor_line": result["anchor_line"],
            "lines": result["lines"],
            "total_lines": result["total_lines"],
            "window_start": result["window_start"],
            "window_end": result["window_end"],
            "clipped_to_file_bounds": result["clipped_to_file_bounds"],
            "symbol": result["symbol"],
            "relocated_to_line": result["relocated_to_line"],
            "relocated": result["relocated"],
            "graph_sha": resolved_graph_sha,
            "platform": platform,
        })
    }


TOOL_HANDLERS = {
    "query_knowledge_base": handle_query_knowledge_base,
    "search_hybrid": handle_search_hybrid,
    "list_workspaces": handle_list_workspaces,
    "list_workspace_documents": handle_list_workspace_documents,
    "get_coding_standards": handle_get_coding_standards,
    # Symbol-graph query tools (see docs/mcp-server.md)
    "find_callers": handle_find_callers,
    "find_importers": handle_find_importers,
    "find_implementers": handle_find_implementers,
    "find_definitions": handle_find_definitions,
    "find_symbol_by_name": handle_find_symbol_by_name,
    "find_symbol_at_line": handle_find_symbol_at_line,
    # Read-source adjudicator -- bounded source-read for verified-value
    # claims (see docs/adr/ADR-???-read-source-adjudicator.md once lands).
    "read_source": handle_read_source,
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
        if session_id:
            _get_session_store().delete(session_id)
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

        if session_id:
            session = _get_session_store().get(session_id)
            if session and time.time() - session.get("last_active", 0) < SESSION_TIMEOUT:
                email = session["email"]
                # Refresh last_active + TTL. Note: this is a targeted field
                # update rather than a full put(), so tool_routing set by a
                # concurrent tools/list isn't clobbered by a stale copy.
                _get_session_store().update_field(
                    session_id, "last_active", time.time()
                )

        if not email:
            # Try API key auth
            auth_header = self.headers.get("Authorization", "")
            if auth_header.startswith("Bearer "):
                api_key = auth_header[7:]
                email = validate_bearer(
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
            email = validate_bearer(
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
        _get_session_store().put(session_id, {
            "email": email,
            "last_active": time.time(),
        })

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
        session = _get_session_store().get(session_id) if session_id else None
        email = (session or {}).get("email")
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
        if session_id:
            _get_session_store().update_field(session_id, "tool_routing", tool_routing)

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
        session = _get_session_store().get(session_id) if session_id else None
        tool_routing = (session or {}).get("tool_routing", {})

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
                if session_id:
                    _get_session_store().update_field(session_id, "tool_routing", tool_routing)
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
    """Periodically clean up expired sessions.

    for the DDB store, DynamoDB TTL owns eviction
    and this loop no-ops (cleanup_expired returns 0). For the in-memory
    store, it retains the pre-refactor behavior of scanning + deleting
    expired rows every 5 minutes.
    """
    store = _get_session_store()
    while True:
        time.sleep(300)  # Every 5 minutes
        try:
            count = store.cleanup_expired()
            if count:
                logger.info(f"Cleaned up {count} expired sessions")
        except Exception:
            logger.exception("session cleanup pass failed; will retry next tick")


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
