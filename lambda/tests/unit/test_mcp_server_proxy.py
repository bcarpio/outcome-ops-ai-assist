"""Unit tests for MCP proxy/fanout functionality in the OutcomeOps MCP server."""

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch

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
    if 'mcp_server_handler' in sys.modules:
        del sys.modules['mcp_server_handler']
    handler_path = os.path.join(os.path.dirname(__file__), '../../mcp-server/handler.py')
    spec = importlib.util.spec_from_file_location("mcp_server_handler", handler_path)
    mcp_module = importlib.util.module_from_spec(spec)
    sys.modules['mcp_server_handler'] = mcp_module
    spec.loader.exec_module(mcp_module)
    return mcp_module


mcp_module = _load_mcp_module()


@pytest.fixture(autouse=True)
def clear_caches():
    """Clear in-memory caches between tests."""
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()
    yield
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()


@pytest.fixture
def mock_dynamodb():
    """Mock DynamoDB resource."""
    table = MagicMock()
    with patch.object(mcp_module, '_get_dynamodb') as mock_ddb:
        mock_ddb.return_value.Table.return_value = table
        yield table


@pytest.fixture
def mock_ssm():
    """Mock SSM client."""
    client = MagicMock()
    with patch.object(mcp_module, '_get_ssm_client', return_value=client):
        yield client


def _make_handler():
    """Create a mock handler that captures _send_json calls."""
    handler = MagicMock()
    handler._responses = []

    def _capture_send_json(status, body, session_id=None):
        handler._responses.append({"status": status, "body": body})

    handler._send_json = _capture_send_json
    return handler


def _sonarqube_metadata():
    return {
        "Item": {
            "PK": "MCP#catalog-sonarqube",
            "SK": "METADATA",
            "mcp_id": "catalog-sonarqube",
            "name": "SonarQube",
            "url": "http://test-alb.example.com/mcp",
            "auth_type": "none",
            "ssm_token_path": "/test/mcp/sonarqube-token",
            "ssm_token_header": "SONARQUBE_TOKEN",
            "status": "active",
        }
    }


def _sonarqube_tools():
    return {
        "Item": {
            "PK": "MCP#catalog-sonarqube",
            "SK": "TOOLS",
            "tools": [
                {
                    "name": "search_issues",
                    "description": "Search for SonarQube issues",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "project_key": {"type": "string"},
                        },
                    },
                },
                {
                    "name": "get_metrics",
                    "description": "Get project metrics",
                    "inputSchema": {"type": "object", "properties": {}},
                },
            ],
        }
    }


class TestMcpServerDiscovery:
    """Test MCP server discovery from DynamoDB."""

    def test_discovers_workspace_assigned_servers(self, mock_dynamodb):
        """MCP servers assigned to user's workspaces are returned."""
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},  # WS query
            {"Items": []},  # global MCP query
        ]
        mock_dynamodb.get_item.side_effect = [
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]

        servers = mcp_module.discover_mcp_servers(["ws-1"])

        assert len(servers) == 1
        assert servers[0]["config"]["mcp_id"] == "catalog-sonarqube"
        assert len(servers[0]["tools"]) == 2

    def test_discovers_global_servers(self, mock_dynamodb):
        """Global MCP servers returned for all users."""
        # With empty workspace_ids, only the global query runs
        mock_dynamodb.query.return_value = {
            "Items": [{"mcp_id": "global-server", "is_global": True}]
        }
        mock_dynamodb.get_item.side_effect = [
            {"Item": {
                "mcp_id": "global-server", "name": "Global",
                "url": "http://test/mcp", "auth_type": "none",
                "status": "active",
            }},
            {"Item": {"tools": [{"name": "global_tool", "description": "A global tool"}]}},
        ]

        servers = mcp_module.discover_mcp_servers([])
        assert len(servers) == 1
        assert servers[0]["config"]["name"] == "Global"

    def test_no_servers_for_empty_workspaces(self, mock_dynamodb):
        """User with no workspaces gets no proxied servers (except global)."""
        mock_dynamodb.query.return_value = {"Items": []}

        servers = mcp_module.discover_mcp_servers([])
        assert len(servers) == 0

    def test_deduplicates_servers(self, mock_dynamodb):
        """Server in multiple workspaces only returned once."""
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},  # ws-1
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},  # ws-2 (same server)
            {"Items": []},  # global
        ]
        mock_dynamodb.get_item.side_effect = [
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]

        servers = mcp_module.discover_mcp_servers(["ws-1", "ws-2"])
        assert len(servers) == 1

    def test_skips_errored_servers(self, mock_dynamodb):
        """Servers with status=error are excluded."""
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "broken-server"}]},
            {"Items": []},
        ]
        mock_dynamodb.get_item.return_value = {
            "Item": {
                "mcp_id": "broken-server", "name": "Broken",
                "status": "error",
            }
        }

        servers = mcp_module.discover_mcp_servers(["ws-1"])
        assert len(servers) == 0

    def test_cache_ttl(self, mock_dynamodb):
        """Cached results expire after TTL."""
        mcp_module._mcp_server_cache["catalog-sonarqube"] = {
            "tools": [{"name": "old_tool"}],
            "config": {"mcp_id": "catalog-sonarqube", "name": "SonarQube", "url": "http://test"},
            "fetched_at": time.time() - 400,  # expired
        }

        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},
            {"Items": []},
        ]
        mock_dynamodb.get_item.side_effect = [
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]

        servers = mcp_module.discover_mcp_servers(["ws-1"])
        assert len(servers[0]["tools"]) == 2

    def test_cache_hit(self, mock_dynamodb):
        """Cached results returned within TTL."""
        mcp_module._mcp_server_cache["catalog-sonarqube"] = {
            "tools": [{"name": "cached_tool"}],
            "config": {"mcp_id": "catalog-sonarqube", "name": "SonarQube", "url": "http://test"},
            "fetched_at": time.time(),  # fresh
        }

        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},
            {"Items": []},
        ]

        servers = mcp_module.discover_mcp_servers(["ws-1"])
        assert len(servers[0]["tools"]) == 1
        assert servers[0]["tools"][0]["name"] == "cached_tool"
        mock_dynamodb.get_item.assert_not_called()


class TestToolNamespacing:
    """Test tool namespacing for proxied tools."""

    def test_native_tools_not_namespaced(self, mock_dynamodb):
        """OutcomeOps native tools keep original names."""
        session_id = "test-session-123"
        mcp_module._sessions[session_id] = {"email": "user@example.com", "last_active": time.time()}

        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": []}
        mock_dynamodb.scan.return_value = {"Items": []}

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_list(handler, 1, session_id)

        response = handler._responses[0]["body"]
        tool_names = [t["name"] for t in response["result"]["tools"]]
        assert "query_knowledge_base" in tool_names
        assert "list_workspaces" in tool_names

    def test_proxied_tools_prefixed(self, mock_dynamodb):
        """Proxied tools prefixed with server_name__."""
        session_id = "test-session-456"
        mcp_module._sessions[session_id] = {"email": "user@example.com", "last_active": time.time()}

        mock_dynamodb.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]
        mock_dynamodb.scan.return_value = {"Items": [{"PK": "WS#ws-1"}]}
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},
            {"Items": []},
        ]

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_list(handler, 1, session_id)

        response = handler._responses[0]["body"]
        tool_names = [t["name"] for t in response["result"]["tools"]]
        assert "sonarqube__search_issues" in tool_names
        assert "sonarqube__get_metrics" in tool_names

    def test_description_includes_server_name(self, mock_dynamodb):
        """Proxied tool descriptions prefixed with [ServerName]."""
        session_id = "test-session-789"
        mcp_module._sessions[session_id] = {"email": "admin@example.com", "last_active": time.time()}

        mock_dynamodb.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]
        mock_dynamodb.scan.return_value = {"Items": [{"PK": "WS#ws-1"}]}
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},
            {"Items": []},
        ]

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_list(handler, 1, session_id)

        response = handler._responses[0]["body"]
        proxied = [t for t in response["result"]["tools"] if t["name"].startswith("sonarqube__")]
        for tool in proxied:
            assert tool["description"].startswith("[SonarQube]")

    def test_name_sanitization(self, mock_dynamodb):
        """Server names with spaces/hyphens sanitized."""
        session_id = "test-session-san"
        mcp_module._sessions[session_id] = {"email": "admin@example.com", "last_active": time.time()}

        mock_dynamodb.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},
            {"Item": {
                "mcp_id": "test-server", "name": "My Cool Server",
                "url": "http://test/mcp", "auth_type": "none", "status": "active",
            }},
            {"Item": {"tools": [{"name": "do_thing", "description": "Does a thing"}]}},
        ]
        mock_dynamodb.scan.return_value = {"Items": [{"PK": "WS#ws-1"}]}
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "test-server"}]},
            {"Items": []},
        ]

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_list(handler, 1, session_id)

        response = handler._responses[0]["body"]
        tool_names = [t["name"] for t in response["result"]["tools"]]
        assert "my_cool_server__do_thing" in tool_names


class TestToolCallRouting:
    """Test tool call routing for native and proxied tools."""

    def test_native_tool_call(self, mock_dynamodb):
        """Native tool calls handled locally."""
        session_id = "test-session-native"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
            "tool_routing": {},
        }

        handler = _make_handler()

        with patch.dict(mcp_module.TOOL_HANDLERS, {
            "list_workspaces": lambda args, email: {"text": "Mocked workspaces"},
        }):
            mcp_module.McpHandler._handle_tools_call(
                handler, 1, {"name": "list_workspaces", "arguments": {}},
                "user@example.com", session_id,
            )

        response = handler._responses[0]["body"]
        assert "result" in response
        assert response["result"]["content"][0]["text"] == "Mocked workspaces"

    def test_unknown_tool_returns_error(self, mock_dynamodb):
        """Unknown tool name returns JSON-RPC error."""
        session_id = "test-session-unknown"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
            "tool_routing": {},
        }

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_call(
            handler, 1, {"name": "nonexistent_tool", "arguments": {}},
            "user@example.com", session_id,
        )

        response = handler._responses[0]["body"]
        assert "error" in response
        assert "Unknown tool" in response["error"]["message"]

    def test_proxied_tool_call(self, mock_dynamodb):
        """Proxied tool calls forwarded to correct server."""
        session_id = "test-session-proxy"
        sonarqube_config = {
            "mcp_id": "catalog-sonarqube",
            "name": "SonarQube",
            "url": "http://test-alb/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
            "tool_routing": {
                "sonarqube__search_issues": {
                    "config": sonarqube_config,
                    "original_name": "search_issues",
                },
            },
        }

        handler = _make_handler()
        mock_result = {
            "content": [{"type": "text", "text": "Found 3 issues"}],
            "isError": False,
        }

        with patch.object(mcp_module, '_proxy_tool_call', return_value=mock_result) as mock_proxy:
            mcp_module.McpHandler._handle_tools_call(
                handler, 1,
                {"name": "sonarqube__search_issues", "arguments": {"project_key": "my-project"}},
                "user@example.com", session_id,
            )

            mock_proxy.assert_called_once_with(
                sonarqube_config,
                "search_issues",
                {"project_key": "my-project"},
            )

        response = handler._responses[0]["body"]
        assert response["result"]["content"][0]["text"] == "Found 3 issues"

    def test_proxy_error_returns_clean_error(self, mock_dynamodb):
        """Network errors from proxied server returned as MCP error."""
        session_id = "test-session-proxy-err"
        mcp_module._sessions[session_id] = {
            "email": "user@example.com",
            "last_active": time.time(),
            "tool_routing": {
                "sonarqube__search_issues": {
                    "config": {
                        "mcp_id": "catalog-sonarqube", "name": "SonarQube",
                        "url": "http://test-alb/mcp", "auth_type": "none",
                        "ssm_token_path": "", "ssm_token_header": "",
                        "encrypted_credentials": "",
                    },
                    "original_name": "search_issues",
                },
            },
        }

        handler = _make_handler()

        with patch.object(mcp_module, '_proxy_tool_call', side_effect=ConnectionError("Connection refused")):
            mcp_module.McpHandler._handle_tools_call(
                handler, 1,
                {"name": "sonarqube__search_issues", "arguments": {}},
                "user@example.com", session_id,
            )

        response = handler._responses[0]["body"]
        assert "error" in response
        assert "Proxy error" in response["error"]["message"]

    def test_auth_headers_applied(self, mock_ssm):
        """SSM tokens added to proxied requests."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "secret-token-123"}
        }

        config = {
            "mcp_id": "catalog-sonarqube",
            "auth_type": "none",
            "ssm_token_path": "/test/mcp/sonarqube-token",
            "ssm_token_header": "SONARQUBE_TOKEN",
            "encrypted_credentials": "",
        }

        headers = {"Content-Type": "application/json"}
        mcp_module._add_mcp_auth_headers(headers, config)

        assert headers["SONARQUBE_TOKEN"] == "secret-token-123"
        mock_ssm.get_parameter.assert_called_once_with(
            Name="/test/mcp/sonarqube-token", WithDecryption=True,
        )


class TestWorkspaceSharing:
    """Test workspace sharing in get_user_workspace_ids."""

    def test_direct_membership(self, mock_dynamodb):
        """User sees workspaces they're directly a member of."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {
            "Items": [{"workspace_id": "ws-1"}, {"workspace_id": "ws-2"}]
        }
        mock_dynamodb.scan.return_value = {"Items": [
            {"PK": "WS#ws-1"},
            {"PK": "WS#ws-2"},
        ]}

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert set(result) == {"ws-1", "ws-2"}

    def test_shared_workspace_access(self, mock_dynamodb):
        """User sees workspaces shared with their workspaces."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {
            "Items": [{"workspace_id": "ws-b"}]
        }
        mock_dynamodb.scan.return_value = {"Items": [
            {"PK": "WS#ws-b"},
            {"PK": "WS#ws-a", "search_allowed_workspaces": ["ws-b"]},
        ]}

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert set(result) == {"ws-a", "ws-b"}

    def test_org_admin_sees_all(self, mock_dynamodb):
        """Org admin sees all workspaces."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "org_admin"}}
        mock_dynamodb.scan.return_value = {
            "Items": [{"PK": "WS#ws-1"}, {"PK": "WS#ws-2"}, {"PK": "WS#ws-3"}]
        }

        result = mcp_module.get_user_workspace_ids("admin@example.com")
        assert set(result) == {"ws-1", "ws-2", "ws-3"}

    def test_no_transitive_sharing(self, mock_dynamodb):
        """Sharing is not transitive. A shares with B, B shares with C.
        User of C doesn't see A."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {
            "Items": [{"workspace_id": "ws-c"}]
        }
        mock_dynamodb.scan.return_value = {"Items": [
            {"PK": "WS#ws-c"},
            {"PK": "WS#ws-b", "search_allowed_workspaces": ["ws-c"]},
            {"PK": "WS#ws-a", "search_allowed_workspaces": ["ws-b"]},
        ]}

        result = mcp_module.get_user_workspace_ids("user@example.com")
        assert set(result) == {"ws-b", "ws-c"}

    def test_no_workspaces(self, mock_dynamodb):
        """User with no memberships gets empty list."""
        mock_dynamodb.get_item.return_value = {"Item": {"org_role": "member"}}
        mock_dynamodb.query.return_value = {"Items": []}

        result = mcp_module.get_user_workspace_ids("nobody@example.com")
        assert result == []


class TestProxySessionManagement:
    """Test session-related proxy behavior."""

    def test_tool_routing_stored_in_session(self, mock_dynamodb):
        """tools/list stores routing map in session."""
        session_id = "test-session-routing"
        mcp_module._sessions[session_id] = {"email": "user@example.com", "last_active": time.time()}

        mock_dynamodb.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},
            _sonarqube_metadata(),
            _sonarqube_tools(),
        ]
        mock_dynamodb.scan.return_value = {"Items": [{"PK": "WS#ws-1"}]}
        mock_dynamodb.query.side_effect = [
            {"Items": [{"mcp_id": "catalog-sonarqube"}]},
            {"Items": []},
        ]

        handler = _make_handler()
        mcp_module.McpHandler._handle_tools_list(handler, 1, session_id)

        session = mcp_module._sessions[session_id]
        assert "tool_routing" in session
        assert "sonarqube__search_issues" in session["tool_routing"]
        route = session["tool_routing"]["sonarqube__search_issues"]
        assert route["original_name"] == "search_issues"
        assert route["config"]["mcp_id"] == "catalog-sonarqube"

    def test_mcp_session_cache(self):
        """MCP sessions with internal servers are cached."""
        mcp_module._mcp_sessions["catalog-sonarqube"] = {
            "session_id": "cached-session-id",
            "created_at": time.time(),
        }

        config = {
            "mcp_id": "catalog-sonarqube",
            "name": "SonarQube",
            "url": "http://test-alb/mcp",
            "auth_type": "none",
            "ssm_token_path": "",
            "ssm_token_header": "",
            "encrypted_credentials": "",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "jsonrpc": "2.0", "id": 1,
            "result": {"content": [{"type": "text", "text": "OK"}]},
        }).encode()
        mock_response.headers = {}

        with patch('urllib.request.urlopen', return_value=mock_response) as mock_urlopen:
            mcp_module._proxy_tool_call(config, "search_issues", {"q": "test"})

        # Should use cached session ID, not re-initialize
        assert mock_urlopen.call_count == 1  # Only the tools/call, no initialize
        req = mock_urlopen.call_args[0][0]
        assert req.get_header("Mcp-session-id") == "cached-session-id"


class TestInitializeCapabilities:
    """Test that initialize reports correct capabilities."""

    def test_list_changed_true(self, mock_dynamodb):
        """listChanged capability is set to true."""
        mock_dynamodb.query.return_value = {
            "Items": [{
                "PK": "USER#user@example.com",
                "SK": "API_KEY#key-1",
                "status": "active",
            }]
        }
        mock_dynamodb.update_item.return_value = {}

        handler = _make_handler()
        handler.headers = {"Authorization": "Bearer ooa_test1234567890abcdef1234567890abcdef"}

        mcp_module.McpHandler._handle_initialize(handler, 1, {})

        response = handler._responses[0]["body"]
        assert response["result"]["capabilities"]["tools"]["listChanged"] is True
