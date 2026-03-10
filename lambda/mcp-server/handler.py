"""
OutcomeOps MCP Server - Streamable HTTP Transport

Exposes OutcomeOps knowledge base tools to MCP-compatible clients
(Claude Code, VS Code, Cursor, etc.) via the MCP Streamable HTTP protocol.

Authentication: API keys (Bearer token), validated against DynamoDB.
Transport: JSON-RPC 2.0 over HTTP POST.
"""

import hashlib
import json
import logging
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import boto3
from botocore.exceptions import ClientError

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
EMBEDDING_MODEL_ID = os.environ.get(
    "BEDROCK_EMBEDDING_MODEL_ID", "amazon.titan-embed-text-v2:0"
)

# Session storage (in-memory, single instance)
_sessions = {}  # session_id -> {"email": str, "last_active": float}
SESSION_TIMEOUT = 3600  # 1 hour

# Cached AWS clients
_dynamodb = None
_s3vectors_client = None
_bedrock_client = None


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
            },
            "required": ["topic"],
        },
    },
]


# ============================================================================
# API Key Authentication
# ============================================================================


def validate_api_key(api_key):
    """Validate an API key against DynamoDB. Returns user email or None."""
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
            return None

        item = items[0]

        if item.get("status") != "active":
            return None

        expires_at = item.get("expires_at")
        if expires_at:
            if datetime.fromisoformat(expires_at) < datetime.now(timezone.utc):
                return None

        pk = item.get("PK", "")
        email = pk.replace("USER#", "") if pk.startswith("USER#") else None
        if not email:
            return None

        # Update last_used_at (best-effort)
        try:
            table.update_item(
                Key={"PK": item["PK"], "SK": item["SK"]},
                UpdateExpression="SET last_used_at = :now",
                ExpressionAttributeValues={
                    ":now": datetime.now(timezone.utc).isoformat(),
                },
            )
        except Exception:
            pass

        return email
    except ClientError as e:
        logger.error(f"Error validating API key: {e}")
        return None


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


def search_knowledge_base(query, workspace_ids=None, top_k=10):
    """Search KB documents using S3 Vectors."""
    s3vectors = _get_s3vectors_client()

    embedding = generate_embedding(query)
    if not embedding:
        return []

    query_params = {
        "vectorBucketName": S3_VECTORS_BUCKET,
        "indexName": S3_VECTORS_INDEX,
        "queryVector": {"float32": embedding},
        "topK": top_k,
        "returnMetadata": True,
        "returnDistance": True,
    }

    if workspace_ids:
        if len(workspace_ids) == 1:
            query_params["filter"] = {"workspace_id": {"$eq": workspace_ids[0]}}
        else:
            query_params["filter"] = {"workspace_id": {"$in": workspace_ids}}

    try:
        response = s3vectors.query_vectors(**query_params)

        results = []
        for vector in response.get("vectors", []):
            metadata = vector.get("metadata", {})
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

        return results
    except ClientError as e:
        logger.error(f"Error searching vectors: {e}")
        return []


def format_results(results):
    """Format search results as text for MCP response."""
    if not results:
        return "No relevant documents found."

    parts = []
    for i, r in enumerate(results, 1):
        source = r.get("repo", "unknown")
        doc_type = r.get("type", "document")
        content = r.get("content", "")[:3000]
        parts.append(f"[{i}] Source: {source} ({doc_type})\n{content}")

    return "\n\n---\n\n".join(parts)


def get_user_workspace_ids(email):
    """Get workspace IDs the user has access to."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        # Check if org admin
        org_resp = table.get_item(Key={"PK": f"USER#{email}", "SK": "ORG_MEMBER"})
        is_admin = org_resp.get("Item", {}).get("org_role") == "org_admin"

        if is_admin:
            # Scan for all workspaces
            workspace_ids = []
            response = table.scan(
                FilterExpression="begins_with(SK, :sk)",
                ExpressionAttributeValues={":sk": "METADATA"},
                ProjectionExpression="PK",
            )
            for item in response.get("Items", []):
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    workspace_ids.append(pk.replace("WS#", ""))
            return workspace_ids
        else:
            response = table.query(
                KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
                ExpressionAttributeValues={
                    ":pk": f"USER#{email}",
                    ":sk": "WS_MEMBER#",
                },
            )
            return [
                item.get("workspace_id")
                for item in response.get("Items", [])
                if item.get("workspace_id")
            ]
    except ClientError as e:
        logger.error(f"Error getting user workspaces: {e}")
        return []


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

    results = search_knowledge_base(query, workspace_ids=workspace_ids)
    return {"text": format_results(results)}


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
    """Handle list_workspace_documents tool call."""
    workspace_id = params.get("workspace_id", "")
    if not workspace_id:
        return {"error": "workspace_id is required"}

    # Verify access
    user_ws_ids = get_user_workspace_ids(email)
    if workspace_id not in user_ws_ids:
        return {"error": "You do not have access to this workspace"}

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(DYNAMODB_TABLE)

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "DOC#",
            },
        )

        documents = []
        for item in response.get("Items", []):
            documents.append({
                "name": item.get("document_name", item.get("SK", "").replace("DOC#", "")),
                "type": item.get("document_type", "unknown"),
                "updated_at": item.get("updated_at", ""),
            })

        if not documents:
            text = "No documents found in this workspace."
        else:
            text = f"Documents in workspace ({len(documents)}):\n\n"
            for doc in documents:
                text += f"- {doc['name']} ({doc['type']})"
                if doc["updated_at"]:
                    text += f" - updated {doc['updated_at']}"
                text += "\n"

        return {"text": text}
    except ClientError as e:
        logger.error(f"Error listing documents: {e}")
        return {"error": "Failed to list workspace documents"}


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

    results = search_knowledge_base(query, workspace_ids=workspace_ids)
    return {"text": format_results(results)}


TOOL_HANDLERS = {
    "query_knowledge_base": handle_query_knowledge_base,
    "list_workspaces": handle_list_workspaces,
    "list_workspace_documents": handle_list_workspace_documents,
    "get_coding_standards": handle_get_coding_standards,
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
                email = validate_api_key(api_key)

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
            email = validate_api_key(api_key)

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
                "tools": {"listChanged": False},
            },
            "serverInfo": {
                "name": "OutcomeOps",
                "version": "1.0.0",
            },
        }

        self._send_json(200, jsonrpc_response(request_id, result), session_id=session_id)

    def _handle_tools_list(self, request_id, session_id):
        """Handle tools/list request."""
        result = {"tools": MCP_TOOLS}
        self._send_json(200, jsonrpc_response(request_id, result), session_id=session_id)

    def _handle_tools_call(self, request_id, params, email, session_id):
        """Handle tools/call request."""
        tool_name = params.get("name", "")
        tool_args = params.get("arguments", {})

        handler = TOOL_HANDLERS.get(tool_name)
        if not handler:
            self._send_json(
                200,
                jsonrpc_error(request_id, -32602, f"Unknown tool: {tool_name}"),
                session_id=session_id,
            )
            return

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
        except Exception as e:
            logger.error(f"Error executing tool {tool_name}: {e}", exc_info=True)
            self._send_json(
                200,
                jsonrpc_error(request_id, -32603, f"Internal error: {type(e).__name__}"),
                session_id=session_id,
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
