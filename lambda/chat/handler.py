"""
Chat Lambda for OutcomeOps AI Assist

Two invocation modes:
1. SDK Streaming (streaming_handler): Invoked via InvokeWithResponseStream from Fargate proxy
2. REST API (lambda_handler): Conversation management endpoints

This Lambda handles the conversational RAG pipeline:
1. User sends a question
2. Generate query embedding using Bedrock Titan v2
3. Search S3 Vectors for relevant KB documents
4. Pass context to Bedrock ConverseStream for answer synthesis
5. Stream answer tokens with source citations
6. Save conversation to DynamoDB

Security: Lambda is invoked via IAM-authenticated SDK calls from Fargate.
No public endpoint - Fargate task role has invoke permission.
"""

import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, Generator, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from pydantic import BaseModel, Field, ValidationError, field_validator

# Add shared module to path for Lambda deployment
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit import log_audit_entry
from shared.prompts import build_chat_system_prompt

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")

# Cached clients and config
_dynamodb = None
_ssm_client = None
_s3vectors_client = None
_bedrock_client = None
_chat_table = None
_vector_bucket = None
_vector_index = None
_workspaces_table = None
_kms_client = None
_bedrock_agent_runtime_client = None

# Bedrock config with longer timeout for streaming
bedrock_config = Config(
    read_timeout=300,
    connect_timeout=10,
    retries={"max_attempts": 3}
)

# Bedrock model IDs (configurable via environment variables)
ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-6"
)
BASIC_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)
RERANK_MODEL_ID = os.environ.get("BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0")
RERANK_ENABLED = os.environ.get("RERANK_ENABLED", "false").lower() == "true"
RERANK_MAX_FETCH = int(os.environ.get("RERANK_MAX_FETCH", "50"))
RERANK_MIN_SCORE = float(os.environ.get("RERANK_MIN_SCORE", "0"))

# Conversation memory config
MEMORY_CHUNK_SIZE = 10  # Number of messages before creating a memory chunk (5 pairs)
MEMORY_TOP_K = 3  # Number of memory chunks to retrieve


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_s3vectors_client():
    global _s3vectors_client
    if _s3vectors_client is None:
        _s3vectors_client = boto3.client("s3vectors")
    return _s3vectors_client


def _get_bedrock_client():
    global _bedrock_client
    if _bedrock_client is None:
        _bedrock_client = boto3.client("bedrock-runtime", config=bedrock_config)
    return _bedrock_client


def _get_bedrock_agent_runtime_client():
    global _bedrock_agent_runtime_client
    if _bedrock_agent_runtime_client is None:
        _bedrock_agent_runtime_client = boto3.client("bedrock-agent-runtime")
    return _bedrock_agent_runtime_client


def _get_chat_table():
    global _chat_table
    if _chat_table is None:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(Name=f"/{ENV}/{APP_NAME}/dynamodb/chat-table")
        _chat_table = response["Parameter"]["Value"]
    return _chat_table


def _get_vector_bucket():
    global _vector_bucket
    if _vector_bucket is None:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(Name=f"/{ENV}/{APP_NAME}/s3vectors/bucket-name")
        _vector_bucket = response["Parameter"]["Value"]
    return _vector_bucket


def _get_vector_index():
    global _vector_index
    if _vector_index is None:
        ssm = _get_ssm_client()
        response = ssm.get_parameter(Name=f"/{ENV}/{APP_NAME}/s3vectors/index-name")
        _vector_index = response["Parameter"]["Value"]
    return _vector_index


def _get_workspaces_table():
    """Get workspaces table name from env var."""
    global _workspaces_table
    if _workspaces_table is None:
        table_name = os.environ.get("WORKSPACES_TABLE")
        if not table_name:
            raise ValueError("WORKSPACES_TABLE environment variable not set")
        _workspaces_table = table_name
    return _workspaces_table


def _get_kms_client():
    """Get cached KMS client."""
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _decrypt_mcp_credentials(encrypted: str) -> str:
    """Decrypt MCP server credentials using KMS."""
    import base64
    kms = _get_kms_client()
    try:
        response = kms.decrypt(
            CiphertextBlob=base64.b64decode(encrypted),
            KeyId=os.environ.get("OAUTH_KMS_KEY_ARN", ""),
        )
        return response["Plaintext"].decode()
    except ClientError as e:
        logger.error(f"[chat_handler] KMS decrypt failed: {e}", exc_info=True)
        raise


def _validate_workspace_access(user_email: str, workspace_ids: List[str]) -> List[str]:
    """Validate user has access to the requested workspaces.

    Returns list of workspace_ids the user is authorized to access.
    Checks for direct membership, org_admin status, or cross-workspace
    search sharing (where one of the user's workspaces has been granted
    search access to the target workspace).
    """
    if not user_email or not workspace_ids:
        return []

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    # Check if user is org admin (can access all workspaces)
    try:
        org_resp = table.get_item(
            Key={"PK": f"USER#{user_email}", "SK": "ORG_MEMBER"}
        )
        if org_resp.get("Item", {}).get("org_role") == "org_admin":
            return workspace_ids
    except ClientError:
        pass

    # Get user's direct workspace memberships
    user_workspace_ids = set()
    try:
        member_resp = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_email}",
                ":sk": "WS_MEMBER#",
            },
        )
        for item in member_resp.get("Items", []):
            ws_id = item.get("workspace_id") or item.get("SK", "").replace("WS_MEMBER#", "")
            if ws_id:
                user_workspace_ids.add(ws_id)
    except ClientError:
        pass

    authorized = []
    for ws_id in workspace_ids:
        # Direct membership
        if ws_id in user_workspace_ids:
            authorized.append(ws_id)
            continue

        # Cross-workspace search sharing: check if the target workspace
        # has granted search access to any of the user's workspaces
        try:
            ws_resp = table.get_item(
                Key={"PK": f"WS#{ws_id}", "SK": "METADATA"}
            )
            ws_meta = ws_resp.get("Item", {})
            allowed = ws_meta.get("search_allowed_workspaces", [])
            if allowed and any(uws in allowed for uws in user_workspace_ids):
                authorized.append(ws_id)
        except ClientError:
            continue

    return authorized


def _load_mcp_configs(mcp_server_ids: List[str]) -> List[Dict]:
    """Load MCP server configs and tool manifests from DynamoDB."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    configs = []
    for mcp_id in mcp_server_ids:
        try:
            # Get metadata
            meta_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "METADATA"})
            if "Item" not in meta_resp:
                logger.warning(f"[chat_handler] MCP server {mcp_id} not found")
                continue

            metadata = meta_resp["Item"]
            if metadata.get("status") == "error":
                logger.warning(f"[chat_handler] MCP server {mcp_id} has error status, skipping")
                continue

            # Get tools
            tools_resp = table.get_item(Key={"PK": f"MCP#{mcp_id}", "SK": "TOOLS"})
            if "Item" not in tools_resp:
                logger.warning(f"[chat_handler] MCP server {mcp_id} has no cached tools, skipping")
                continue

            tools = tools_resp["Item"].get("tools", [])
            if not tools:
                continue

            configs.append({
                "mcp_id": mcp_id,
                "name": metadata.get("name", ""),
                "url": metadata.get("url", ""),
                "auth_type": metadata.get("auth_type", "none"),
                "encrypted_credentials": metadata.get("encrypted_credentials"),
                "ssm_token_path": metadata.get("ssm_token_path"),
                "ssm_token_header": metadata.get("ssm_token_header"),
                "tools": tools,
            })
        except ClientError as e:
            logger.error(f"[chat_handler] Error loading MCP config for {mcp_id}: {e}", exc_info=True)
            continue

    return configs


def _build_bedrock_tool_config(mcp_configs: List[Dict]):
    """Convert MCP tool manifests to Bedrock toolConfig format.

    Returns (tool_config, tool_mapping) where:
    - tool_config is the Bedrock toolConfig dict
    - tool_mapping maps prefixed tool names to MCP server info
    """
    tools = []
    tool_mapping = {}

    for config in mcp_configs:
        mcp_id = config["mcp_id"]
        for tool in config["tools"]:
            # Prefix tool name with mcp_id to avoid collisions
            prefixed_name = f"{mcp_id}___{tool['name']}"
            # Bedrock tool names must be alphanumeric + underscores, max 64 chars
            prefixed_name = prefixed_name[:64]

            tool_spec = {
                "toolSpec": {
                    "name": prefixed_name,
                    "description": tool.get("description", "No description"),
                    "inputSchema": {
                        "json": tool.get("inputSchema", {"type": "object", "properties": {}})
                    },
                }
            }
            tools.append(tool_spec)

            tool_mapping[prefixed_name] = {
                "mcp_id": mcp_id,
                "original_tool_name": tool["name"],
                "server_config": {
                    "url": config["url"],
                    "auth_type": config["auth_type"],
                    "encrypted_credentials": config.get("encrypted_credentials"),
                    "ssm_token_path": config.get("ssm_token_path"),
                    "ssm_token_header": config.get("ssm_token_header"),
                },
            }

    tool_config = {"tools": tools} if tools else None
    return tool_config, tool_mapping


def _mcp_request(url: str, headers: Dict[str, str], method: str, params: Optional[Dict] = None,
                 request_id: int = 1) -> Dict:
    """Send a JSON-RPC 2.0 request to an MCP server and return the parsed response."""
    import urllib.request

    payload: Dict[str, Any] = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
    }
    if params:
        payload["params"] = params

    merged_headers = dict(headers)
    merged_headers.setdefault("Content-Type", "application/json")
    merged_headers.setdefault("Accept", "application/json, text/event-stream")

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=merged_headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        session_id = resp.headers.get("Mcp-Session-Id")
        raw = resp.read().decode()
        content_type = resp.headers.get("Content-Type") or ""

        # Handle SSE responses (Streamable HTTP may return text/event-stream)
        if "text/event-stream" in content_type:
            # Parse SSE: extract JSON from "data: {...}" lines
            for line in raw.splitlines():
                if line.startswith("data: "):
                    raw = line[6:]
                    break

        data = json.loads(raw)
        if session_id:
            data["_session_id"] = session_id
        return data


def _mcp_initialize(url: str, headers: Dict[str, str]) -> str:
    """Perform MCP protocol initialize handshake, return session ID."""
    import urllib.request

    init_params = {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "outcome-ops-ai-assist", "version": "1.0"},
    }
    data = _mcp_request(url, headers, "initialize", params=init_params, request_id=0)
    if "error" in data:
        raise Exception(f"MCP initialize error: {data['error'].get('message', 'Unknown error')}")

    session_id = data.get("_session_id", "")

    # Send initialized notification
    notif_payload = json.dumps({
        "jsonrpc": "2.0",
        "method": "notifications/initialized",
    }).encode()
    notif_headers = dict(headers)
    if session_id:
        notif_headers["Mcp-Session-Id"] = session_id
    notif_req = urllib.request.Request(url, data=notif_payload, headers=notif_headers, method="POST")
    urllib.request.urlopen(notif_req, timeout=10).read()

    return session_id


def _execute_mcp_tool_call(tool_mapping_entry: Dict, tool_input: Dict) -> str:
    """Execute a tool call against an MCP server via JSON-RPC 2.0 with proper initialization."""
    import urllib.request
    import urllib.error

    server = tool_mapping_entry["server_config"]
    original_name = tool_mapping_entry["original_tool_name"]

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    if server["auth_type"] == "bearer" and server.get("encrypted_credentials"):
        token = _decrypt_mcp_credentials(server["encrypted_credentials"])
        headers["Authorization"] = f"Bearer {token}"
    elif server["auth_type"] == "api_key" and server.get("encrypted_credentials"):
        key = _decrypt_mcp_credentials(server["encrypted_credentials"])
        headers["X-API-Key"] = key

    # Catalog servers: read token from SSM and send as custom header
    ssm_token_path = server.get("ssm_token_path")
    ssm_token_header = server.get("ssm_token_header")
    if ssm_token_path and ssm_token_header:
        try:
            ssm = boto3.client("ssm")
            param = ssm.get_parameter(Name=ssm_token_path, WithDecryption=True)
            headers[ssm_token_header] = param["Parameter"]["Value"]
        except Exception as e:
            logger.warning(f"[chat_handler] Failed to read SSM token {ssm_token_path}: {e}")

    try:
        # Step 1: Initialize session
        session_id = _mcp_initialize(server["url"], headers)
        if session_id:
            headers["Mcp-Session-Id"] = session_id

        # Step 2: Call tool
        data = _mcp_request(server["url"], headers, "tools/call", params={
            "name": original_name,
            "arguments": tool_input,
        }, request_id=1)

        if "error" in data:
            return f"Tool error: {data['error'].get('message', 'Unknown error')}"
        # Extract content from result
        result = data.get("result", {})
        content = result.get("content", [])
        text_parts = []
        for item in content:
            if isinstance(item, dict):
                if item.get("type") == "text":
                    text_parts.append(item.get("text", ""))
                else:
                    text_parts.append(json.dumps(item))
            else:
                text_parts.append(str(item))
        return "\n".join(text_parts) if text_parts else json.dumps(result)
    except urllib.error.URLError as e:
        raise Exception(f"Failed to connect to MCP server: {e}")
    except json.JSONDecodeError as e:
        raise Exception(f"Invalid JSON response from MCP server: {e}")


# ============================================================================
# Request/Response Models
# ============================================================================

class ChatMessageRequest(BaseModel):
    """Request model for chat messages."""
    conversation_id: Optional[str] = None
    message: str = Field(..., min_length=1, max_length=10000)
    top_k: int = Field(default=10, ge=1, le=50)
    advanced: bool = Field(default=False)  # Use Sonnet vs Haiku
    show_cost: bool = Field(default=False)  # Include token cost in response
    user: Optional[Dict] = None  # User identity from OIDC (email, name)
    workspace_id: Optional[str] = None  # Single workspace (deprecated, use workspace_ids)
    workspace_ids: Optional[List[str]] = None  # Multiple workspaces for cross-workspace search
    mcp_server_ids: Optional[List[str]] = None  # MCP servers for tool use
    prompt_workspace_id: Optional[str] = None  # Workspace whose custom prompt to use

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        return v.strip()


# Pricing per 1K tokens (configurable via env vars, values in USD per million tokens)
PRICING = {
    "haiku": {
        "input": float(os.environ.get("BEDROCK_BASIC_INPUT_PRICE", "0.80")) / 1000,
        "output": float(os.environ.get("BEDROCK_BASIC_OUTPUT_PRICE", "4.0")) / 1000,
    },
    "sonnet": {
        "input": float(os.environ.get("BEDROCK_ADVANCED_INPUT_PRICE", "3.0")) / 1000,
        "output": float(os.environ.get("BEDROCK_ADVANCED_OUTPUT_PRICE", "15.0")) / 1000,
    },
}


def _stream_error(error: str, status_code: int = 400) -> str:
    """Format error as NDJSON."""
    return json.dumps({"type": "error", "error": error, "status_code": status_code})


# ============================================================================
# Conversation Management (DynamoDB)
# ============================================================================

def generate_id() -> str:
    """Generate a unique ID."""
    return secrets.token_urlsafe(16)


def generate_title(message: str) -> str:
    """Generate a title from the first message."""
    # Use first 50 chars, truncate at word boundary
    title = message[:50]
    if len(message) > 50:
        last_space = title.rfind(" ")
        if last_space > 20:
            title = title[:last_space]
        title += "..."
    return title


def get_conversation(conversation_id: str) -> Optional[Dict]:
    """Get conversation metadata."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        response = table.get_item(
            Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"}
        )
        return response.get("Item")
    except ClientError as e:
        logger.error(f"Error getting conversation: {e}")
        return None


def create_conversation(title: str, user_id: str = None, workspace_id: str = None) -> Dict:
    """Create a new conversation."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    conversation_id = generate_id()
    now = datetime.now(timezone.utc).isoformat()

    item = {
        "PK": f"CONV#{conversation_id}",
        "SK": "METADATA",
        "conversation_id": conversation_id,
        "title": title,
        "message_count": 0,
        "memory_chunk_count": 0,
        "created_at": now,
        "updated_at": now,
    }
    if user_id:
        item["user_id"] = user_id
        item["GSI2PK"] = f"USER#{user_id}"
        item["GSI2SK"] = f"CONV#{now}"
    if workspace_id:
        item["workspace_id"] = workspace_id

    table.put_item(Item=item)
    logger.info(f"Created conversation: {conversation_id} (user: {user_id}, workspace: {workspace_id})")
    return item


def update_conversation(conversation_id: str, message_count: int):
    """Update conversation message count and timestamp, and sync share items."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    now = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"},
            UpdateExpression="SET message_count = :mc, updated_at = :ua, GSI2SK = :gsi2sk",
            ExpressionAttributeValues={
                ":mc": message_count,
                ":ua": now,
                ":gsi2sk": f"CONV#{now}",
            },
        )

        # Sync share items if conversation is shared
        _sync_share_items(conversation_id, message_count=message_count, updated_at=now)
    except ClientError as e:
        logger.error(f"Error updating conversation: {e}")


def get_conversation_messages(conversation_id: str, limit: int = 10) -> List[Dict]:
    """Get recent messages for a conversation."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"CONV#{conversation_id}",
                ":sk": "MSG#",
            },
            ScanIndexForward=False,  # Most recent first
            Limit=limit,
        )
        # Reverse to get chronological order
        messages = response.get("Items", [])
        messages.reverse()
        return messages
    except ClientError as e:
        logger.error(f"Error getting messages: {e}")
        return []


def save_message(conversation_id: str, role: str, content: str, sources: List[Dict] = None):
    """Save a message to the conversation."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    message_id = generate_id()
    now = datetime.now(timezone.utc).isoformat()

    item = {
        "PK": f"CONV#{conversation_id}",
        "SK": f"MSG#{now}#{message_id}",
        "message_id": message_id,
        "role": role,
        "content": content,
        "created_at": now,
    }
    if sources:
        item["sources"] = sources

    table.put_item(Item=item)
    return message_id


# ============================================================================
# Vector Search
# ============================================================================

def generate_embedding(text: str) -> List[float]:
    """Generate embedding using Bedrock Titan v2."""
    bedrock = _get_bedrock_client()

    try:
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

    except ClientError as e:
        logger.error(f"Error generating embedding: {e}")
        raise


def build_contextual_search_query(message: str, history: List[Dict]) -> str:
    """
    Build search query with conversation context for follow-up questions.
    """
    if not history:
        return message

    # Detect follow-up questions
    follow_up_indicators = [
        "what about", "tell me more", "why", "how", "when", "where",
        "can you", "could you", "explain", "clarify", "elaborate",
    ]

    is_likely_followup = (
        len(message) < 80 or
        any(indicator in message.lower() for indicator in follow_up_indicators)
    )

    if not is_likely_followup:
        return message

    # Get context from recent messages
    context_parts = []
    for msg in reversed(history):
        if msg.get("role") == "assistant":
            content = msg.get("content", "")[:500]
            if content:
                context_parts.append(f"Previous context: {content}")
            break

    for msg in reversed(history):
        if msg.get("role") == "user":
            content = msg.get("content", "")[:200]
            if content:
                context_parts.append(f"Original topic: {content}")
            break

    if not context_parts:
        return message

    augmented = " ".join(context_parts) + f" Current question: {message}"
    logger.info(f"Augmented search query: {len(message)} -> {len(augmented)} chars")
    return augmented


def search_kb(
    query: str,
    top_k: int = 10,
    workspace_id: str = None,
    workspace_ids: List[str] = None,
) -> List[Dict]:
    """Search KB documents in S3 Vectors.

    Args:
        query: Search query text
        top_k: Number of results to return
        workspace_id: Single workspace ID (deprecated, use workspace_ids)
        workspace_ids: List of workspace IDs for cross-workspace search

    When workspace filtering is provided, results are limited to documents
    that belong to the specified workspace(s).
    """
    s3vectors = _get_s3vectors_client()
    vector_bucket = _get_vector_bucket()
    vector_index = _get_vector_index()

    # Generate query embedding
    embedding = generate_embedding(query)
    if not embedding:
        logger.warning("Empty embedding generated")
        return []

    # Normalize workspace filter - prefer workspace_ids over workspace_id
    ws_filter_ids = workspace_ids or ([workspace_id] if workspace_id else None)

    try:
        # Build query params
        query_params = {
            "vectorBucketName": vector_bucket,
            "indexName": vector_index,
            "queryVector": {"float32": embedding},
            "topK": top_k,
            "returnMetadata": True,
            "returnDistance": True,
        }

        # Add workspace filter if provided
        # S3 Vectors uses $eq/$in operators (not equals/orAll)
        if ws_filter_ids:
            if len(ws_filter_ids) == 1:
                # Single workspace - use $eq filter
                query_params["filter"] = {"workspace_id": {"$eq": ws_filter_ids[0]}}
                logger.info(f"Filtering KB search by workspace: {ws_filter_ids[0]}")
            else:
                # Multiple workspaces - use $in filter
                query_params["filter"] = {"workspace_id": {"$in": ws_filter_ids}}
                logger.info(f"Filtering KB search by {len(ws_filter_ids)} workspaces: {ws_filter_ids}")

        response = s3vectors.query_vectors(**query_params)

        results = []
        for vector in response.get("vectors", []):
            metadata = vector.get("metadata", {})
            # Extract repo: prefer explicit field, fall back to source-specific logic
            repo = metadata.get("repo", "")
            if not repo:
                source_type = metadata.get("source", "")
                folder_path = metadata.get("folder_path", "")
                if source_type == "github":
                    repo = folder_path.split("/")[0] if folder_path else ""
                elif source_type and folder_path:
                    # e.g., "Confluence/OutcomeOps" from source=confluence, folder_path=confluence/OutcomeOps
                    space = folder_path.split("/")[-1] if "/" in folder_path else folder_path
                    repo = f"{source_type.capitalize()}/{space}"
                elif source_type:
                    repo = source_type.capitalize()

            results.append({
                "key": vector.get("key", ""),
                "score": vector.get("distance", 0),
                "content": metadata.get("content", ""),
                "repo": repo,
                "type": metadata.get("type", ""),
                "file_path": metadata.get("file_path", ""),
                "workspace_id": metadata.get("workspace_id", ""),
            })

        logger.info(f"Found {len(results)} KB results")
        return results

    except ClientError as e:
        logger.error(f"Error searching vectors: {e}")
        return []


def rerank_results(
    query: str,
    documents: List[Dict],
    top_n: Optional[int] = None,
    user_email: str = "",
    workspace_id: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> List[Dict]:
    """Rerank documents using Bedrock Rerank API (Cohere Rerank 3.5).

    Falls back to original order if reranking fails.

    Caller should pass user_email + workspace_id + source_ip + user_agent
    so the rerank audit row gets attributed correctly. Without these the
    row lands at PK=WS#SYSTEM and the analytics aggregator skips it,
    making rerank invisible in cost-by-workspace / cost-by-user views.
    """
    if not documents:
        return documents

    try:
        client = _get_bedrock_agent_runtime_client()

        sources = []
        for doc in documents:
            text = doc.get("content", "") or doc.get("text", "")
            sources.append({
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {
                        "text": text[:4096]
                    }
                }
            })

        rerank_start = time.time()

        rerank_params = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": sources,
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {
                        "modelArn": f"arn:aws:bedrock:{client.meta.region_name}::foundation-model/{RERANK_MODEL_ID}"
                    }
                }
            }
        }

        if top_n is not None:
            rerank_params["rerankingConfiguration"]["bedrockRerankingConfiguration"]["numberOfResults"] = top_n

        response = client.rerank(**rerank_params)

        rerank_duration_ms = int((time.time() - rerank_start) * 1000)
        logger.info(f"Reranked {len(documents)} documents in {rerank_duration_ms}ms")

        reranked = []
        for result in response.get("results", []):
            idx = result.get("index", 0)
            if idx < len(documents):
                doc = documents[idx].copy()
                doc["rerank_score"] = result.get("relevanceScore", 0)
                reranked.append(doc)

        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="chat",
            model_id=RERANK_MODEL_ID,
            input_text=f"rerank:{query}",
            output_text=f"reranked {len(reranked)} of {len(documents)} docs",
            duration_ms=rerank_duration_ms,
            user_email=user_email,
            workspace_id=workspace_id,
            source_ip=source_ip,
            user_agent=user_agent,
        )

        return reranked

    except ClientError as e:
        logger.warning(f"Reranking failed, falling back to cosine order: {e}")
        return documents[:top_n] if top_n else documents
    except Exception as e:
        logger.warning(f"Unexpected rerank error, falling back: {e}")
        return documents[:top_n] if top_n else documents


def format_context(results: List[Dict]) -> str:
    """Format search results as context for the prompt."""
    if not results:
        return "No relevant documents found."

    context_parts = []
    for i, r in enumerate(results, 1):
        source = r.get("repo", "unknown")
        doc_type = r.get("type", "document")
        max_doc_chars = int(os.environ.get("RAG_MAX_DOC_CHARS", "3000"))
        content = r.get("content", "")[:max_doc_chars]
        context_parts.append(f"[{i}] Source: {source} ({doc_type})\n{content}")

    return "\n\n---\n\n".join(context_parts)


# ============================================================================
# Conversation Memory
# ============================================================================

def summarize_messages_for_memory(messages: List[Dict]) -> str:
    """
    Summarize a batch of messages into a memory chunk text.
    Creates a condensed representation for embedding.
    """
    summary_parts = []

    for msg in messages:
        role = msg.get("role", "user")
        content = msg.get("content", "")

        # Truncate long messages
        if len(content) > 500:
            content = content[:500] + "..."

        if role == "user":
            summary_parts.append(f"User asked: {content}")
        else:
            summary_parts.append(f"Assistant answered: {content}")

    return "\n".join(summary_parts)


def create_conversation_memory_chunk(
    conversation_id: str,
    chunk_number: int,
    messages: List[Dict],
) -> bool:
    """
    Create a memory chunk from messages and store in S3 Vectors.
    Returns True on success.
    """
    s3vectors = _get_s3vectors_client()
    vector_bucket = _get_vector_bucket()
    vector_index = _get_vector_index()

    # Create summary text
    summary_text = summarize_messages_for_memory(messages)
    if not summary_text:
        logger.warning(f"Empty summary for conversation {conversation_id} chunk {chunk_number}")
        return False

    # Generate embedding
    try:
        embedding = generate_embedding(summary_text)
    except Exception as e:
        logger.error(f"Failed to generate memory embedding: {e}")
        return False

    if not embedding:
        return False

    # Store in S3 Vectors with memory-specific key
    key = f"memory#{conversation_id}#{chunk_number:04d}"

    try:
        s3vectors.put_vectors(
            vectorBucketName=vector_bucket,
            indexName=vector_index,
            vectors=[{
                "key": key,
                "data": {"float32": embedding},
                "metadata": {
                    "type": "conversation_memory",
                    "conversation_id": conversation_id,
                    "chunk_number": chunk_number,
                    "content": summary_text[:5000],  # Store summary for retrieval
                    "message_count": len(messages),
                },
            }],
        )

        logger.info(f"Created memory chunk: {key}")
        return True

    except ClientError as e:
        logger.error(f"Failed to store memory chunk: {e}")
        return False


def search_conversation_memory(
    conversation_id: str,
    query: str,
    top_k: int = MEMORY_TOP_K,
) -> List[Dict]:
    """
    Search for relevant memory chunks in this conversation.
    """
    s3vectors = _get_s3vectors_client()
    vector_bucket = _get_vector_bucket()
    vector_index = _get_vector_index()

    # Generate query embedding
    try:
        embedding = generate_embedding(query)
    except Exception as e:
        logger.error(f"Failed to generate query embedding for memory: {e}")
        return []

    if not embedding:
        return []

    try:
        response = s3vectors.query_vectors(
            vectorBucketName=vector_bucket,
            indexName=vector_index,
            queryVector={"float32": embedding},
            topK=top_k * 3,  # Fetch extra to filter by conversation
            returnMetadata=True,
            returnDistance=True,
            # S3 Vectors uses $and/$eq operators (not andAll/equals)
            filter={
                "$and": [
                    {"type": {"$eq": "conversation_memory"}},
                    {"conversation_id": {"$eq": conversation_id}},
                ]
            },
        )

        results = []
        for vector in response.get("vectors", []):
            metadata = vector.get("metadata", {})
            results.append({
                "key": vector.get("key", ""),
                "score": vector.get("distance", 0),
                "content": metadata.get("content", ""),
                "chunk_number": metadata.get("chunk_number", 0),
            })

        # Limit to top_k
        results = results[:top_k]
        logger.info(f"Found {len(results)} memory chunks for conversation {conversation_id}")
        return results

    except ClientError as e:
        logger.error(f"Error searching memory: {e}")
        return []


def delete_conversation_memory(conversation_id: str) -> bool:
    """
    Delete all memory chunks for a conversation.
    """
    s3vectors = _get_s3vectors_client()
    vector_bucket = _get_vector_bucket()
    vector_index = _get_vector_index()

    try:
        # Query to find all memory chunks for this conversation
        # Use listVectors with prefix filter
        response = s3vectors.list_vectors(
            vectorBucketName=vector_bucket,
            indexName=vector_index,
            segmentCount=1,
            segmentIndex=0,
        )

        # Find memory keys for this conversation
        keys_to_delete = []
        for vector in response.get("vectors", []):
            key = vector.get("key", "")
            if key.startswith(f"memory#{conversation_id}#"):
                keys_to_delete.append(key)

        if not keys_to_delete:
            logger.info(f"No memory chunks to delete for conversation {conversation_id}")
            return True

        # Delete the vectors
        s3vectors.delete_vectors(
            vectorBucketName=vector_bucket,
            indexName=vector_index,
            keys=keys_to_delete,
        )

        logger.info(f"Deleted {len(keys_to_delete)} memory chunks for conversation {conversation_id}")
        return True

    except ClientError as e:
        logger.error(f"Error deleting memory: {e}")
        return False


def format_memory_context(memory_results: List[Dict]) -> str:
    """Format memory chunks as context for the prompt."""
    if not memory_results:
        return ""

    # Sort by chunk number to maintain chronological order
    sorted_results = sorted(memory_results, key=lambda x: x.get("chunk_number", 0))

    memory_parts = []
    for r in sorted_results:
        content = r.get("content", "")
        if content:
            memory_parts.append(content)

    if not memory_parts:
        return ""

    return "CONVERSATION HISTORY SUMMARY:\n" + "\n---\n".join(memory_parts)


def update_memory_chunk_count(conversation_id: str, chunk_count: int):
    """Update the memory chunk count for a conversation."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    now = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"},
            UpdateExpression="SET memory_chunk_count = :mcc, updated_at = :ua, GSI2SK = :gsi2sk",
            ExpressionAttributeValues={
                ":mcc": chunk_count,
                ":ua": now,
                ":gsi2sk": f"CONV#{now}",
            },
        )
    except ClientError as e:
        logger.error(f"Error updating memory chunk count: {e}")


# ============================================================================
# Answer Generation (Streaming)
# ============================================================================

def _get_workspace_custom_prompt(workspace_id: str) -> Optional[str]:
    """Fetch custom system prompt for a workspace, if set."""
    if not workspace_id:
        return None
    try:
        dynamodb = _get_dynamodb()
        table = dynamodb.Table(_get_workspaces_table())
        response = table.get_item(
            Key={"PK": f"WS#{workspace_id}", "SK": "METADATA"},
            ProjectionExpression="custom_system_prompt",
        )
        return response.get("Item", {}).get("custom_system_prompt")
    except ClientError:
        return None


def generate_answer_stream(
    question: str,
    context: str,
    history: List[Dict],
    memory_context: str = "",
    advanced: bool = False,
    tool_config: Dict = None,
    tool_mapping: Dict = None,
    workspace_prompt: str = None,
) -> Generator:
    """
    Stream answer from Bedrock using ConverseStream.
    Yields text chunks, tool_use dicts, and final usage dict.
    When tool_config is provided, implements a tool use loop.
    """
    bedrock = _get_bedrock_client()

    # Build message history
    messages = []
    for msg in history[-10:]:
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if content:
            messages.append({"role": role, "content": [{"text": content}]})

    # Build the prompt with optional memory context
    memory_section = ""
    if memory_context:
        memory_section = f"\n{memory_context}\n\n---\n\n"

    user_prompt = f"""The following code maps and documents are extracted directly from source code. They contain exact implementations. Use them as fact.
{memory_section}
KNOWLEDGE BASE CONTEXT:
{context}

USER QUESTION:
{question}

Remember: code maps ARE the source code. Quote exact method signatures and trace complete call chains. Never hedge."""

    messages.append({"role": "user", "content": [{"text": user_prompt}]})

    model_id = ADVANCED_MODEL_ID if advanced else BASIC_MODEL_ID
    max_iterations = 10
    total_input_tokens = 0
    total_output_tokens = 0

    system_prompt = build_chat_system_prompt(workspace_prompt=workspace_prompt)

    try:
        for iteration in range(max_iterations):
            params = {
                "modelId": model_id,
                "messages": messages,
                "system": [{"text": system_prompt}],
                "inferenceConfig": {
                    "maxTokens": 8192,
                    "temperature": 0.3,
                },
            }
            if tool_config:
                params["toolConfig"] = tool_config

            response = bedrock.converse_stream(**params)

            # Process stream
            assistant_text_parts = []
            current_tool_use = None
            tool_use_blocks = []

            for event in response.get("stream", []):
                if "contentBlockStart" in event:
                    start = event["contentBlockStart"].get("start", {})
                    if "toolUse" in start:
                        current_tool_use = {
                            "toolUseId": start["toolUse"]["toolUseId"],
                            "name": start["toolUse"]["name"],
                            "input_parts": [],
                        }
                        # Yield progress event
                        if tool_mapping and start["toolUse"]["name"] in tool_mapping:
                            original_name = tool_mapping[start["toolUse"]["name"]]["original_tool_name"]
                            yield {"type": "tool_use", "tool": original_name, "status": "calling"}

                elif "contentBlockDelta" in event:
                    delta = event["contentBlockDelta"].get("delta", {})
                    if "text" in delta:
                        yield delta["text"]
                        assistant_text_parts.append(delta["text"])
                    elif "toolUse" in delta:
                        if current_tool_use:
                            current_tool_use["input_parts"].append(delta["toolUse"]["input"])

                elif "contentBlockStop" in event:
                    if current_tool_use:
                        full_input = "".join(current_tool_use["input_parts"])
                        try:
                            tool_input = json.loads(full_input) if full_input else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        tool_use_blocks.append({
                            "toolUseId": current_tool_use["toolUseId"],
                            "name": current_tool_use["name"],
                            "input": tool_input,
                        })
                        current_tool_use = None

                elif "metadata" in event:
                    usage_data = event["metadata"].get("usage", {})
                    total_input_tokens += usage_data.get("inputTokens", 0)
                    total_output_tokens += usage_data.get("outputTokens", 0)

            # If no tool uses, we're done
            if not tool_use_blocks:
                break

            # Build assistant message content
            assistant_content = []
            if assistant_text_parts:
                assistant_content.append({"text": "".join(assistant_text_parts)})
            for tu in tool_use_blocks:
                assistant_content.append({
                    "toolUse": {
                        "toolUseId": tu["toolUseId"],
                        "name": tu["name"],
                        "input": tu["input"],
                    }
                })

            messages.append({"role": "assistant", "content": assistant_content})

            # Execute tool calls
            tool_results = []
            for tu in tool_use_blocks:
                mapping_entry = tool_mapping.get(tu["name"]) if tool_mapping else None
                if mapping_entry:
                    try:
                        result = _execute_mcp_tool_call(mapping_entry, tu["input"])
                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tu["toolUseId"],
                                "content": [{"text": result}],
                            }
                        })
                    except Exception as e:
                        logger.error(f"[chat_handler] MCP tool execution error: {e}", exc_info=True)
                        tool_results.append({
                            "toolResult": {
                                "toolUseId": tu["toolUseId"],
                                "content": [{"text": f"Error: {str(e)}"}],
                                "status": "error",
                            }
                        })
                else:
                    tool_results.append({
                        "toolResult": {
                            "toolUseId": tu["toolUseId"],
                            "content": [{"text": "Error: Unknown tool"}],
                            "status": "error",
                        }
                    })

            messages.append({"role": "user", "content": tool_results})
            # Loop continues for next Bedrock call

        # Yield final usage
        yield {"usage": {"inputTokens": total_input_tokens, "outputTokens": total_output_tokens}}

    except ClientError as e:
        logger.error(f"Error generating answer: {e}")
        raise


# ============================================================================
# Main Streaming Handler
# ============================================================================

def handle_chat_stream(event: Dict) -> Generator[str, None, None]:
    """
    Generator for streaming chat responses.
    Yields NDJSON strings for each event.
    """
    # Parse request
    try:
        body = json.loads(event.get("body") or "{}")
        request = ChatMessageRequest(**body)
    except (json.JSONDecodeError, ValidationError) as e:
        yield _stream_error(f"Invalid request: {e}") + "\n"
        return

    # Extract user identity (set by Fargate proxy from OIDC headers)
    user_id = None
    if request.user:
        user_id = request.user.get("email")

    # Pull source_ip / user_agent off the synthesized requestContext so
    # bedrock audit rows can record the browser IP. main.py:_extract_browser_ctx
    # already populated these from x-real-source-ip / x-real-user-agent
    # (with x-forwarded-for fallback).
    _rc = event.get("requestContext", {}) or {}
    _http = _rc.get("http", {}) or {}
    source_ip = _http.get("sourceIp", "") or ""
    user_agent = _http.get("userAgent", "") or ""

    # Get or create conversation
    conversation = None
    # Support both workspace_ids (new) and workspace_id (deprecated)
    workspace_ids = request.workspace_ids or ([request.workspace_id] if request.workspace_id else None)

    # Validate workspace access - only allow workspaces the user is a member of
    if workspace_ids and user_id:
        authorized_ws = _validate_workspace_access(user_id, workspace_ids)
        if not authorized_ws:
            yield _stream_error("You do not have access to the requested workspace(s)", 403) + "\n"
            return
        workspace_ids = authorized_ws

    # For conversation storage, use first workspace_id
    primary_workspace_id = workspace_ids[0] if workspace_ids else None

    # Use explicitly selected prompt workspace, or fall back to primary
    prompt_ws_id = request.prompt_workspace_id or primary_workspace_id
    workspace_prompt = _get_workspace_custom_prompt(prompt_ws_id)
    mcp_server_ids = request.mcp_server_ids

    if request.conversation_id:
        conversation = get_conversation(request.conversation_id)
        if not conversation:
            yield _stream_error("Conversation not found", 404) + "\n"
            return
        # Only the conversation owner can send messages
        if user_id and conversation.get("user_id") and conversation.get("user_id") != user_id:
            yield _stream_error("Read-only: shared conversations cannot be modified", 403) + "\n"
            return
        # Use workspace_id from existing conversation if not provided in request
        if not workspace_ids:
            conv_ws = conversation.get("workspace_id")
            if conv_ws:
                workspace_ids = [conv_ws]
    else:
        title = generate_title(request.message)
        conversation = create_conversation(title, user_id=user_id, workspace_id=primary_workspace_id)

    conversation_id = conversation["conversation_id"]
    message_count = int(conversation.get("message_count", 0))
    memory_chunk_count = int(conversation.get("memory_chunk_count", 0))

    # Get conversation history BEFORE saving current message
    history = get_conversation_messages(conversation_id, limit=10)

    # Save user message
    save_message(conversation_id, "user", request.message)
    message_count += 1

    # Check if we need to create a memory chunk
    # Create chunk when we have MEMORY_CHUNK_SIZE messages since last chunk
    messages_since_last_chunk = message_count - (memory_chunk_count * MEMORY_CHUNK_SIZE)
    if messages_since_last_chunk >= MEMORY_CHUNK_SIZE:
        # Get the messages to summarize (the last MEMORY_CHUNK_SIZE messages before current)
        chunk_messages = get_conversation_messages(conversation_id, limit=MEMORY_CHUNK_SIZE + 1)
        # Exclude the message we just saved
        chunk_messages = chunk_messages[:-1] if chunk_messages else []

        if len(chunk_messages) >= MEMORY_CHUNK_SIZE:
            if create_conversation_memory_chunk(
                conversation_id,
                memory_chunk_count + 1,
                chunk_messages[:MEMORY_CHUNK_SIZE],
            ):
                memory_chunk_count += 1
                update_memory_chunk_count(conversation_id, memory_chunk_count)
                logger.info(f"Created memory chunk {memory_chunk_count} for {conversation_id}")

    # Search for relevant memory context if we have memory chunks
    memory_context = ""
    if memory_chunk_count > 0:
        memory_results = search_conversation_memory(
            conversation_id,
            request.message,
            top_k=MEMORY_TOP_K,
        )
        memory_context = format_memory_context(memory_results)
        if memory_context:
            logger.info(f"Found {len(memory_results)} relevant memory chunks")

    # Load MCP tool configs if requested
    tool_config = None
    tool_mapping = None
    if mcp_server_ids:
        try:
            mcp_configs = _load_mcp_configs(mcp_server_ids)
            if mcp_configs:
                tool_config, tool_mapping = _build_bedrock_tool_config(mcp_configs)
                logger.info(f"[chat_handler] Loaded {len(mcp_configs)} MCP servers with {len(tool_mapping or {})} tools")
        except Exception as e:
            logger.error(f"[chat_handler] Failed to load MCP configs: {e}", exc_info=True)

    # QA test trigger: input "1234567890" forces a refusal without calling Bedrock
    if request.message.strip() == "1234567890":
        test_answer = "I'm sorry, I can't assist with that request. [TEST_REFUSAL]"
        logger.warning(f"[chat] Test refusal triggered by user {user_id}")
        metadata = {
            "type": "metadata",
            "conversation_id": conversation_id,
            "sources": [],
            "title": conversation.get("title", "Untitled"),
        }
        yield json.dumps(metadata) + "\n"
        yield json.dumps({"type": "token", "text": test_answer}) + "\n"
        # model_id is set to the basic model so the row looks like a real
        # refusal in cost-by-model analytics (zero tokens/cost still get
        # attributed, so this doesn't inflate haiku spend). flagged_reason
        # still says test_refusal_trigger so QA tooling can distinguish.
        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="chat",
            model_id=BASIC_MODEL_ID,
            input_text=request.message,
            output_text=test_answer,
            status="refusal",
            flagged_reason="test_refusal_trigger",
            user_email=user_id or "",
            workspace_id=primary_workspace_id or "",
            source_ip=source_ip,
            user_agent=user_agent,
        )
        message_id = save_message(conversation_id, "assistant", test_answer, [])
        message_count += 1
        update_conversation(conversation_id, message_count)
        yield json.dumps({"type": "done", "message_id": message_id}) + "\n"
        return

    # Build contextual search query
    search_query = build_contextual_search_query(request.message, history)

    # Search KB (skip only in MCP-only mode when no workspaces selected)
    if mcp_server_ids and not workspace_ids:
        search_results = []
    else:
        fetch_k = min(request.top_k * 4, RERANK_MAX_FETCH) if RERANK_ENABLED else request.top_k
        search_results = search_kb(search_query, top_k=fetch_k, workspace_ids=workspace_ids)

        # Rerank results if enabled. Thread the request context so the
        # rerank audit row is attributed to the workspace + user that
        # caused it - otherwise it lands on WS#SYSTEM and the analytics
        # aggregator skips it.
        if RERANK_ENABLED and len(search_results) > 1:
            search_results = rerank_results(
                search_query,
                search_results,
                top_n=request.top_k,
                user_email=user_id or "",
                workspace_id=primary_workspace_id or "",
                source_ip=source_ip,
                user_agent=user_agent,
            )
            # Log score distribution for threshold tuning (print for LWA/CloudWatch visibility)
            has_rerank_scores = any("rerank_score" in r for r in search_results)
            if has_rerank_scores:
                scores = sorted([r.get("rerank_score", 0) for r in search_results], reverse=True)
                print(f"[RERANK] Scores: max={scores[0]:.3f} min={scores[-1]:.3f} median={scores[len(scores)//2]:.3f} ({len(scores)} docs)")
                print(f"[RERANK] All scores: {[round(s, 3) for s in scores]}")
            # Filter out low-relevance results if min score threshold is set
            if RERANK_MIN_SCORE > 0 and has_rerank_scores:
                before_count = len(search_results)
                search_results = [r for r in search_results if r.get("rerank_score", 0) >= RERANK_MIN_SCORE]
                print(f"[RERANK] Min score filter ({RERANK_MIN_SCORE}): {before_count} -> {len(search_results)} docs")
            print(f"[RERANK] Final: {len(search_results)} docs (requested {request.top_k})")

    # Format context
    context = format_context(search_results)

    # Build sources (deduplicated)
    sources = []
    seen_paths = set()
    for r in search_results:
        path = r.get("file_path") or r.get("key", "")
        if path and path not in seen_paths:
            seen_paths.add(path)
            sources.append({
                "repo": r.get("repo", ""),
                "type": r.get("type", ""),
                "path": path,
            })

    # Get title
    title = conversation.get("title", "Untitled")

    # Send metadata first
    metadata = {
        "type": "metadata",
        "conversation_id": conversation_id,
        "sources": sources,
        "title": title,
    }
    yield json.dumps(metadata) + "\n"

    # Stream the answer
    answer_chunks = []
    usage = None
    bedrock_start = time.time()
    try:
        for chunk in generate_answer_stream(
            request.message,
            context,
            history,
            memory_context,
            request.advanced,
            tool_config=tool_config,
            tool_mapping=tool_mapping,
            workspace_prompt=workspace_prompt,
        ):
            if isinstance(chunk, dict):
                if chunk.get("type") == "tool_use":
                    yield json.dumps(chunk) + "\n"
                elif "usage" in chunk:
                    usage = chunk["usage"]
                    logger.info(f"Token usage: {usage}")
            else:
                answer_chunks.append(chunk)
                yield json.dumps({"type": "token", "text": chunk}) + "\n"
    except ClientError as e:
        logger.error(f"Failed to generate answer: {e}")
        yield _stream_error("Failed to generate response", 500) + "\n"
        return
    bedrock_duration_ms = int((time.time() - bedrock_start) * 1000)

    answer = "".join(answer_chunks)

    # Audit log the Bedrock call
    audit_model = ADVANCED_MODEL_ID if request.advanced else BASIC_MODEL_ID
    audit_input_tokens = usage.get("inputTokens", 0) if usage else 0
    audit_output_tokens = usage.get("outputTokens", 0) if usage else 0
    log_audit_entry(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        lambda_name="chat",
        model_id=audit_model,
        input_text=request.message,
        output_text=answer,
        input_tokens=audit_input_tokens,
        output_tokens=audit_output_tokens,
        duration_ms=bedrock_duration_ms,
        source_ip=source_ip,
        user_agent=user_agent,
        user_email=user_id or "",
        workspace_id=primary_workspace_id or "",
        workspace_ids=workspace_ids,
        sources=sources,
    )

    # Save assistant message
    message_id = save_message(conversation_id, "assistant", answer, sources)
    message_count += 1

    # Update conversation
    update_conversation(conversation_id, message_count)

    # Build done event
    done_event = {"type": "done", "message_id": message_id}

    # Include cost info if requested
    if request.show_cost and usage:
        pricing = PRICING["sonnet" if request.advanced else "haiku"]
        input_tokens = usage.get("inputTokens", 0)
        output_tokens = usage.get("outputTokens", 0)
        cost = (input_tokens * pricing["input"] / 1000) + (output_tokens * pricing["output"] / 1000)
        done_event["cost"] = {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost": round(cost, 6),
        }

    yield json.dumps(done_event) + "\n"


# ============================================================================
# REST API Handler (for conversation management)
# ============================================================================

def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    REST API handler for conversation management.
    Routes:
        GET /conversations - List conversations
        GET /conversations/{id} - Get conversation with messages (+ shared access fallback)
        DELETE /conversations/{id} - Delete conversation
        PUT /conversations/{id}/share - Set sharing (non-empty workspace_ids = share, empty = unshare)
        GET /shared - List shared conversations
    """
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    raw_path = event.get("path") or event.get("requestContext", {}).get("http", {}).get("path", "/")
    # Strip query string from path (Function URL may include it)
    path = raw_path.split("?")[0]

    # Extract user identity from header (set by Fargate proxy)
    headers = event.get("headers") or {}
    user_id = headers.get("x-user-email")

    logger.info(f"REST API: {http_method} {path} (user: {user_id})")

    # Parse query string parameters
    query_params = event.get("queryStringParameters") or {}

    # Parse body for POST/PUT/DELETE with body
    body = {}
    raw_body = event.get("body")
    if raw_body:
        try:
            body = json.loads(raw_body) if isinstance(raw_body, str) else raw_body
        except (json.JSONDecodeError, TypeError):
            body = {}

    # Route handling
    # Share routes must be checked BEFORE generic /conversations/{id} routes
    if path == "/conversations" and http_method == "GET":
        limit = min(int(query_params.get("limit", "20")), 100)
        cursor = query_params.get("cursor")
        return list_conversations(user_id=user_id, limit=limit, cursor=cursor)
    elif path.endswith("/share") and path.startswith("/conversations/") and http_method == "PUT":
        conv_id = path.split("/")[-2]
        return handle_share_conversation(conv_id, user_id=user_id, body=body)
    elif path == "/shared" and http_method == "GET":
        return list_shared_conversations(user_id=user_id, query_params=query_params)
    elif path.startswith("/conversations/") and not path.endswith("/share") and http_method == "GET":
        conv_id = path.split("/")[-1]
        return get_conversation_with_messages(conv_id, user_id=user_id)
    elif path.startswith("/conversations/") and not path.endswith("/share") and http_method == "DELETE":
        conv_id = path.split("/")[-1]
        return delete_conversation(conv_id, user_id=user_id)
    else:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Not found"}),
        }


# ============================================================================
# Conversation Sharing
# ============================================================================


def _sync_share_items(
    conversation_id: str,
    title: str = None,
    message_count: int = None,
    updated_at: str = None,
):
    """Sync share items when conversation metadata changes.

    This is best-effort - failures are logged but do not propagate.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        # Find all SHARE# items for this conversation
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"CONV#{conversation_id}",
                ":sk": "SHARE#",
            },
        )

        share_items = response.get("Items", [])
        if not share_items:
            return

        # Build update expression dynamically
        update_parts = []
        expr_values = {}

        if updated_at:
            update_parts.append("updated_at = :ua")
            expr_values[":ua"] = updated_at
        if title is not None:
            update_parts.append("title = :t")
            expr_values[":t"] = title
        if message_count is not None:
            update_parts.append("message_count = :mc")
            expr_values[":mc"] = message_count

        if not update_parts:
            return

        # Also update GSI3SK for sort order
        if updated_at:
            update_parts.append("GSI3SK = :gsi3sk")
            expr_values[":gsi3sk"] = f"CONV#{updated_at}"

        update_expr = "SET " + ", ".join(update_parts)

        for item in share_items:
            try:
                table.update_item(
                    Key={"PK": item["PK"], "SK": item["SK"]},
                    UpdateExpression=update_expr,
                    ExpressionAttributeValues=expr_values,
                )
            except ClientError as e:
                logger.error(
                    f"Error syncing share item {item['SK']}: {e}"
                )
    except Exception as e:
        # Best-effort sync - log and continue
        logger.debug(f"Share item sync skipped for {conversation_id}: {e}")


def handle_share_conversation(
    conversation_id: str, user_id: str, body: Dict
) -> Dict:
    """Set sharing for a conversation (PUT semantics - replaces current sharing).

    - Non-empty workspace_ids: share with those workspaces (replaces, not additive)
    - Empty workspace_ids list: unshare all
    """
    workspace_ids = body.get("workspace_ids", [])

    # Get conversation metadata
    conversation = get_conversation(conversation_id)
    if not conversation:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Conversation not found"}),
        }

    # Verify ownership
    if conversation.get("user_id") != user_id:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Conversation not found"}),
        }

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())
    existing_shared = set(conversation.get("shared_workspace_ids", []))

    try:
        if not workspace_ids:
            # --- Unshare all ---
            # Delete all existing SHARE items
            with table.batch_writer() as batch:
                for ws_id in existing_shared:
                    batch.delete_item(
                        Key={
                            "PK": f"CONV#{conversation_id}",
                            "SK": f"SHARE#{ws_id}",
                        }
                    )

            # Remove shared_workspace_ids and shared_at from METADATA
            table.update_item(
                Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"},
                UpdateExpression="REMOVE shared_workspace_ids, shared_at",
            )

            logger.info(f"Unshared conversation {conversation_id} from all workspaces")

            return {
                "statusCode": 200,
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps({
                    "shared_workspace_ids": [],
                    "share_url": f"/chat/{conversation_id}",
                }),
            }

        # --- Share with specified workspaces (replace) ---
        authorized_ws = _validate_workspace_access(user_id, workspace_ids)
        if not authorized_ws:
            return {
                "statusCode": 403,
                "body": json.dumps({"error": "No access to the specified workspaces"}),
            }

        now = datetime.now(timezone.utc).isoformat()
        updated_at = conversation.get("updated_at", now)

        # Delete old SHARE items that are no longer in the new set
        to_remove = existing_shared - set(authorized_ws)
        if to_remove:
            with table.batch_writer() as batch:
                for ws_id in to_remove:
                    batch.delete_item(
                        Key={
                            "PK": f"CONV#{conversation_id}",
                            "SK": f"SHARE#{ws_id}",
                        }
                    )

        # Write SHARE items for each authorized workspace
        with table.batch_writer() as batch:
            for ws_id in authorized_ws:
                share_item = {
                    "PK": f"CONV#{conversation_id}",
                    "SK": f"SHARE#{ws_id}",
                    "GSI3PK": f"WS_SHARE#{ws_id}",
                    "GSI3SK": f"CONV#{updated_at}",
                    "conversation_id": conversation_id,
                    "title": conversation.get("title", ""),
                    "message_count": conversation.get("message_count", 0),
                    "user_id": user_id,
                    "updated_at": updated_at,
                    "shared_at": now,
                }
                batch.put_item(Item=share_item)

        # Update METADATA: replace shared_workspace_ids, preserve original shared_at
        existing_shared_at = conversation.get("shared_at")
        update_expr = "SET shared_workspace_ids = :sws"
        expr_values = {":sws": authorized_ws}
        if not existing_shared_at:
            update_expr += ", shared_at = :sat"
            expr_values[":sat"] = now

        table.update_item(
            Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )

        logger.info(
            "Shared conversation %s with workspaces: %s", conversation_id, authorized_ws
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({
                "shared_workspace_ids": sorted(authorized_ws),
                "share_url": f"/chat/{conversation_id}",
            }),
        }
    except ClientError as e:
        logger.error(f"Error updating share for conversation: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to update conversation sharing"}),
        }
    except Exception as e:
        logger.error(f"Unexpected error in share_conversation: {e}", exc_info=True)
        return {
            "statusCode": 500,
            "body": json.dumps({"error": f"Internal error: {str(e)}"}),
        }


def list_shared_conversations(
    user_id: str, query_params: Dict
) -> Dict:
    """List shared conversations visible to the user."""
    import base64

    workspace_id = query_params.get("workspace_id")
    limit = min(int(query_params.get("limit", "20")), 100)
    cursor = query_params.get("cursor")

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        # Determine which workspaces to query
        if workspace_id:
            # Validate user has access to this workspace
            authorized = _validate_workspace_access(user_id, [workspace_id])
            if not authorized:
                return {
                    "statusCode": 403,
                    "body": json.dumps({"error": "Access denied to workspace"}),
                }
            workspace_ids_to_query = [workspace_id]
        else:
            # Get all workspaces the user is a member of
            workspace_ids_to_query = _get_user_workspace_ids(user_id)
            if not workspace_ids_to_query:
                return {
                    "statusCode": 200,
                    "headers": {"Content-Type": "application/json"},
                    "body": json.dumps({
                        "conversations": [],
                        "has_more": False,
                    }),
                }

        # Query GSI3 for each workspace
        all_conversations = []
        for ws_id in workspace_ids_to_query:
            try:
                gsi_query = {
                    "IndexName": "GSI3",
                    "KeyConditionExpression": "GSI3PK = :pk",
                    "ExpressionAttributeValues": {
                        ":pk": f"WS_SHARE#{ws_id}"
                    },
                    "ScanIndexForward": False,
                    "Limit": limit,
                }
                response = table.query(**gsi_query)
                all_conversations.extend(response.get("Items", []))
            except ClientError as gsi_err:
                error_code = gsi_err.response.get("Error", {}).get("Code", "")
                if error_code in ("ValidationException", "AccessDeniedException"):
                    logger.warning("GSI3 not available, skipping workspace query")
                    continue
                raise

        # Deduplicate by conversation_id and exclude conversations owned by the viewer
        seen = set()
        unique_conversations = []
        for conv in all_conversations:
            cid = conv.get("conversation_id")
            if cid and cid not in seen and conv.get("user_id") != user_id:
                seen.add(cid)
                unique_conversations.append(conv)

        # Sort by updated_at descending
        unique_conversations.sort(
            key=lambda x: x.get("updated_at", ""), reverse=True
        )

        # Apply pagination
        start = 0
        if cursor:
            try:
                cursor_data = json.loads(base64.b64decode(cursor).decode())
                cursor_updated_at = cursor_data.get("updated_at", "")
                # Find start position after cursor
                for i, conv in enumerate(unique_conversations):
                    if conv.get("updated_at", "") < cursor_updated_at:
                        start = i
                        break
                else:
                    start = len(unique_conversations)
            except Exception:
                return {
                    "statusCode": 400,
                    "body": json.dumps({"error": "Invalid cursor"}),
                }

        page = unique_conversations[start : start + limit]
        has_more = (start + limit) < len(unique_conversations)

        result = {"conversations": page, "has_more": has_more}

        if has_more and page:
            last_item = page[-1]
            next_cursor_data = {"updated_at": last_item.get("updated_at", "")}
            result["next_cursor"] = base64.b64encode(
                json.dumps(next_cursor_data).encode()
            ).decode()

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result, default=str),
        }
    except ClientError as e:
        logger.error(f"Error listing shared conversations: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to list shared conversations"}),
        }


def _get_user_workspace_ids(user_id: str) -> List[str]:
    """Get all workspace IDs the user is a member of."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_workspaces_table())

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"USER#{user_id}",
                ":sk": "WS_MEMBER#",
            },
        )
        workspace_ids = []
        for item in response.get("Items", []):
            # SK format is WS_MEMBER#{workspace_id}
            sk = item.get("SK", "")
            if sk.startswith("WS_MEMBER#"):
                ws_id = sk[len("WS_MEMBER#"):]
                if ws_id:
                    workspace_ids.append(ws_id)
        return workspace_ids
    except ClientError as e:
        logger.error(f"Error getting user workspace IDs: {e}")
        return []


def list_conversations(user_id: str = None, limit: int = 20, cursor: str = None) -> Dict:
    """List conversations for a user, paginated, most recent first."""
    import base64

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        if user_id:
            if cursor:
                try:
                    exclusive_start_key = json.loads(
                        base64.b64decode(cursor).decode()
                    )
                except Exception:
                    return {
                        "statusCode": 400,
                        "body": json.dumps({"error": "Invalid cursor"}),
                    }

            # Try GSI2 query first, fall back to scan if index doesn't exist yet
            try:
                query_params = {
                    "IndexName": "GSI2",
                    "KeyConditionExpression": "GSI2PK = :pk",
                    "ExpressionAttributeValues": {":pk": f"USER#{user_id}"},
                    "ScanIndexForward": False,
                    "Limit": limit,
                }
                if cursor:
                    query_params["ExclusiveStartKey"] = exclusive_start_key

                response = table.query(**query_params)
            except ClientError as gsi_err:
                error_code = gsi_err.response.get("Error", {}).get("Code", "")
                if error_code in ("ValidationException", "AccessDeniedException"):
                    # GSI2 doesn't exist yet — fall back to scan
                    logger.warning("GSI2 not available, falling back to scan")
                    response = table.scan(
                        FilterExpression="SK = :sk AND user_id = :uid",
                        ExpressionAttributeValues={":sk": "METADATA", ":uid": user_id},
                    )
                    # Sort in Python since scan doesn't guarantee order
                    response["Items"] = sorted(
                        response.get("Items", []),
                        key=lambda x: x.get("updated_at", ""),
                        reverse=True,
                    )[:limit]
                else:
                    raise
        else:
            # Fallback to scan when no user_id (admin use case)
            response = table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "METADATA"},
            )

        conversations = response.get("Items", [])

        result = {"conversations": conversations}

        last_key = response.get("LastEvaluatedKey")
        if last_key:
            result["next_cursor"] = base64.b64encode(
                json.dumps(last_key, default=str).encode()
            ).decode()
        result["has_more"] = last_key is not None

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps(result, default=str),
        }
    except ClientError as e:
        logger.error(f"Error listing conversations: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to list conversations"}),
        }


def get_conversation_with_messages(conversation_id: str, user_id: str = None) -> Dict:
    """Get conversation with messages (supports owner access and shared access fallback)."""
    conversation = get_conversation(conversation_id)
    if not conversation:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Conversation not found"}),
        }

    # user_id=None means internal call (no auth) - treat as owner for backward compat
    is_owner = (user_id is None) or (conversation.get("user_id") == user_id)

    if not is_owner:
        # Check if shared with any workspace the viewer is a member of
        shared_workspace_ids = conversation.get("shared_workspace_ids", [])
        if not shared_workspace_ids:
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "Conversation not found"}),
            }

        authorized_ws = _validate_workspace_access(user_id, shared_workspace_ids)
        if not authorized_ws:
            return {
                "statusCode": 404,
                "body": json.dumps({"error": "Conversation not found"}),
            }

    messages = get_conversation_messages(
        conversation_id, limit=30 if is_owner else 100
    )

    # Build response with is_owner flag
    conv_response = {
        "conversation_id": conversation.get("conversation_id"),
        "title": conversation.get("title", ""),
        "message_count": conversation.get("message_count", 0),
        "created_at": conversation.get("created_at"),
        "updated_at": conversation.get("updated_at"),
        "is_owner": is_owner,
        "shared_workspace_ids": conversation.get("shared_workspace_ids", []),
    }
    if not is_owner:
        conv_response["owner_email"] = conversation.get("user_id")

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "conversation": conv_response,
            "messages": messages,
        }, default=str),
    }


def delete_conversation(conversation_id: str, user_id: str = None) -> Dict:
    """Delete a conversation, all its messages, and memory chunks (verifies ownership)."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        # Verify ownership before deleting
        if user_id:
            conversation = get_conversation(conversation_id)
            if not conversation:
                return {
                    "statusCode": 404,
                    "body": json.dumps({"error": "Conversation not found"}),
                }
            if conversation.get("user_id") != user_id:
                return {
                    "statusCode": 403,
                    "body": json.dumps({"error": "Access denied"}),
                }

        # Delete memory chunks from S3 Vectors first
        delete_conversation_memory(conversation_id)

        # Get all items for this conversation (includes messages, metadata, and share items)
        response = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"CONV#{conversation_id}"},
        )

        # Delete all items (messages, metadata, SHARE# items)
        with table.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})

        logger.info(f"Deleted conversation: {conversation_id}")
        return {
            "statusCode": 200,
            "body": json.dumps({"deleted": conversation_id}),
        }
    except ClientError as e:
        logger.error(f"Error deleting conversation: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to delete conversation"}),
        }


# ============================================================================
# SDK Streaming Handler (for InvokeWithResponseStream)
# ============================================================================

def streaming_handler(event, context):
    """
    Streaming handler for SDK invocation via InvokeWithResponseStream.

    This handler is invoked by the Fargate proxy server using the AWS SDK.
    It returns a generator that yields NDJSON chunks, which Lambda streams
    back to the caller.

    Expected event format (same as HTTP handler):
    {
        "body": "{\"message\": \"...\", \"conversation_id\": \"...\"}",
        "headers": {},
        "requestContext": {"http": {"method": "POST", "path": "/"}}
    }

    Yields NDJSON chunks:
        {"type": "metadata", "conversation_id": "...", "sources": [...]}
        {"type": "token", "text": "..."}
        {"type": "done", "message_id": "..."}
    """
    logger.info(f"[streaming] Invoked with event keys: {list(event.keys())}")

    # Use the existing handle_chat_stream generator
    try:
        for chunk in handle_chat_stream(event):
            yield chunk.encode("utf-8") if isinstance(chunk, str) else chunk
    except Exception as e:
        logger.error(f"[streaming] Error: {e}", exc_info=True)
        yield json.dumps({"type": "error", "error": str(e)}).encode("utf-8")
        yield b"\n"
