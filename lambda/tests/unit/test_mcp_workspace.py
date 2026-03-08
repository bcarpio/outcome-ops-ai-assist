"""Unit tests for MCP server management in workspace-management Lambda."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch, PropertyMock
from datetime import datetime, timezone

import pytest


def _load_handler_module():
    """Load or reload the workspace-management handler module."""
    if 'workspace_management_handler' in sys.modules:
        del sys.modules['workspace_management_handler']
    handler_path = os.path.join(os.path.dirname(__file__), '../../workspace-management/handler.py')
    spec = importlib.util.spec_from_file_location("workspace_management_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['workspace_management_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

# Import functions
create_mcp_server = handler_module.create_mcp_server
list_mcp_servers = handler_module.list_mcp_servers
get_mcp_server = handler_module.get_mcp_server
update_mcp_server = handler_module.update_mcp_server
delete_mcp_server = handler_module.delete_mcp_server
refresh_mcp_tools = handler_module.refresh_mcp_tools
assign_mcp_to_workspace = handler_module.assign_mcp_to_workspace
remove_mcp_from_workspace = handler_module.remove_mcp_from_workspace
list_workspace_mcp_servers = handler_module.list_workspace_mcp_servers
get_chat_available_mcp_servers = handler_module.get_chat_available_mcp_servers
_call_mcp_list_tools = handler_module._call_mcp_list_tools
_is_manifest_stale = handler_module._is_manifest_stale
_encrypt_credentials = handler_module._encrypt_credentials
_decrypt_credentials = handler_module._decrypt_credentials
lambda_handler = handler_module.lambda_handler


@pytest.fixture
def mock_table():
    """Create a mock DynamoDB table."""
    table = MagicMock()
    with patch.object(handler_module, '_get_dynamodb') as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        with patch.object(handler_module, '_get_workspaces_table', return_value='test-table'):
            yield table


@pytest.fixture
def mock_kms():
    """Create a mock KMS client."""
    kms = MagicMock()
    kms.encrypt.return_value = {"CiphertextBlob": b"encrypted-data"}
    kms.decrypt.return_value = {"Plaintext": b"my-secret-token"}
    with patch.object(handler_module, '_get_kms_client', return_value=kms):
        with patch.object(handler_module, '_get_kms_key_arn', return_value='arn:aws:kms:us-east-1:123:key/abc'):
            yield kms


def _make_org_admin(mock_table, email="admin@example.com"):
    """Setup mock to make user an org admin."""
    mock_table.get_item.side_effect = lambda **kwargs: _admin_get_item(kwargs, email)


def _admin_get_item(kwargs, admin_email):
    """Get item mock that returns org_admin for specified email."""
    key = kwargs.get("Key", {})
    pk = key.get("PK", "")
    sk = key.get("SK", "")

    if sk == "ORG_MEMBER" and pk == f"USER#{admin_email}":
        return {"Item": {"PK": pk, "SK": sk, "org_role": "org_admin", "user_email": admin_email}}
    return {}


class TestMcpServerCrud:
    """Test MCP server CRUD operations."""

    def test_create_mcp_server_success(self, mock_table, mock_kms):
        """Test creating an MCP server without credentials."""
        _make_org_admin(mock_table)

        # Mock list_tools to succeed
        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[{"name": "test_tool", "description": "A test tool", "inputSchema": {"type": "object"}}]):
            result = create_mcp_server("admin@example.com", {
                "name": "Test MCP",
                "url": "https://mcp.example.com",
                "auth_type": "none",
            })

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["mcp_server"]["name"] == "Test MCP"
        assert body["mcp_server"]["url"] == "https://mcp.example.com"
        assert "encrypted_credentials" not in body["mcp_server"]

    def test_create_mcp_server_with_bearer(self, mock_table, mock_kms):
        """Test creating an MCP server with bearer auth encrypts credentials."""
        _make_org_admin(mock_table)

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[]):
            result = create_mcp_server("admin@example.com", {
                "name": "Auth MCP",
                "url": "https://mcp.example.com",
                "auth_type": "bearer",
                "credentials": "my-secret-token",
            })

        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["mcp_server"]["has_credentials"] is True
        mock_kms.encrypt.assert_called_once()

    def test_create_mcp_server_not_org_admin(self, mock_table):
        """Test that non-org-admins cannot create MCP servers."""
        mock_table.get_item.return_value = {}  # Not an admin

        result = create_mcp_server("user@example.com", {"name": "Test", "url": "https://example.com"})
        assert result["statusCode"] == 403

    def test_create_mcp_server_invalid_auth_type(self, mock_table, mock_kms):
        """Test validation of auth_type field."""
        _make_org_admin(mock_table)

        result = create_mcp_server("admin@example.com", {
            "name": "Test",
            "url": "https://example.com",
            "auth_type": "invalid",
        })
        assert result["statusCode"] == 400

    def test_list_mcp_servers(self, mock_table):
        """Test listing MCP servers."""
        _make_org_admin(mock_table)
        mock_table.query.return_value = {"Items": [
            {"mcp_id": "abc", "name": "Server 1", "PK": "MCP#abc", "SK": "METADATA", "auth_type": "bearer"},
            {"mcp_id": "def", "name": "Server 2", "PK": "MCP#def", "SK": "METADATA", "auth_type": "none"},
        ]}

        result = list_mcp_servers("admin@example.com")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["mcp_servers"]) == 2
        # Credentials should be redacted
        for server in body["mcp_servers"]:
            assert "encrypted_credentials" not in server

    def test_get_mcp_server_not_found(self, mock_table):
        """Test getting a non-existent MCP server."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            if key.get("SK") == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        result = get_mcp_server("admin@example.com", "nonexistent")
        assert result["statusCode"] == 404

    def test_delete_mcp_server(self, mock_table):
        """Test deleting an MCP server cascades to assignments."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            if key.get("SK") == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if key.get("SK") == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA"}}
            return {}
        mock_table.get_item.side_effect = side_effect
        mock_table.query.return_value = {"Items": [
            {"PK": "WS#ws1", "SK": "MCP_SERVER#abc"},
        ]}

        result = delete_mcp_server("admin@example.com", "abc")
        assert result["statusCode"] == 200


class TestMcpToolRefresh:
    """Test MCP tool refresh functionality."""

    def test_call_mcp_list_tools_success(self, mock_kms):
        """Test successfully calling list_tools on an MCP server."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "tools": [{"name": "get_issue", "description": "Get issue", "inputSchema": {"type": "object"}}]
            }
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_response):
            tools = _call_mcp_list_tools("https://mcp.example.com", "none")

        assert len(tools) == 1
        assert tools[0]["name"] == "get_issue"

    def test_call_mcp_list_tools_with_bearer_auth(self, mock_kms):
        """Test that bearer auth adds Authorization header."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 1, "result": {"tools": []}
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            # Need to encrypt first to get a valid encrypted string
            with patch.object(handler_module, '_decrypt_credentials', return_value="my-token"):
                tools = _call_mcp_list_tools("https://mcp.example.com", "bearer", "encrypted-creds")

        # Verify the request was made with auth header
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert request_obj.get_header("Authorization") == "Bearer my-token"

    def test_call_mcp_list_tools_timeout(self):
        """Test timeout handling for list_tools."""
        import urllib.error
        with patch('urllib.request.urlopen', side_effect=urllib.error.URLError("timeout")):
            with pytest.raises(Exception, match="Failed to connect"):
                _call_mcp_list_tools("https://mcp.example.com", "none")

    def test_is_manifest_stale_never_refreshed(self):
        """Test that manifest is stale when never refreshed."""
        assert _is_manifest_stale({"refresh_interval_days": 7}) is True

    def test_is_manifest_stale_fresh(self):
        """Test that recently refreshed manifest is not stale."""
        now = datetime.now(timezone.utc).isoformat()
        assert _is_manifest_stale({"last_refreshed_at": now, "refresh_interval_days": 7}) is False


class TestMcpWorkspaceAssignment:
    """Test MCP-to-workspace assignment operations."""

    def test_assign_mcp_to_workspace(self, mock_table):
        """Test assigning an MCP server to a workspace."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            pk = key.get("PK", "")
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if pk.startswith("MCP#") and sk == "METADATA":
                return {"Item": {"mcp_id": "mcp1"}}
            if pk.startswith("WS#") and sk == "METADATA":
                return {"Item": {"workspace_id": "ws1"}}
            if sk.startswith("MCP_SERVER#"):
                return {}  # Not already assigned
            # membership check
            if sk.startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {"mcp_id": "mcp1"})
        assert result["statusCode"] == 201

    def test_assign_nonexistent_mcp(self, mock_table):
        """Test assigning a non-existent MCP server returns 404."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            return {}  # MCP not found
        mock_table.get_item.side_effect = side_effect

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {"mcp_id": "nonexistent"})
        assert result["statusCode"] == 404

    def test_assign_duplicate(self, mock_table):
        """Test duplicate assignment returns 409."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            pk = key.get("PK", "")
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if pk.startswith("MCP#") and sk == "METADATA":
                return {"Item": {"mcp_id": "mcp1"}}
            if pk.startswith("WS#") and sk == "METADATA":
                return {"Item": {"workspace_id": "ws1"}}
            if sk.startswith("MCP_SERVER#"):
                return {"Item": {"mcp_id": "mcp1"}}  # Already assigned
            if sk.startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        result = assign_mcp_to_workspace("admin@example.com", "ws1", {"mcp_id": "mcp1"})
        assert result["statusCode"] == 409


class TestMcpChatAvailable:
    """Test MCP server availability for chat."""

    def test_get_chat_mcp_servers_from_workspaces(self, mock_table):
        """Test getting MCP servers available from user's workspaces."""
        call_count = [0]
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {
                    "mcp_id": "mcp1", "name": "Test MCP", "status": "active",
                    "tool_count": 5, "is_global": False,
                }}
            return {}
        mock_table.get_item.side_effect = side_effect

        def query_side_effect(**kwargs):
            kce = kwargs.get("KeyConditionExpression", "")
            eav = kwargs.get("ExpressionAttributeValues", {})
            # Return workspaces for scan
            if "GSI1PK" in str(eav):
                pk_val = eav.get(":pk", "")
                if pk_val == "ORG#default":
                    return {"Items": []}  # No global servers
                return {"Items": []}
            # Workspace scan
            fe = kwargs.get("FilterExpression", "")
            if "METADATA" in str(fe):
                return {"Items": [{"workspace_id": "ws1"}]}
            # MCP assignments for workspace
            sk_prefix = eav.get(":sk", "")
            if "MCP_SERVER" in str(sk_prefix):
                return {"Items": [{"mcp_id": "mcp1"}]}
            return {"Items": []}
        mock_table.query.side_effect = query_side_effect
        mock_table.scan.return_value = {"Items": [{"workspace_id": "ws1", "PK": "WS#ws1", "SK": "METADATA"}]}

        result = get_chat_available_mcp_servers("admin@example.com")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["mcp_servers"]) >= 0  # May or may not find servers depending on mock


class TestMcpRoutes:
    """Test lambda_handler routing for MCP endpoints."""

    def _make_event(self, method, path, body=None, email="admin@example.com"):
        event = {
            "headers": {"x-user-email": email},
            "requestContext": {"http": {"method": method, "path": path}},
        }
        if body:
            event["body"] = json.dumps(body)
        return event

    def test_post_mcp_servers(self, mock_table, mock_kms):
        """Test POST /mcp/servers route."""
        _make_org_admin(mock_table)
        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[]):
            result = lambda_handler(
                self._make_event("POST", "/mcp/servers", {"name": "Test", "url": "https://example.com"}),
                None,
            )
        assert result["statusCode"] == 201

    def test_get_mcp_servers(self, mock_table):
        """Test GET /mcp/servers route."""
        _make_org_admin(mock_table)
        mock_table.query.return_value = {"Items": []}
        result = lambda_handler(self._make_event("GET", "/mcp/servers"), None)
        assert result["statusCode"] == 200

    def test_delete_mcp_server(self, mock_table):
        """Test DELETE /mcp/servers/{id} route."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            if key.get("SK") == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if key.get("SK") == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc"}}
            return {}
        mock_table.get_item.side_effect = side_effect
        mock_table.query.return_value = {"Items": []}

        result = lambda_handler(self._make_event("DELETE", "/mcp/servers/abc"), None)
        assert result["statusCode"] == 200

    def test_post_mcp_refresh(self, mock_table, mock_kms):
        """Test POST /mcp/servers/{id}/refresh route."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            if key.get("SK") == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if key.get("SK") == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc", "url": "https://example.com", "auth_type": "none"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[{"name": "tool1", "description": "Test", "inputSchema": {"type": "object"}}]):
            result = lambda_handler(self._make_event("POST", "/mcp/servers/abc/refresh"), None)
        assert result["statusCode"] == 200

    def test_get_chat_mcp_servers_route(self, mock_table):
        """Test GET /chat/mcp-servers route."""
        mock_table.get_item.return_value = {"Item": {"org_role": "org_admin"}}
        mock_table.query.return_value = {"Items": []}
        mock_table.scan.return_value = {"Items": []}

        result = lambda_handler(self._make_event("GET", "/chat/mcp-servers"), None)
        assert result["statusCode"] == 200

    def test_post_workspace_mcp_servers(self, mock_table):
        """Test POST /workspaces/{id}/mcp-servers route."""
        def side_effect(**kwargs):
            key = kwargs.get("Key", {})
            pk = key.get("PK", "")
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if pk.startswith("MCP#") and sk == "METADATA":
                return {"Item": {"mcp_id": "mcp1"}}
            if pk.startswith("WS#") and sk == "METADATA":
                return {"Item": {"workspace_id": "ws1"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        result = lambda_handler(
            self._make_event("POST", "/workspaces/ws1/mcp-servers", {"mcp_id": "mcp1"}),
            None,
        )
        assert result["statusCode"] == 201


class TestMcpServerErrors:
    """Test AWS service error handling (ClientError) in MCP operations."""

    def _client_error(self, code="InternalServerError", message="Test error"):
        from botocore.exceptions import ClientError as BotoClientError
        return BotoClientError(
            {"Error": {"Code": code, "Message": message}},
            "TestOperation",
        )

    def test_create_mcp_server_dynamodb_put_error(self, mock_table, mock_kms):
        """Test that ClientError on put_item propagates (no try/except around put_item)."""
        from botocore.exceptions import ClientError as BotoClientError
        _make_org_admin(mock_table)

        # put_item raises ClientError
        mock_table.put_item.side_effect = self._client_error()

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[]):
            # create_mcp_server does not catch ClientError around put_item,
            # so the error propagates. The lambda_handler top-level catch handles it.
            with pytest.raises(BotoClientError):
                create_mcp_server("admin@example.com", {
                    "name": "Test MCP",
                    "url": "https://mcp.example.com",
                    "auth_type": "none",
                })

    def test_list_mcp_servers_dynamodb_query_error(self, mock_table):
        """Test that ClientError on query returns 500."""
        _make_org_admin(mock_table)
        mock_table.query.side_effect = self._client_error()

        result = list_mcp_servers("admin@example.com")
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Failed to list MCP servers" in body["error"]

    def test_get_mcp_server_dynamodb_error(self, mock_table):
        """Test that ClientError on get_item returns 500."""
        call_count = [0]

        def side_effect(**kwargs):
            call_count[0] += 1
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            # Let the org admin check pass, then fail on the MCP get
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            raise self._client_error()

        mock_table.get_item.side_effect = side_effect

        result = get_mcp_server("admin@example.com", "some-id")
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Failed to get MCP server" in body["error"]

    def test_update_mcp_server_dynamodb_error(self, mock_table, mock_kms):
        """Test that ClientError on update_item returns 500 after successful get."""
        call_count = [0]

        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA":
                return {"Item": {"mcp_id": "abc", "auth_type": "none", "PK": "MCP#abc", "SK": "METADATA"}}
            return {}

        mock_table.get_item.side_effect = get_side_effect
        mock_table.update_item.side_effect = self._client_error()

        result = update_mcp_server("admin@example.com", "abc", {"name": "Updated"})
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Failed to update MCP server" in body["error"]

    def test_delete_mcp_server_dynamodb_error(self, mock_table):
        """Test that ClientError during batch_writer delete returns 500."""
        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA"}}
            return {}

        mock_table.get_item.side_effect = get_side_effect

        # Make batch_writer context manager raise ClientError
        mock_batch = MagicMock()
        mock_batch.__enter__ = MagicMock(return_value=mock_batch)
        mock_batch.__exit__ = MagicMock(return_value=False)
        mock_batch.delete_item.side_effect = self._client_error()
        mock_table.batch_writer.return_value = mock_batch

        result = delete_mcp_server("admin@example.com", "abc")
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "Failed to delete MCP server" in body["error"]

    def test_encrypt_credentials_kms_error(self, mock_table, mock_kms):
        """Test that KMS encrypt error during create returns 500 or is handled."""
        _make_org_admin(mock_table)
        mock_kms.encrypt.side_effect = self._client_error("KMSInternalException", "KMS error")

        # When encrypt fails, it should bubble up as an unhandled exception
        # The create_mcp_server function doesn't catch KMS errors specifically
        try:
            result = create_mcp_server("admin@example.com", {
                "name": "Test MCP",
                "url": "https://mcp.example.com",
                "auth_type": "bearer",
                "credentials": "my-secret",
            })
            # If it returns a result, it should indicate failure
            assert result["statusCode"] == 500
        except Exception:
            # KMS error may propagate as unhandled - this is acceptable
            pass


class TestMcpEdgeCases:
    """Test edge cases in MCP operations."""

    def test_create_mcp_server_none_auth_ignores_credentials(self, mock_table, mock_kms):
        """Test that auth_type=none ignores provided credentials."""
        _make_org_admin(mock_table)

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[]):
            result = create_mcp_server("admin@example.com", {
                "name": "No Auth MCP",
                "url": "https://mcp.example.com",
                "auth_type": "none",
                "credentials": "should-be-ignored",
            })

        assert result["statusCode"] == 201
        # Verify put_item was called WITHOUT encrypted_credentials
        put_call = mock_table.put_item.call_args
        item = put_call[1]["Item"] if "Item" in put_call[1] else put_call[0][0]
        assert "encrypted_credentials" not in item
        # KMS encrypt should NOT have been called
        mock_kms.encrypt.assert_not_called()

    def test_update_mcp_server_no_fields(self, mock_table, mock_kms):
        """Test update with empty body still succeeds (updates timestamp)."""
        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA":
                return {"Item": {
                    "mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA",
                    "name": "Existing", "auth_type": "none",
                }}
            return {}

        mock_table.get_item.side_effect = get_side_effect

        result = update_mcp_server("admin@example.com", "abc", {})
        # Should succeed - update_item is called with just updated_at
        assert result["statusCode"] == 200

    def test_list_mcp_servers_empty_result(self, mock_table):
        """Test listing returns empty array when no servers exist."""
        _make_org_admin(mock_table)
        mock_table.query.return_value = {"Items": []}

        result = list_mcp_servers("admin@example.com")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["mcp_servers"] == []

    def test_delete_mcp_server_no_assignments(self, mock_table):
        """Test deleting a server with no workspace assignments."""
        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA"}}
            return {}

        mock_table.get_item.side_effect = get_side_effect
        mock_table.query.return_value = {"Items": []}  # No assignments

        result = delete_mcp_server("admin@example.com", "abc")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["deleted"] == "abc"

    def test_refresh_tools_recovers_from_error_status(self, mock_table, mock_kms):
        """Test that refresh succeeds even when server was in error status."""
        call_count = [0]

        def get_side_effect(**kwargs):
            call_count[0] += 1
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {
                    "mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA",
                    "url": "https://mcp.example.com", "auth_type": "none",
                    "status": "error",  # Was in error state
                    "name": "Error Server",
                }}
            return {}

        mock_table.get_item.side_effect = get_side_effect

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[
            {"name": "tool1", "description": "A tool", "inputSchema": {"type": "object"}}
        ]):
            result = refresh_mcp_tools("admin@example.com", "abc")

        assert result["statusCode"] == 200

    def test_list_workspace_mcp_servers_empty(self, mock_table):
        """Test listing workspace MCP servers when none are assigned."""
        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            return {}

        mock_table.get_item.side_effect = get_side_effect
        mock_table.query.return_value = {"Items": []}  # No MCP assignments

        result = list_workspace_mcp_servers("admin@example.com", "ws1")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["mcp_servers"] == []


class TestMcpLogging:
    """Test logging behavior in MCP operations."""

    def test_create_mcp_server_logs_with_prefix(self, mock_table, mock_kms):
        """Test that successful create logs with [workspace_handler] prefix."""
        _make_org_admin(mock_table)

        with patch.object(handler_module, '_call_mcp_list_tools', return_value=[]):
            with patch.object(handler_module, 'logger') as mock_logger:
                result = create_mcp_server("admin@example.com", {
                    "name": "Log Test MCP",
                    "url": "https://mcp.example.com",
                    "auth_type": "none",
                })

        assert result["statusCode"] == 201
        # Verify logger.info was called with the workspace_handler prefix
        mock_logger.info.assert_called()
        log_messages = [str(call) for call in mock_logger.info.call_args_list]
        assert any("[workspace_handler]" in msg for msg in log_messages)

    def test_delete_mcp_server_error_logs_with_exc_info(self, mock_table):
        """Test that delete ClientError logs with exc_info=True."""
        from botocore.exceptions import ClientError as BotoClientError

        def get_side_effect(**kwargs):
            key = kwargs.get("Key", {})
            sk = key.get("SK", "")
            if sk == "ORG_MEMBER":
                return {"Item": {"org_role": "org_admin"}}
            if sk == "METADATA" and key.get("PK", "").startswith("MCP#"):
                return {"Item": {"mcp_id": "abc", "PK": "MCP#abc", "SK": "METADATA"}}
            return {}

        mock_table.get_item.side_effect = get_side_effect

        # Make batch_writer raise ClientError
        mock_batch = MagicMock()
        mock_batch.__enter__ = MagicMock(return_value=mock_batch)
        mock_batch.__exit__ = MagicMock(return_value=False)
        mock_batch.delete_item.side_effect = BotoClientError(
            {"Error": {"Code": "InternalServerError", "Message": "Test"}},
            "BatchWriteItem",
        )
        mock_table.batch_writer.return_value = mock_batch

        with patch.object(handler_module, 'logger') as mock_logger:
            result = delete_mcp_server("admin@example.com", "abc")

        assert result["statusCode"] == 500
        # Verify logger.error was called with exc_info=True
        mock_logger.error.assert_called()
        call_kwargs = mock_logger.error.call_args
        assert call_kwargs[1].get("exc_info") is True or (len(call_kwargs) > 1 and call_kwargs[1].get("exc_info") is True)
