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

# Bedrock config with longer timeout for streaming
bedrock_config = Config(
    read_timeout=300,
    connect_timeout=10,
    retries={"max_attempts": 3}
)

# Bedrock model IDs (configurable via environment variables)
ADVANCED_MODEL_ID = os.environ.get(
    "BEDROCK_ADVANCED_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
)
BASIC_MODEL_ID = os.environ.get(
    "BEDROCK_BASIC_MODEL_ID", "us.anthropic.claude-3-5-haiku-20241022-v1:0"
)
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

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

    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers=headers, method="POST"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        session_id = resp.headers.get("Mcp-Session-Id")
        data = json.loads(resp.read().decode())
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

    headers = {"Content-Type": "application/json"}

    if server["auth_type"] == "bearer" and server.get("encrypted_credentials"):
        token = _decrypt_mcp_credentials(server["encrypted_credentials"])
        headers["Authorization"] = f"Bearer {token}"
    elif server["auth_type"] == "api_key" and server.get("encrypted_credentials"):
        key = _decrypt_mcp_credentials(server["encrypted_credentials"])
        headers["X-API-Key"] = key

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

    @field_validator("message")
    @classmethod
    def strip_message(cls, v: str) -> str:
        return v.strip()


# Pricing per 1K tokens (as of Jan 2025)
PRICING = {
    "haiku": {"input": 0.001, "output": 0.005},  # Haiku 3.5
    "sonnet": {"input": 0.003, "output": 0.015},  # Sonnet 4.5
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
    if workspace_id:
        item["workspace_id"] = workspace_id

    table.put_item(Item=item)
    logger.info(f"Created conversation: {conversation_id} (user: {user_id}, workspace: {workspace_id})")
    return item


def update_conversation(conversation_id: str, message_count: int):
    """Update conversation message count and timestamp."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    now = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={"PK": f"CONV#{conversation_id}", "SK": "METADATA"},
            UpdateExpression="SET message_count = :mc, updated_at = :ua",
            ExpressionAttributeValues={":mc": message_count, ":ua": now},
        )
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
            # Extract repo: prefer explicit field, fall back to folder_path for GitHub sources
            repo = metadata.get("repo", "")
            if not repo and metadata.get("source") == "github":
                folder_path = metadata.get("folder_path", "")
                repo = folder_path.split("/")[0] if folder_path else ""

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


def format_context(results: List[Dict]) -> str:
    """Format search results as context for the prompt."""
    if not results:
        return "No relevant documents found."

    context_parts = []
    for i, r in enumerate(results, 1):
        source = r.get("repo", "unknown")
        doc_type = r.get("type", "document")
        content = r.get("content", "")[:3000]  # Limit per doc
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
            UpdateExpression="SET memory_chunk_count = :mcc, updated_at = :ua",
            ExpressionAttributeValues={":mcc": chunk_count, ":ua": now},
        )
    except ClientError as e:
        logger.error(f"Error updating memory chunk count: {e}")


# ============================================================================
# Answer Generation (Streaming)
# ============================================================================

# System prompt is built from shared module for consistency
SYSTEM_PROMPT = build_chat_system_prompt()


def generate_answer_stream(
    question: str,
    context: str,
    history: List[Dict],
    memory_context: str = "",
    advanced: bool = False,
    tool_config: Dict = None,
    tool_mapping: Dict = None,
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

    user_prompt = f"""Based on the following knowledge base context, answer the user's question.
{memory_section}
KNOWLEDGE BASE CONTEXT:
{context}

USER QUESTION:
{question}

Provide a helpful, accurate answer based on the context above. If the context doesn't contain enough information to fully answer the question, acknowledge this and provide what information you can."""

    messages.append({"role": "user", "content": [{"text": user_prompt}]})

    model_id = ADVANCED_MODEL_ID if advanced else BASIC_MODEL_ID
    max_iterations = 10
    total_input_tokens = 0
    total_output_tokens = 0

    try:
        for iteration in range(max_iterations):
            params = {
                "modelId": model_id,
                "messages": messages,
                "system": [{"text": SYSTEM_PROMPT}],
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

    # Get or create conversation
    conversation = None
    # Support both workspace_ids (new) and workspace_id (deprecated)
    workspace_ids = request.workspace_ids or ([request.workspace_id] if request.workspace_id else None)
    # For conversation storage, use first workspace_id
    primary_workspace_id = workspace_ids[0] if workspace_ids else None
    mcp_server_ids = request.mcp_server_ids

    if request.conversation_id:
        conversation = get_conversation(request.conversation_id)
        if not conversation:
            yield _stream_error("Conversation not found", 404) + "\n"
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
        log_audit_entry(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            lambda_name="chat",
            model_id="test",
            input_text=request.message,
            output_text=test_answer,
            status="refusal",
            flagged_reason="test_refusal_trigger",
            user_email=user_id or "",
            workspace_id=primary_workspace_id or "",
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
        search_results = search_kb(search_query, top_k=request.top_k, workspace_ids=workspace_ids)

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
        user_email=user_id or "",
        workspace_id=primary_workspace_id or "",
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
        GET /conversations/{id} - Get conversation with messages
        DELETE /conversations/{id} - Delete conversation
    """
    http_method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    path = event.get("path") or event.get("requestContext", {}).get("http", {}).get("path", "/")

    # Extract user identity from header (set by Fargate proxy)
    headers = event.get("headers") or {}
    user_id = headers.get("x-user-email")

    logger.info(f"REST API: {http_method} {path} (user: {user_id})")

    # Route handling
    if path == "/conversations" and http_method == "GET":
        return list_conversations(user_id=user_id)
    elif path.startswith("/conversations/") and http_method == "GET":
        conv_id = path.split("/")[-1]
        return get_conversation_with_messages(conv_id)
    elif path.startswith("/conversations/") and http_method == "DELETE":
        conv_id = path.split("/")[-1]
        return delete_conversation(conv_id)
    else:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Not found"}),
        }


def list_conversations(user_id: str = None) -> Dict:
    """List conversations (most recent first), optionally filtered by user."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        # Scan for conversation metadata, optionally filtered by user_id
        if user_id:
            response = table.scan(
                FilterExpression="SK = :sk AND user_id = :uid",
                ExpressionAttributeValues={":sk": "METADATA", ":uid": user_id},
            )
        else:
            response = table.scan(
                FilterExpression="SK = :sk",
                ExpressionAttributeValues={":sk": "METADATA"},
            )

        conversations = sorted(
            response.get("Items", []),
            key=lambda x: x.get("updated_at", ""),
            reverse=True,
        )

        return {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": json.dumps({"conversations": conversations}, default=str),
        }
    except ClientError as e:
        logger.error(f"Error listing conversations: {e}")
        return {
            "statusCode": 500,
            "body": json.dumps({"error": "Failed to list conversations"}),
        }


def get_conversation_with_messages(conversation_id: str) -> Dict:
    """Get conversation with all messages."""
    conversation = get_conversation(conversation_id)
    if not conversation:
        return {
            "statusCode": 404,
            "body": json.dumps({"error": "Conversation not found"}),
        }

    messages = get_conversation_messages(conversation_id, limit=30)

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({
            "conversation": conversation,
            "messages": messages,
        }, default=str),
    }


def delete_conversation(conversation_id: str) -> Dict:
    """Delete a conversation, all its messages, and memory chunks."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(_get_chat_table())

    try:
        # Delete memory chunks from S3 Vectors first
        delete_conversation_memory(conversation_id)

        # Get all items for this conversation
        response = table.query(
            KeyConditionExpression="PK = :pk",
            ExpressionAttributeValues={":pk": f"CONV#{conversation_id}"},
        )

        # Delete all items
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
