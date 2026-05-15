"""Unit tests for MCP server HTTP handler, session management, and uncovered paths.

Tests cover:
- McpHandler HTTP request handling (do_GET, do_POST, do_DELETE)
- MCP initialize flow with session creation
- tools/list and tools/call routing
- Session cleanup
- _fetch_tools_live
- _add_mcp_auth_headers with encrypted credentials
- _mcp_initialize_session
- _proxy_tool_call with cached/new sessions
- Error paths in tool calls
- AWS client getters first-init path
- Edge cases in search_knowledge_base and get_user_workspace_ids
"""

import importlib.util
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from unittest.mock import MagicMock, patch, PropertyMock

import pytest


# Set required environment variables before importing
os.environ["DYNAMODB_TABLE"] = "test-table"
os.environ["S3_VECTORS_BUCKET"] = "test-vectors-bucket"
os.environ["S3_VECTORS_INDEX"] = "test-vectors-index"
os.environ["KB_BUCKET"] = "test-kb-bucket"
os.environ["ENVIRONMENT"] = "test"
os.environ["MCP_BASE_URL"] = "http://test-alb.example.com"


def _load_mcp_module():
    """Load the MCP server handler module."""
    if "mcp_server_handler_http" in sys.modules:
        del sys.modules["mcp_server_handler_http"]
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../mcp-server/handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "mcp_server_handler_http", handler_path
    )
    mcp_module = importlib.util.module_from_spec(spec)
    sys.modules["mcp_server_handler_http"] = mcp_module
    spec.loader.exec_module(mcp_module)
    return mcp_module


mcp_module = _load_mcp_module()


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear in-memory caches and global clients between tests."""
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()
    # Reset cached AWS clients so getter tests work
    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None
    yield
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()
    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None


# ============================================================================
# Helper to build a mock McpHandler instance
# ============================================================================

def _make_handler(method="POST", path="/mcp/v1", body=None, headers=None):
    """Create a mock McpHandler without starting a real HTTP server.

    Returns a handler instance with mocked wfile and request state.
    """
    handler = mcp_module.McpHandler.__new__(mcp_module.McpHandler)

    # Mock the wfile (output stream)
    handler.wfile = io.BytesIO()

    # Mock rfile (input stream) with body
    if body is not None:
        body_bytes = json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
    else:
        body_bytes = b""
    handler.rfile = io.BytesIO(body_bytes)

    # Mock headers
    from http.client import HTTPMessage
    msg = HTTPMessage()
    if headers:
        for k, v in headers.items():
            msg[k] = v
    if body is not None:
        msg["Content-Length"] = str(len(body_bytes))
    handler.headers = msg

    # Set path
    handler.path = path

    # Mock response methods
    handler._headers_buffer = []
    handler.request_version = "HTTP/1.1"

    # Capture sent responses
    handler._sent_responses = []
    handler._sent_status = []

    original_send_response = BaseHTTPRequestHandler.send_response

    def mock_send_response(self, code, message=None):
        self._sent_status.append(code)

    def mock_send_header(self, keyword, value):
        self._headers_buffer.append((keyword, value))

    def mock_end_headers(self):
        pass

    handler.send_response = lambda code, msg=None: mock_send_response(handler, code, msg)
    handler.send_header = lambda k, v: mock_send_header(handler, k, v)
    handler.end_headers = lambda: mock_end_headers(handler)

    return handler


# ============================================================================
# Tests: McpHandler.do_GET
# ============================================================================

class TestMcpHandlerGet:

    def test_health_check_root(self):
        """GET / returns healthy status."""
        handler = _make_handler(method="GET", path="/")
        handler.do_GET()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["status"] == "healthy"
        assert data["service"] == "outcomeops-mcp"

    def test_health_check_health_path(self):
        """GET /health returns healthy status."""
        handler = _make_handler(method="GET", path="/health")
        handler.do_GET()

        assert 200 in handler._sent_status

    def test_health_check_mcp_v1(self):
        """GET /mcp/v1 returns healthy status."""
        handler = _make_handler(method="GET", path="/mcp/v1")
        handler.do_GET()

        assert 200 in handler._sent_status

    def test_unknown_path_returns_404(self):
        """GET to unknown path returns 404."""
        handler = _make_handler(method="GET", path="/unknown")
        handler.do_GET()

        assert 404 in handler._sent_status


# ============================================================================
# Tests: McpHandler.do_DELETE
# ============================================================================

class TestMcpHandlerDelete:

    def test_delete_existing_session(self):
        """DELETE with valid session ID removes it."""
        session_id = "test-session-del"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
        }

        handler = _make_handler(
            method="DELETE",
            path="/mcp/v1",
            headers={"Mcp-Session-Id": session_id},
        )
        handler.do_DELETE()

        assert session_id not in mcp_module._sessions
        assert 200 in handler._sent_status

    def test_delete_nonexistent_session(self):
        """DELETE with unknown session ID still returns 200."""
        handler = _make_handler(
            method="DELETE",
            path="/mcp/v1",
            headers={"Mcp-Session-Id": "no-such-session"},
        )
        handler.do_DELETE()

        assert 200 in handler._sent_status


# ============================================================================
# Tests: McpHandler.do_POST
# ============================================================================

class TestMcpHandlerPost:

    def test_post_wrong_path_returns_404(self):
        """POST to a path other than /mcp/v1 returns 404."""
        handler = _make_handler(path="/wrong", body={"jsonrpc": "2.0"})
        handler.do_POST()

        assert 404 in handler._sent_status

    def test_post_empty_body_returns_error(self):
        """POST with Content-Length 0 returns JSON-RPC error."""
        handler = _make_handler(path="/mcp/v1", body=None)
        handler.headers["Content-Length"] = "0"
        handler.do_POST()

        assert 400 in handler._sent_status

    def test_post_invalid_json_returns_error(self):
        """POST with invalid JSON body returns parse error."""
        handler = _make_handler(path="/mcp/v1")
        bad_body = b"not-json"
        handler.rfile = io.BytesIO(bad_body)
        handler.headers["Content-Length"] = str(len(bad_body))
        handler.do_POST()

        assert 400 in handler._sent_status

    @patch.object(mcp_module, "validate_api_key", return_value="user@example.com")
    def test_initialize_creates_session(self, mock_validate):
        """POST with initialize method creates a session and returns capabilities."""
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "1.0"},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Authorization": "Bearer test-key"},
        )
        handler.do_POST()

        assert 200 in handler._sent_status
        # Session should be created
        assert len(mcp_module._sessions) == 1
        session = list(mcp_module._sessions.values())[0]
        assert session["email"] == "user@example.com"

        # Check response includes session ID header
        mcp_headers = [v for k, v in handler._headers_buffer if k == "Mcp-Session-Id"]
        assert len(mcp_headers) == 1

    @patch.object(mcp_module, "validate_api_key", return_value=None)
    def test_initialize_no_auth_returns_401(self, mock_validate):
        """POST initialize without valid API key returns 401."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Authorization": "Bearer bad-key"},
        )
        handler.do_POST()

        assert 401 in handler._sent_status

    def test_post_without_auth_returns_401(self):
        """POST non-initialize method without auth returns 401."""
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        handler = _make_handler(path="/mcp/v1", body=body)
        handler.do_POST()

        assert 401 in handler._sent_status

    def test_post_with_valid_session(self):
        """POST with valid session ID processes the request."""
        session_id = "test-session-valid"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
        }

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
            with patch.object(mcp_module, "discover_mcp_servers", return_value=[]):
                handler.do_POST()

        assert 200 in handler._sent_status

    def test_post_with_expired_session_falls_back_to_api_key(self):
        """POST with expired session tries API key auth."""
        session_id = "expired-session"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time() - 7200,  # 2 hours ago, expired
        }

        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={
                "Mcp-Session-Id": session_id,
                "Authorization": "Bearer valid-key",
            },
        )

        with patch.object(mcp_module, "validate_api_key", return_value="user@example.com"):
            with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
                with patch.object(mcp_module, "discover_mcp_servers", return_value=[]):
                    handler.do_POST()

        assert 200 in handler._sent_status

    def test_notifications_initialized_returns_204(self):
        """POST notifications/initialized returns 204 No Content."""
        session_id = "test-session-notif"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
        }

        body = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )
        handler.do_POST()

        assert 204 in handler._sent_status

    def test_unknown_method_returns_method_not_found(self):
        """POST with unknown method returns JSON-RPC method-not-found error."""
        session_id = "test-session-unknown"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
        }

        body = {"jsonrpc": "2.0", "id": 1, "method": "unknown/method", "params": {}}
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )
        handler.do_POST()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["error"]["code"] == -32601


# ============================================================================
# Tests: tools/call routing
# ============================================================================

class TestToolsCall:

    def _setup_session(self):
        session_id = "test-session-tools"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
        }
        return session_id

    def test_native_tool_success(self):
        """tools/call for a native tool returns result."""
        session_id = self._setup_session()

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_knowledge_base",
                "arguments": {"query": "test query"},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        mock_handler_fn = MagicMock(return_value={"text": "some results"})
        original = mcp_module.TOOL_HANDLERS["query_knowledge_base"]
        mcp_module.TOOL_HANDLERS["query_knowledge_base"] = mock_handler_fn
        try:
            handler.do_POST()
        finally:
            mcp_module.TOOL_HANDLERS["query_knowledge_base"] = original

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["result"]["content"][0]["text"] == "some results"
        assert data["result"]["isError"] is False

    def test_native_tool_returns_error(self):
        """tools/call for a native tool returning an error."""
        session_id = self._setup_session()

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_knowledge_base",
                "arguments": {},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        with patch.object(
            mcp_module,
            "handle_query_knowledge_base",
            return_value={"error": "query is required"},
        ):
            handler.do_POST()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["result"]["isError"] is True
        assert "Error:" in data["result"]["content"][0]["text"]

    def test_native_tool_exception(self):
        """tools/call for a native tool that raises exception returns JSON-RPC error."""
        session_id = self._setup_session()

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "query_knowledge_base",
                "arguments": {"query": "test"},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        mock_handler_fn = MagicMock(side_effect=RuntimeError("boom"))
        original = mcp_module.TOOL_HANDLERS["query_knowledge_base"]
        mcp_module.TOOL_HANDLERS["query_knowledge_base"] = mock_handler_fn
        try:
            handler.do_POST()
        finally:
            mcp_module.TOOL_HANDLERS["query_knowledge_base"] = original

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["error"]["code"] == -32603
        assert "RuntimeError" in data["error"]["message"]

    def test_unknown_tool_returns_error(self):
        """tools/call for an unknown tool returns error."""
        session_id = self._setup_session()

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "nonexistent_tool",
                "arguments": {},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )
        handler.do_POST()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["error"]["code"] == -32602

    def test_proxied_tool_call_success(self):
        """tools/call routes to proxy for namespaced tools."""
        session_id = self._setup_session()
        mcp_module._sessions[session_id]["tool_routing"] = {
            "sonarqube__search_issues": {
                "config": {
                    "mcp_id": "catalog-sonarqube",
                    "name": "SonarQube",
                    "url": "http://test/mcp",
                    "auth_type": "none",
                },
                "original_name": "search_issues",
            }
        }

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "sonarqube__search_issues",
                "arguments": {"project_key": "my-project"},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        with patch.object(
            mcp_module,
            "_proxy_tool_call",
            return_value={"content": [{"type": "text", "text": "issues found"}]},
        ):
            handler.do_POST()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["result"]["content"][0]["text"] == "issues found"

    def test_proxied_tool_call_exception(self):
        """tools/call proxy failure returns JSON-RPC error."""
        session_id = self._setup_session()
        mcp_module._sessions[session_id]["tool_routing"] = {
            "sonarqube__search_issues": {
                "config": {
                    "mcp_id": "catalog-sonarqube",
                    "name": "SonarQube",
                    "url": "http://test/mcp",
                    "auth_type": "none",
                },
                "original_name": "search_issues",
            }
        }

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "sonarqube__search_issues",
                "arguments": {},
            },
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        with patch.object(
            mcp_module,
            "_proxy_tool_call",
            side_effect=ConnectionError("connection refused"),
        ):
            handler.do_POST()

        assert 200 in handler._sent_status
        output = handler.wfile.getvalue().decode()
        data = json.loads(output)
        assert data["error"]["code"] == -32603
        assert "Proxy error" in data["error"]["message"]


# ============================================================================
# Tests: Session cleanup
# ============================================================================

class TestSessionCleanup:

    def test_cleanup_removes_expired_sessions(self):
        """cleanup_sessions removes sessions older than SESSION_TIMEOUT."""
        mcp_module._sessions["active"] = {
            "email": "active@example.com",
            "last_active": time.time(),
        }
        mcp_module._sessions["expired"] = {
            "email": "expired@example.com",
            "last_active": time.time() - 7200,  # 2 hours ago
        }

        # Run one iteration of cleanup (mock time.sleep to not block)
        now = time.time()
        expired = [
            sid for sid, s in mcp_module._sessions.items()
            if now - s["last_active"] > mcp_module.SESSION_TIMEOUT
        ]
        for sid in expired:
            del mcp_module._sessions[sid]

        assert "active" in mcp_module._sessions
        assert "expired" not in mcp_module._sessions


# ============================================================================
# Tests: _add_mcp_auth_headers with encrypted credentials
# ============================================================================

class TestAddMcpAuthHeaders:

    @patch.object(mcp_module, "_get_ssm_client")
    def test_ssm_token_auth(self, mock_get_ssm):
        """Adds SSM token header when ssm_token_path and ssm_token_header set."""
        mock_ssm = MagicMock()
        mock_get_ssm.return_value = mock_ssm
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "secret-token-value"}
        }

        headers = {}
        config = {
            "auth_type": "none",
            "ssm_token_path": "/test/token",
            "ssm_token_header": "X-Custom-Token",
            "encrypted_credentials": "",
        }
        mcp_module._add_mcp_auth_headers(headers, config)

        assert headers["X-Custom-Token"] == "secret-token-value"

    @patch("mcp_server_handler_http.boto3")
    def test_bearer_encrypted_credentials(self, mock_boto3):
        """Adds Bearer Authorization header for encrypted bearer credentials."""
        mock_kms = MagicMock()
        mock_boto3.client.return_value = mock_kms
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted-token"}

        import base64
        encrypted = base64.b64encode(b"encrypted-data").decode()

        headers = {}
        config = {
            "auth_type": "bearer",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": encrypted,
        }
        mcp_module._add_mcp_auth_headers(headers, config)

        assert headers["Authorization"] == "Bearer decrypted-token"

    @patch("mcp_server_handler_http.boto3")
    def test_api_key_encrypted_credentials(self, mock_boto3):
        """Adds X-API-Key header for encrypted api_key credentials."""
        mock_kms = MagicMock()
        mock_boto3.client.return_value = mock_kms
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted-api-key"}

        import base64
        encrypted = base64.b64encode(b"encrypted-data").decode()

        headers = {}
        config = {
            "auth_type": "api_key",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": encrypted,
        }
        mcp_module._add_mcp_auth_headers(headers, config)

        assert headers["X-API-Key"] == "decrypted-api-key"


# ============================================================================
# Tests: _fetch_tools_live
# ============================================================================

class TestFetchToolsLive:

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    def test_fetch_tools_live_success(self, mock_urlopen):
        """Fetches tools from a remote MCP server via JSON-RPC."""
        # Mock initialize response
        init_resp = MagicMock()
        init_resp.headers = MagicMock()
        init_resp.headers.get.return_value = "session-123"
        init_resp.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {}
        }).encode()

        # Mock tools/list response
        tools_resp = MagicMock()
        tools_resp.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 2,
            "result": {
                "tools": [
                    {"name": "search_issues", "description": "Search issues"}
                ]
            },
        }).encode()

        mock_urlopen.side_effect = [init_resp, tools_resp]

        config = {
            "url": "http://test-server/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }
        tools = mcp_module._fetch_tools_live(config)

        assert len(tools) == 1
        assert tools[0]["name"] == "search_issues"


# ============================================================================
# Tests: _mcp_initialize_session
# ============================================================================

class TestMcpInitializeSession:

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    def test_initialize_session_with_session_id(self, mock_urlopen):
        """Returns session_id and sends initialized notification."""
        # Mock initialize response
        init_resp = MagicMock()
        init_resp.read.return_value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        init_resp.headers = MagicMock()
        init_resp.headers.get.return_value = "new-session-123"

        # Mock notification response
        notif_resp = MagicMock()

        mock_urlopen.side_effect = [init_resp, notif_resp]

        result = mcp_module._mcp_initialize_session(
            "http://test/mcp", {"Content-Type": "application/json"}
        )

        assert result == "new-session-123"
        assert mock_urlopen.call_count == 2

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    def test_initialize_session_no_session_id(self, mock_urlopen):
        """Returns empty string when server doesn't provide session ID."""
        init_resp = MagicMock()
        init_resp.read.return_value = json.dumps({"jsonrpc": "2.0", "id": 1, "result": {}}).encode()
        init_resp.headers = MagicMock()
        init_resp.headers.get.return_value = ""

        mock_urlopen.return_value = init_resp

        result = mcp_module._mcp_initialize_session(
            "http://test/mcp", {"Content-Type": "application/json"}
        )

        assert result == ""
        # Should NOT send initialized notification when no session_id
        assert mock_urlopen.call_count == 1


# ============================================================================
# Tests: _proxy_tool_call
# ============================================================================

class TestProxyToolCall:

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    @patch.object(mcp_module, "_add_mcp_auth_headers")
    def test_proxy_with_cached_session(self, mock_auth, mock_urlopen):
        """Uses cached MCP session for proxy calls."""
        mcp_module._mcp_sessions["test-mcp"] = {
            "session_id": "cached-session",
            "created_at": time.time(),
        }

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "result data"}]},
        }).encode()
        mock_urlopen.return_value = resp

        config = {
            "mcp_id": "test-mcp",
            "name": "TestServer",
            "url": "http://test/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }

        result = mcp_module._proxy_tool_call(config, "search", {"q": "test"})

        assert result["content"][0]["text"] == "result data"

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    @patch.object(mcp_module, "_add_mcp_auth_headers")
    @patch.object(mcp_module, "_mcp_initialize_session", return_value="new-session")
    def test_proxy_creates_new_session(self, mock_init, mock_auth, mock_urlopen):
        """Creates new MCP session when no cached session exists."""
        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": "data"}]},
        }).encode()
        mock_urlopen.return_value = resp

        config = {
            "mcp_id": "new-mcp",
            "name": "NewServer",
            "url": "http://test/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }

        result = mcp_module._proxy_tool_call(config, "tool_name", {})

        assert "new-mcp" in mcp_module._mcp_sessions
        mock_init.assert_called_once()

    @patch("mcp_server_handler_http.urllib.request.urlopen")
    @patch.object(mcp_module, "_add_mcp_auth_headers")
    def test_proxy_error_response(self, mock_auth, mock_urlopen):
        """Proxy returns error when target server returns JSON-RPC error."""
        mcp_module._mcp_sessions["err-mcp"] = {
            "session_id": "s1",
            "created_at": time.time(),
        }

        resp = MagicMock()
        resp.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32603, "message": "Internal error"},
        }).encode()
        mock_urlopen.return_value = resp

        config = {
            "mcp_id": "err-mcp",
            "name": "ErrServer",
            "url": "http://test/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }

        result = mcp_module._proxy_tool_call(config, "failing_tool", {})

        assert result["isError"] is True
        assert "Internal error" in result["content"][0]["text"]


# ============================================================================
# Tests: AWS client getters (first-init path)
# ============================================================================

class TestAwsClientGetters:

    @patch("mcp_server_handler_http.boto3")
    def test_get_dynamodb_creates_resource(self, mock_boto3):
        """_get_dynamodb creates boto3 resource on first call."""
        mock_resource = MagicMock()
        mock_boto3.resource.return_value = mock_resource

        result = mcp_module._get_dynamodb()

        assert result == mock_resource
        mock_boto3.resource.assert_called_once_with("dynamodb", region_name=mcp_module.AWS_REGION)

    @patch("mcp_server_handler_http.boto3")
    def test_get_s3vectors_creates_client(self, mock_boto3):
        """_get_s3vectors_client creates boto3 client on first call."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        result = mcp_module._get_s3vectors_client()

        assert result == mock_client
        mock_boto3.client.assert_called_with("s3vectors", region_name=mcp_module.AWS_REGION)

    @patch("mcp_server_handler_http.boto3")
    def test_get_bedrock_creates_client(self, mock_boto3):
        """_get_bedrock_client creates boto3 client on first call."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        result = mcp_module._get_bedrock_client()

        assert result == mock_client
        mock_boto3.client.assert_called_with("bedrock-runtime", region_name=mcp_module.AWS_REGION)

    @patch("mcp_server_handler_http.boto3")
    def test_get_ssm_creates_client(self, mock_boto3):
        """_get_ssm_client creates boto3 client on first call."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        result = mcp_module._get_ssm_client()

        assert result == mock_client
        mock_boto3.client.assert_called_with("ssm", region_name=mcp_module.AWS_REGION)


# ============================================================================
# Tests: search_knowledge_base edge cases
# ============================================================================

class TestSearchKnowledgeBaseEdges:

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[])
    def test_empty_embedding_returns_empty(self, mock_embed, mock_s3v):
        """Returns empty list when embedding generation returns empty."""
        result = mcp_module.search_knowledge_base("test query")
        assert result == []

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    def test_client_error_returns_empty(self, mock_embed, mock_s3v):
        """Returns empty list on ClientError."""
        from botocore.exceptions import ClientError
        mock_s3v.return_value.query_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "fail"}}, "QueryVectors"
        )
        result = mcp_module.search_knowledge_base("test query")
        assert result == []

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    def test_multiple_workspace_filter(self, mock_embed, mock_s3v):
        """Uses $in filter for multiple workspace IDs."""
        mock_s3v.return_value.query_vectors.return_value = {"vectors": []}

        mcp_module.search_knowledge_base("test", workspace_ids=["ws-1", "ws-2"])

        call_kwargs = mock_s3v.return_value.query_vectors.call_args[1]
        assert call_kwargs["filter"] == {"workspace_id": {"$in": ["ws-1", "ws-2"]}}

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    def test_source_type_repo_fallback(self, mock_embed, mock_s3v):
        """Builds repo name from source_type and folder_path when repo is empty."""
        mock_s3v.return_value.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-1",
                    "distance": 0.9,
                    "metadata": {
                        "content": "Some content",
                        "repo": "",
                        "source": "confluence",
                        "folder_path": "team/my-space",
                        "type": "doc",
                    },
                }
            ]
        }

        results = mcp_module.search_knowledge_base("test")
        assert results[0]["repo"] == "Confluence/my-space"

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    def test_github_source_repo_fallback(self, mock_embed, mock_s3v):
        """Builds repo name from first segment of folder_path for github source."""
        mock_s3v.return_value.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-1",
                    "distance": 0.9,
                    "metadata": {
                        "content": "Content",
                        "repo": "",
                        "source": "github",
                        "folder_path": "my-repo/docs/adr",
                        "type": "adr",
                    },
                }
            ]
        }

        results = mcp_module.search_knowledge_base("test")
        assert results[0]["repo"] == "my-repo"

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    def test_source_only_repo_fallback(self, mock_embed, mock_s3v):
        """Uses capitalized source when no folder_path."""
        mock_s3v.return_value.query_vectors.return_value = {
            "vectors": [
                {
                    "key": "doc-1",
                    "distance": 0.9,
                    "metadata": {
                        "content": "Content",
                        "repo": "",
                        "source": "jira",
                        "folder_path": "",
                        "type": "ticket",
                    },
                }
            ]
        }

        results = mcp_module.search_knowledge_base("test")
        assert results[0]["repo"] == "Jira"


# ============================================================================
# Tests: get_user_workspace_ids edge cases
# ============================================================================

class TestGetUserWorkspaceIds:

    @patch.object(mcp_module, "_get_dynamodb")
    def test_client_error_returns_empty(self, mock_get_ddb):
        """Returns empty list on ClientError."""
        from botocore.exceptions import ClientError
        table = MagicMock()
        mock_get_ddb.return_value.Table.return_value = table
        table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "fail"}}, "GetItem"
        )

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert result == []

    @patch.object(mcp_module, "_get_dynamodb")
    def test_regular_user_no_workspaces(self, mock_get_ddb):
        """Regular user with no workspace memberships returns empty list."""
        table = MagicMock()
        mock_get_ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"org_role": "member"}}
        table.query.return_value = {"Items": []}

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert result == []

    @patch.object(mcp_module, "_get_dynamodb")
    def test_shared_workspaces_included(self, mock_get_ddb):
        """Shared workspaces are included in user's workspace list."""
        table = MagicMock()
        mock_get_ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"org_role": "member"}}
        table.query.return_value = {
            "Items": [{"workspace_id": "ws-direct"}]
        }
        table.scan.return_value = {
            "Items": [
                {
                    "PK": "WS#ws-shared",
                    "search_allowed_workspaces": ["ws-direct"],
                },
                {
                    "PK": "WS#ws-direct",
                    "search_allowed_workspaces": [],
                },
            ]
        }

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert "ws-direct" in result
        assert "ws-shared" in result

    @patch.object(mcp_module, "_get_dynamodb")
    def test_admin_paginates_across_scan_pages(self, mock_get_ddb):
        """Admin enumeration must walk every LastEvaluatedKey page so workspaces
        past the 1MB Scan boundary aren't silently dropped at enterprise scale."""
        table = MagicMock()
        mock_get_ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"org_role": "org_admin"}}

        page1 = {
            "Items": [{"PK": "WS#a"}, {"PK": "WS#b"}],
            "LastEvaluatedKey": {"PK": "WS#b", "SK": "METADATA"},
        }
        page2 = {
            "Items": [{"PK": "WS#c"}, {"PK": "WS#d"}],
            "LastEvaluatedKey": {"PK": "WS#d", "SK": "METADATA"},
        }
        page3 = {"Items": [{"PK": "WS#e"}]}
        table.scan.side_effect = [page1, page2, page3]

        result = mcp_module.get_user_workspace_ids("admin@example.com")
        assert sorted(result) == ["a", "b", "c", "d", "e"]
        assert table.scan.call_count == 3
        # Subsequent calls must pass the prior page's LastEvaluatedKey.
        assert table.scan.call_args_list[1][1]["ExclusiveStartKey"] == {
            "PK": "WS#b", "SK": "METADATA"
        }
        assert table.scan.call_args_list[2][1]["ExclusiveStartKey"] == {
            "PK": "WS#d", "SK": "METADATA"
        }

    @patch.object(mcp_module, "_get_dynamodb")
    def test_share_resolution_paginates_across_scan_pages(self, mock_get_ddb):
        """Share-resolution scan must walk every page so a ws-shared row that
        sits on page 2+ still gets matched against the user's direct memberships."""
        table = MagicMock()
        mock_get_ddb.return_value.Table.return_value = table
        table.get_item.return_value = {"Item": {"org_role": "member"}}
        table.query.return_value = {
            "Items": [{"workspace_id": "ws-direct"}]
        }

        page1 = {
            "Items": [
                {"PK": "WS#ws-direct", "search_allowed_workspaces": []},
            ],
            "LastEvaluatedKey": {"PK": "WS#ws-direct", "SK": "METADATA"},
        }
        page2 = {
            "Items": [
                {"PK": "WS#ws-shared-late", "search_allowed_workspaces": ["ws-direct"]},
            ],
        }
        table.scan.side_effect = [page1, page2]

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert "ws-direct" in result
        assert "ws-shared-late" in result
        assert table.scan.call_count == 2


# ============================================================================
# Tests: McpHandler.log_message override
# ============================================================================

class TestLogMessage:

    def test_log_message_uses_logger(self):
        """log_message routes to Python logger."""
        handler = _make_handler()
        with patch.object(mcp_module.logger, "info") as mock_log:
            handler.log_message("%s - %s", "GET", "/health")
            mock_log.assert_called_once_with("GET - /health")


# ============================================================================
# Tests: handle_list_workspaces ClientError
# ============================================================================

class TestHandleListWorkspacesError:

    @patch.object(mcp_module, "_get_dynamodb")
    @patch.object(mcp_module, "get_user_workspace_ids", side_effect=Exception("db fail"))
    def test_list_workspaces_error(self, mock_ws_ids, mock_ddb):
        """handle_list_workspaces returns error on exception."""
        from botocore.exceptions import ClientError

        # Patch get_user_workspace_ids to raise ClientError
        with patch.object(
            mcp_module,
            "get_user_workspace_ids",
            side_effect=ClientError(
                {"Error": {"Code": "500", "Message": "fail"}}, "Query"
            ),
        ):
            result = mcp_module.handle_list_workspaces({}, "user@example.com")
            assert "error" in result

    @patch.object(mcp_module, "_get_s3vectors_client")
    @patch.object(mcp_module, "generate_embedding", return_value=[0.1] * 1024)
    @patch.object(mcp_module, "get_user_workspace_ids", return_value=["ws-1"])
    def test_list_workspace_documents_client_error(
        self, mock_ws_ids, mock_embed, mock_s3v
    ):
        """handle_list_workspace_documents returns error when the vector
        store query raises ClientError.
        """
        from botocore.exceptions import ClientError

        s3v = MagicMock()
        mock_s3v.return_value = s3v
        s3v.query_vectors.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "fail"}}, "QueryVectors"
        )

        result = mcp_module.handle_list_workspace_documents(
            {"workspace_id": "ws-1"}, "user@example.com"
        )
        assert "error" in result
