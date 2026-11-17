# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for confluence-integration Lambda handler."""

import importlib.util
import json
import os
import sys
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_handler_module():
    """Load or reload the confluence-integration handler module."""
    if 'confluence_integration_handler' in sys.modules:
        del sys.modules['confluence_integration_handler']

    # Add confluence-integration dir to path so handler can resolve its sibling imports
    confluence_dir = os.path.join(os.path.dirname(__file__), '../../confluence-integration')
    if confluence_dir not in sys.path:
        sys.path.insert(0, confluence_dir)

    handler_path = os.path.join(confluence_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("confluence_integration_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['confluence_integration_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
get_authorize_url = handler_module.get_authorize_url
handle_callback = handler_module.handle_callback
list_connections = handler_module.list_connections
get_connection = handler_module.get_connection
list_spaces = handler_module.list_spaces
update_spaces = handler_module.update_spaces
trigger_sync = handler_module.trigger_sync
delete_connection = handler_module.delete_connection
_response = handler_module._response
_error = handler_module._error
_redirect = handler_module._redirect
_get_user_from_request = handler_module._get_user_from_request
_parse_path = handler_module._parse_path
_get_membership = handler_module._get_membership
_is_workspace_admin = handler_module._is_workspace_admin
generate_id = handler_module.generate_id
_queue_sync = handler_module._queue_sync


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_module_globals():
    """Reset cached module globals between tests."""
    handler_module._oauth_config = None
    handler_module._dynamodb = None
    handler_module._ssm = None
    handler_module._sqs = None
    yield


@pytest.fixture
def mock_table():
    """Create a mock DynamoDB table."""
    return MagicMock()


@pytest.fixture
def mock_dynamodb(mock_table):
    """Create a mock DynamoDB resource."""
    mock_db = MagicMock()
    mock_db.Table.return_value = mock_table
    return mock_db


@pytest.fixture
def oauth_config():
    """Sample OAuth configuration."""
    return {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "callback_url": "https://example.com/api/confluence/callback",
    }


@pytest.fixture
def sample_connection():
    """Sample Confluence connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "CONFLUENCE_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "cloud_id": "cloud-123",
        "site_name": "Test Site",
        "site_url": "https://test.atlassian.net",
        "selected_spaces": [
            {"id": "space1", "key": "TS", "name": "Test Space"},
            {"id": "space2", "key": "DS", "name": "Dev Space"},
        ],
        "include_attachments": False,
        "last_sync_status": "completed",
        "page_count": 42,
        "attachment_count": 5,
    }


# ============================================================================
# Helper Functions
# ============================================================================

class TestResponseHelpers:
    """Test response helper functions."""

    def test_response_builds_correct_format(self):
        """Test _response builds HTTP response with JSON body."""
        result = _response(200, {"key": "value"})
        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "application/json"
        assert result["headers"]["Access-Control-Allow-Origin"] == "*"
        assert json.loads(result["body"]) == {"key": "value"}

    def test_response_with_different_status_codes(self):
        """Test _response works with various status codes."""
        for code in [200, 400, 401, 403, 404, 500]:
            result = _response(code, {"status": "test"})
            assert result["statusCode"] == code

    def test_response_serializes_datetime(self):
        """Test _response handles non-serializable types via default=str."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        result = _response(200, {"timestamp": now})
        body = json.loads(result["body"])
        assert "timestamp" in body

    def test_error_builds_error_response(self):
        """Test _error returns response with error field."""
        result = _error(400, "Bad request")
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "Bad request"

    def test_error_with_500(self):
        """Test _error with server error status."""
        result = _error(500, "Internal server error")
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "Internal server error"

    def test_redirect_builds_302_response(self):
        """Test _redirect builds redirect response."""
        result = _redirect("https://example.com")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com"
        assert result["body"] == ""

    def test_get_user_from_request_extracts_email(self):
        """Test extracting user email from headers."""
        event = {"headers": {"x-user-email": "test@example.com"}}
        assert _get_user_from_request(event) == "test@example.com"

    def test_get_user_from_request_missing_headers(self):
        """Test returns None when headers missing."""
        assert _get_user_from_request({}) is None
        assert _get_user_from_request({"headers": {}}) is None

    def test_get_user_from_request_none_headers(self):
        """Test returns None when headers key is None."""
        assert _get_user_from_request({"headers": None}) is None

    def test_parse_path_splits_correctly(self):
        """Test path parsing into components."""
        assert _parse_path("/confluence/authorize") == ("confluence", "authorize")
        assert _parse_path("/workspaces/ws1/confluence/connections") == (
            "workspaces", "ws1", "confluence", "connections"
        )
        assert _parse_path("//extra//slashes//") == ("extra", "slashes")

    def test_parse_path_with_deep_paths(self):
        """Test path parsing with connection sub-routes."""
        result = _parse_path("/workspaces/ws1/confluence/connections/conn1/spaces")
        assert result == ("workspaces", "ws1", "confluence", "connections", "conn1", "spaces")

    def test_parse_path_empty(self):
        """Test path parsing with empty/root path."""
        assert _parse_path("/") == ()
        assert _parse_path("") == ()

    def test_generate_id_returns_unique_ids(self):
        """Test that generate_id returns unique IDs."""
        ids = [generate_id() for _ in range(50)]
        assert len(set(ids)) == 50

    def test_generate_id_returns_string(self):
        """Test that generate_id returns a string."""
        result = generate_id()
        assert isinstance(result, str)
        assert len(result) > 0


# ============================================================================
# Workspace Helpers
# ============================================================================

class TestWorkspaceHelpers:
    """Test workspace membership helpers."""

    def test_get_membership_found(self, mock_table):
        """Test getting existing membership."""
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        result = _get_membership(mock_table, "user@example.com", "ws_test")
        assert result["role"] == "workspace_admin"
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws_test"}
        )

    def test_get_membership_not_found(self, mock_table):
        """Test returns None when not a member."""
        mock_table.get_item.return_value = {}
        result = _get_membership(mock_table, "user@example.com", "ws_test")
        assert result is None

    def test_get_membership_client_error(self, mock_table):
        """Test returns None on DynamoDB error."""
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "GetItem"
        )
        result = _get_membership(mock_table, "user@example.com", "ws_test")
        assert result is None

    def test_is_workspace_admin_true(self, mock_table):
        """Test admin check returns True for admin."""
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        assert _is_workspace_admin(mock_table, "admin@example.com", "ws_test") is True

    def test_is_workspace_admin_false_for_member(self, mock_table):
        """Test admin check returns False for regular member."""
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        assert _is_workspace_admin(mock_table, "user@example.com", "ws_test") is False

    def test_is_workspace_admin_false_when_not_member(self, mock_table):
        """Test admin check returns False when not a member."""
        mock_table.get_item.return_value = {}
        assert _is_workspace_admin(mock_table, "stranger@example.com", "ws_test") is False


# ============================================================================
# OAuth Flow
# ============================================================================

class TestGetAuthorizeUrl:
    """Test OAuth authorization URL generation."""

    @patch.object(handler_module, '_load_oauth_config')
    def test_generates_auth_url(self, mock_config, oauth_config):
        """Test generates valid Atlassian OAuth URL."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body

        auth_url = body["auth_url"]
        assert "auth.atlassian.com/authorize" in auth_url
        assert "client_id=test-client-id" in auth_url
        assert "response_type=code" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_scopes_in_url(self, mock_config, oauth_config):
        """Test auth URL includes Confluence OAuth scopes."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        # URL-encoded scopes should be present
        assert "offline_access" in auth_url
        assert "confluence" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_workspace_in_state(self, mock_config, oauth_config):
        """Test auth URL encodes workspace_id in state."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "workspace_id" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_audience_param(self, mock_config, oauth_config):
        """Test auth URL includes Atlassian audience parameter."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "audience=api.atlassian.com" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_consent_prompt(self, mock_config, oauth_config):
        """Test auth URL includes prompt=consent."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "prompt=consent" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_redirect_uri(self, mock_config, oauth_config):
        """Test auth URL includes the callback URL."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert urllib.parse.quote("https://example.com/api/confluence/callback", safe='') in auth_url


class TestHandleCallback:
    """Test OAuth callback handling."""

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_success(self, mock_db, mock_config, mock_exchange, mock_resources, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test successful OAuth callback creates connection."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = [
            {"id": "cloud-123", "name": "Test Site", "url": "https://test.atlassian.net"}
        ]

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "/workspaces/ws_test/settings" in body["redirect"]
        assert "configure=" in body["redirect"]
        mock_table.put_item.assert_called_once()
        mock_save.assert_called_once()

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_creates_correct_connection_record(self, mock_db, mock_config, mock_exchange, mock_resources, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test callback creates connection with correct fields."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = [
            {"id": "cloud-123", "name": "My Site", "url": "https://my.atlassian.net"}
        ]

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        handle_callback(event)

        put_call = mock_table.put_item.call_args
        item = put_call[1]["Item"] if "Item" in put_call[1] else put_call[0][0]
        assert item["workspace_id"] == "ws_test"
        assert item["cloud_id"] == "cloud-123"
        assert item["site_name"] == "My Site"
        assert item["site_url"] == "https://my.atlassian.net"
        assert item["selected_spaces"] == []
        assert item["include_attachments"] is False
        assert item["last_sync_status"] == "needs_configuration"
        assert item["page_count"] == 0
        assert item["attachment_count"] == 0
        assert item["PK"] == "WS#ws_test"
        assert item["SK"].startswith("CONFLUENCE_CONNECTION#")

    def test_callback_with_error_param(self):
        """Test callback handles OAuth error."""
        event = {
            "queryStringParameters": {"error": "access_denied"},
        }
        result = handle_callback(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "error=oauth_denied" in body["redirect"]

    def test_callback_missing_code(self):
        """Test callback handles missing code."""
        event = {
            "queryStringParameters": {"state": json.dumps({"workspace_id": "ws_test"})},
        }
        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=invalid_callback" in body.get("redirect", "")

    def test_callback_missing_state(self):
        """Test callback handles missing state."""
        event = {
            "queryStringParameters": {"code": "auth_code_123"},
        }
        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=invalid_callback" in body.get("redirect", "")

    def test_callback_missing_both_code_and_state(self):
        """Test callback handles missing code and state."""
        event = {
            "queryStringParameters": {},
        }
        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=invalid_callback" in body.get("redirect", "")

    def test_callback_no_query_params(self):
        """Test callback handles None queryStringParameters."""
        event = {
            "queryStringParameters": None,
        }
        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=invalid_callback" in body.get("redirect", "")

    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    def test_callback_token_exchange_fails(self, mock_config, mock_exchange, oauth_config):
        """Test callback handles failed token exchange."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = (None, None, None)

        event = {
            "queryStringParameters": {
                "code": "bad_code",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=token_exchange_failed" in body["redirect"]

    def test_callback_invalid_state_json(self):
        """Test callback handles invalid state JSON."""
        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": "not-json",
            }
        }
        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=invalid_state" in body.get("redirect", "")

    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    def test_callback_api_error(self, mock_config, mock_exchange, mock_resources, oauth_config):
        """Test callback handles accessible resources API error."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.side_effect = handler_module.ConfluenceAPIError("API error")

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=api_error" in body["redirect"]

    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    def test_callback_no_confluence_access(self, mock_config, mock_exchange, mock_resources, oauth_config):
        """Test callback handles no accessible Confluence resources."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = []

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=no_confluence_access" in body["redirect"]

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_url_encoded_state(self, mock_db, mock_config, mock_exchange, mock_resources, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test callback handles URL-encoded state parameter."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = [
            {"id": "cloud-123", "name": "Test", "url": "https://test.atlassian.net"}
        ]

        state = urllib.parse.quote(json.dumps({"workspace_id": "ws_test"}))

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": state,
            }
        }

        result = handle_callback(event)
        assert result["statusCode"] == 200
        mock_table.put_item.assert_called_once()

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_from_post_body(self, mock_db, mock_config, mock_exchange, mock_resources, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test callback accepts parameters from body (POST)."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = [
            {"id": "cloud-123", "name": "Test", "url": "https://test.atlassian.net"}
        ]

        event = {"queryStringParameters": {}}
        body = {
            "code": "auth_code_123",
            "state": json.dumps({"workspace_id": "ws_test"}),
        }

        result = handle_callback(event, body)
        assert result["statusCode"] == 200
        mock_table.put_item.assert_called_once()

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_database_error(self, mock_db, mock_config, mock_exchange, mock_resources, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test callback handles DynamoDB put_item failure."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_resources.return_value = [
            {"id": "cloud-123", "name": "Test", "url": "https://test.atlassian.net"}
        ]
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "PutItem"
        )

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=database_error" in body["redirect"]

    def test_callback_error_from_body(self):
        """Test callback handles OAuth error from body parameter."""
        event = {"queryStringParameters": {}}
        body = {"error": "access_denied"}

        result = handle_callback(event, body)
        assert result["statusCode"] == 200
        body_resp = json.loads(result["body"])
        assert "error=oauth_denied" in body_resp["redirect"]

    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    def test_callback_uses_first_resource(self, mock_config, mock_exchange, mock_resources, oauth_config, mock_dynamodb, mock_table):
        """Test callback uses the first accessible Confluence site."""
        with patch.object(handler_module, '_get_dynamodb', return_value=mock_dynamodb):
            with patch.object(handler_module, 'save_tokens'):
                mock_config.return_value = oauth_config
                mock_exchange.return_value = ("access_token", "refresh_token", 3600)
                mock_resources.return_value = [
                    {"id": "cloud-first", "name": "First Site", "url": "https://first.atlassian.net"},
                    {"id": "cloud-second", "name": "Second Site", "url": "https://second.atlassian.net"},
                ]

                event = {
                    "queryStringParameters": {
                        "code": "auth_code_123",
                        "state": json.dumps({"workspace_id": "ws_test"}),
                    }
                }

                handle_callback(event)

                put_call = mock_table.put_item.call_args
                item = put_call[1]["Item"] if "Item" in put_call[1] else put_call[0][0]
                assert item["cloud_id"] == "cloud-first"
                assert item["site_name"] == "First Site"


# ============================================================================
# Connection Operations
# ============================================================================

class TestListConnections:
    """Test listing Confluence connections."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_success(self, mock_db, mock_dynamodb, mock_table, sample_connection):
        """Test listing connections returns sanitized data."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {"Items": [sample_connection.copy()]}

        result = list_connections("user@example.com", "ws_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        conns = body["connections"]
        assert len(conns) == 1
        # Internal fields should be stripped
        assert "PK" not in conns[0]
        assert "SK" not in conns[0]
        # Business fields should remain
        assert conns[0]["connection_id"] == "conn_test"
        assert conns[0]["cloud_id"] == "cloud-123"

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test non-member gets 403."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = list_connections("stranger@example.com", "ws_test")
        assert result["statusCode"] == 403

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_empty(self, mock_db, mock_dynamodb, mock_table):
        """Test empty connections list."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {"Items": []}

        result = list_connections("user@example.com", "ws_test")
        body = json.loads(result["body"])
        assert body["connections"] == []

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_query_uses_correct_keys(self, mock_db, mock_dynamodb, mock_table):
        """Test query uses correct PK and SK prefix."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {"Items": []}

        list_connections("user@example.com", "ws_test")

        query_call = mock_table.query.call_args
        expr_values = query_call[1]["ExpressionAttributeValues"]
        assert expr_values[":pk"] == "WS#ws_test"
        assert expr_values[":sk"] == "CONFLUENCE_CONNECTION#"

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_dynamo_error(self, mock_db, mock_dynamodb, mock_table):
        """Test handles DynamoDB query error."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "Query"
        )

        result = list_connections("user@example.com", "ws_test")
        assert result["statusCode"] == 500

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_multiple(self, mock_db, mock_dynamodb, mock_table):
        """Test listing multiple connections."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_table.query.return_value = {
            "Items": [
                {"PK": "WS#ws_test", "SK": "CONFLUENCE_CONNECTION#c1", "connection_id": "c1"},
                {"PK": "WS#ws_test", "SK": "CONFLUENCE_CONNECTION#c2", "connection_id": "c2"},
            ]
        }

        result = list_connections("user@example.com", "ws_test")
        body = json.loads(result["body"])
        assert len(body["connections"]) == 2


class TestGetConnection:
    """Test getting a single connection."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_found(self, mock_db, mock_dynamodb, mock_table, sample_connection):
        """Test returns connection item when found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},       # membership check
            {"Item": sample_connection},          # connection lookup
        ]

        result = get_connection("user@example.com", "ws_test", "conn_test")
        assert result is not None
        assert result["connection_id"] == "conn_test"

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test returns None when user is not a member."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}  # no membership

        result = get_connection("stranger@example.com", "ws_test", "conn_test")
        assert result is None

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_not_found(self, mock_db, mock_dynamodb, mock_table):
        """Test returns None when connection does not exist."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},  # membership check
            {},                             # no connection
        ]

        result = get_connection("user@example.com", "ws_test", "nonexistent")
        assert result is None

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_dynamo_error(self, mock_db, mock_dynamodb, mock_table):
        """Test returns None on DynamoDB error."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            ClientError({"Error": {"Code": "500", "Message": "err"}}, "GetItem"),
        ]

        result = get_connection("user@example.com", "ws_test", "conn_test")
        assert result is None


# ============================================================================
# Space Operations
# ============================================================================

class TestListSpaces:
    """Test listing available Confluence spaces."""

    @patch.object(handler_module, 'list_all_spaces')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_success(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test successfully listing Confluence spaces."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-123",
            "selected_spaces": [{"id": "s1", "key": "TS", "name": "Test"}],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list.return_value = [
            {"id": "s1", "key": "TS", "name": "Test Space", "type": "global"},
            {"id": "s2", "key": "DS", "name": "Dev Space", "type": "global"},
        ]

        result = list_spaces("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["spaces"]) == 2
        assert body["selected_count"] == 1
        # s1 should be marked as selected
        s1 = next(s for s in body["spaces"] if s["id"] == "s1")
        assert s1["selected"] is True
        s2 = next(s for s in body["spaces"] if s["id"] == "s2")
        assert s2["selected"] is False

    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_connection_not_found(self, mock_get_conn):
        """Test returns 404 when connection not found."""
        mock_get_conn.return_value = None

        result = list_spaces("user@example.com", "ws_test", "bad_id")
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_token_refresh_fails(self, mock_get_conn, mock_config, mock_refresh):
        """Test returns 401 when token refresh fails."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-123",
            "selected_spaces": [],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = None

        result = list_spaces("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 401
        body = json.loads(result["body"])
        assert "reconnect" in body["error"].lower()

    @patch.object(handler_module, 'list_all_spaces')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_auth_error(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test returns 401 on Confluence auth error."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-123",
            "selected_spaces": [],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list.side_effect = handler_module.ConfluenceAuthError("Auth failed")

        result = list_spaces("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'list_all_spaces')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_api_error(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test returns 500 on Confluence API error."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-123",
            "selected_spaces": [],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list.side_effect = handler_module.ConfluenceAPIError("API error")

        result = list_spaces("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'list_all_spaces')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_no_selected_spaces(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test lists spaces when none are selected."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-123",
            "selected_spaces": [],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list.return_value = [
            {"id": "s1", "key": "TS", "name": "Test"},
        ]

        result = list_spaces("user@example.com", "ws_test", "conn_test")
        body = json.loads(result["body"])
        assert body["selected_count"] == 0
        assert body["spaces"][0]["selected"] is False

    @patch.object(handler_module, 'list_all_spaces')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_spaces_calls_api_with_correct_cloud_id(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test passes correct cloud_id to list_all_spaces."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "cloud_id": "cloud-abc",
            "selected_spaces": [],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "fresh_token"
        mock_list.return_value = []

        list_spaces("user@example.com", "ws_test", "conn_test")
        mock_list.assert_called_once_with("fresh_token", "cloud-abc")


class TestUpdateSpaces:
    """Test updating selected spaces."""

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_success(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test updating spaces triggers sync."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}

        body = {
            "selected_spaces": [{"id": "s1", "key": "TS", "name": "Test Space"}],
            "include_attachments": True,
        }
        handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/queue"

        result = update_spaces("admin@example.com", "ws_test", "conn_test", body)
        assert result["statusCode"] == 200
        resp_body = json.loads(result["body"])
        assert resp_body["selected_count"] == 1
        mock_queue.assert_called_once_with("ws_test", "conn_test")

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test non-admin gets 403."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        result = update_spaces("user@example.com", "ws_test", "conn_test", {})
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_connection_not_found(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        result = update_spaces("admin@example.com", "ws_test", "bad_id", {})
        assert result["statusCode"] == 404

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_empty_selection_no_sync(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test no sync queued when selected_spaces is empty."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}
        handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/queue"

        body = {"selected_spaces": []}
        result = update_spaces("admin@example.com", "ws_test", "conn_test", body)
        assert result["statusCode"] == 200
        mock_queue.assert_not_called()

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_no_queue_configured(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test no sync queued when CONFLUENCE_SYNC_QUEUE is not set."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}
        handler_module.CONFLUENCE_SYNC_QUEUE = None

        body = {"selected_spaces": [{"id": "s1", "key": "TS", "name": "Test"}]}
        result = update_spaces("admin@example.com", "ws_test", "conn_test", body)
        assert result["statusCode"] == 200
        mock_queue.assert_not_called()

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_dynamo_error(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test handles DynamoDB update error."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "UpdateItem"
        )

        body = {"selected_spaces": [{"id": "s1"}]}
        result = update_spaces("admin@example.com", "ws_test", "conn_test", body)
        assert result["statusCode"] == 500

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_sets_status_to_queued(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test update sets last_sync_status to queued."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}
        handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/queue"

        body = {"selected_spaces": [{"id": "s1"}]}
        update_spaces("admin@example.com", "ws_test", "conn_test", body)

        update_call = mock_table.update_item.call_args
        expr_values = update_call[1]["ExpressionAttributeValues"]
        assert expr_values[":status"] == "queued"

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_spaces_default_include_attachments(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test include_attachments defaults to False when not provided."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}
        handler_module.CONFLUENCE_SYNC_QUEUE = None

        body = {"selected_spaces": []}
        update_spaces("admin@example.com", "ws_test", "conn_test", body)

        update_call = mock_table.update_item.call_args
        expr_values = update_call[1]["ExpressionAttributeValues"]
        assert expr_values[":attach"] is False


# ============================================================================
# Sync Operations
# ============================================================================

class TestTriggerSync:
    """Test manual sync trigger."""

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_success(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test triggering sync queues job."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_spaces": [{"id": "s1"}],
        }

        result = trigger_sync("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "queued"
        assert body["message"] == "Sync queued"
        mock_queue.assert_called_once()

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_no_spaces(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test trigger fails when no spaces selected."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_spaces": [],
        }

        result = trigger_sync("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 400

    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test non-admin gets 403."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        result = trigger_sync("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_connection_not_found(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test trigger returns 404 when connection not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        result = trigger_sync("admin@example.com", "ws_test", "bad_id")
        assert result["statusCode"] == 404

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_dynamo_error(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test handles DynamoDB update error during sync trigger."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_spaces": [{"id": "s1"}],
        }
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "UpdateItem"
        )

        result = trigger_sync("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 500

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_updates_status(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test trigger updates last_sync_status to queued."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_spaces": [{"id": "s1"}],
        }

        trigger_sync("admin@example.com", "ws_test", "conn_test")

        update_call = mock_table.update_item.call_args
        expr_values = update_call[1]["ExpressionAttributeValues"]
        assert expr_values[":status"] == "queued"


# ============================================================================
# Delete Connection
# ============================================================================

class TestDeleteConnection:
    """Test deleting a Confluence connection."""

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_success(self, mock_db, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table, sample_connection):
        """Test deleting connection removes record and tokens."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        handler_module.DELETE_QUEUE = None  # No delete queue configured

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "Connection deleted"
        mock_table.delete_item.assert_called_once()
        mock_del_tokens.assert_called_once_with("conn_test")

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test non-admin gets 403."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        result = delete_connection("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_not_found(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test delete returns 404 when not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        result = delete_connection("admin@example.com", "ws_test", "bad_id")
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_sqs')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_queues_space_deletion(self, mock_db, mock_sqs_fn, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table, sample_connection):
        """Test deleting connection queues space deletion via SQS."""
        mock_db.return_value = mock_dynamodb
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        # Should queue deletion for each selected space (2 spaces in sample_connection)
        assert mock_sqs_client.send_message.call_count == 2

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_sqs')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_queue_message_format(self, mock_db, mock_sqs_fn, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table):
        """Test deletion queue messages have correct format."""
        mock_db.return_value = mock_dynamodb
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "workspace_id": "ws_test",
            "selected_spaces": [{"id": "s1", "key": "TS", "name": "Test Space"}],
        }
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        delete_connection("admin@example.com", "ws_test", "conn_test")

        call_args = mock_sqs_client.send_message.call_args
        msg = json.loads(call_args[1]["MessageBody"])
        assert msg["action"] == "delete_prefix"
        assert msg["workspace_id"] == "ws_test"
        assert msg["source"] == "confluence"
        assert msg["prefix"] == "Test Space/"

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_no_selected_spaces(self, mock_db, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table):
        """Test delete works when no spaces were selected (no SQS messages)."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "workspace_id": "ws_test",
            "selected_spaces": [],
        }
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        mock_del_tokens.assert_called_once_with("conn_test")

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_sqs')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_space_with_key_fallback(self, mock_db, mock_sqs_fn, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table):
        """Test delete falls back to space key when name is missing."""
        mock_db.return_value = mock_dynamodb
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "workspace_id": "ws_test",
            "selected_spaces": [{"id": "s1", "key": "TS"}],  # no "name" key
        }
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        delete_connection("admin@example.com", "ws_test", "conn_test")

        call_args = mock_sqs_client.send_message.call_args
        msg = json.loads(call_args[1]["MessageBody"])
        assert msg["prefix"] == "TS/"

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_dynamo_error(self, mock_db, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table):
        """Test handles DynamoDB delete error."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "workspace_id": "ws_test",
            "selected_spaces": [],
        }
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "DeleteItem"
        )
        handler_module.DELETE_QUEUE = None

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 500


# ============================================================================
# Queue Sync Helper
# ============================================================================

class TestQueueSync:
    """Test SQS sync queuing."""

    @patch.object(handler_module, '_get_sqs')
    def test_queue_sync_sends_message(self, mock_sqs_fn):
        """Test queuing a sync job sends SQS message."""
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/queue"

        _queue_sync("ws_test", "conn_test")
        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args
        assert call_args[1]["QueueUrl"] == "https://sqs.example.com/queue"
        msg_body = json.loads(call_args[1]["MessageBody"])
        assert msg_body["workspace_id"] == "ws_test"
        assert msg_body["connection_id"] == "conn_test"
        assert "queued_at" in msg_body

    def test_queue_sync_no_queue_configured(self):
        """Test no-op when queue not configured."""
        handler_module.CONFLUENCE_SYNC_QUEUE = None
        # Should not raise
        _queue_sync("ws_test", "conn_test")

    @patch.object(handler_module, '_get_sqs')
    def test_queue_sync_sqs_error(self, mock_sqs_fn):
        """Test handles SQS send error gracefully."""
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/queue"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "SQS error"}}, "SendMessage"
        )

        # Should not raise
        _queue_sync("ws_test", "conn_test")


# ============================================================================
# OAuth Config Loading
# ============================================================================

class TestLoadOAuthConfig:
    """Test loading OAuth config from SSM."""

    @patch.object(handler_module, '_get_ssm')
    def test_load_oauth_config_success(self, mock_ssm_fn):
        """Test loading OAuth config from SSM."""
        mock_ssm_client = MagicMock()
        mock_ssm_fn.return_value = mock_ssm_client
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "client-id-value"}},
            {"Parameter": {"Value": "client-secret-value"}},
            {"Parameter": {"Value": "https://example.com/callback"}},
        ]

        config = handler_module._load_oauth_config()
        assert config["client_id"] == "client-id-value"
        assert config["client_secret"] == "client-secret-value"
        assert config["callback_url"] == "https://example.com/callback"

    @patch.object(handler_module, '_get_ssm')
    def test_load_oauth_config_ssm_error(self, mock_ssm_fn):
        """Test raises on SSM error."""
        mock_ssm_client = MagicMock()
        mock_ssm_fn.return_value = mock_ssm_client
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

        with pytest.raises(ClientError):
            handler_module._load_oauth_config()

    @patch.object(handler_module, '_get_ssm')
    def test_load_oauth_config_caches_result(self, mock_ssm_fn):
        """Test config is cached after first load."""
        mock_ssm_client = MagicMock()
        mock_ssm_fn.return_value = mock_ssm_client
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "cid"}},
            {"Parameter": {"Value": "csec"}},
            {"Parameter": {"Value": "url"}},
        ]

        handler_module._load_oauth_config()
        handler_module._load_oauth_config()
        # Should only call SSM once (3 params)
        assert mock_ssm_client.get_parameter.call_count == 3

    @patch.object(handler_module, '_get_ssm')
    def test_load_oauth_config_uses_correct_ssm_paths(self, mock_ssm_fn):
        """Test SSM parameter paths use correct prefix."""
        mock_ssm_client = MagicMock()
        mock_ssm_fn.return_value = mock_ssm_client
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "cid"}},
            {"Parameter": {"Value": "csec"}},
            {"Parameter": {"Value": "url"}},
        ]

        handler_module._load_oauth_config()

        calls = mock_ssm_client.get_parameter.call_args_list
        # client-id (first call)
        assert "client-id" in calls[0][1]["Name"]
        assert "atlassian" in calls[0][1]["Name"]
        # client-secret (second call, with decryption)
        assert "client-secret" in calls[1][1]["Name"]
        assert calls[1][1]["WithDecryption"] is True
        # callback-url (third call)
        assert "callback-url" in calls[2][1]["Name"]


# ============================================================================
# Lambda Handler Routing
# ============================================================================

class TestLambdaHandlerRouting:
    """Test main handler routes requests correctly."""

    @patch.object(handler_module, '_is_workspace_admin', return_value=True)
    @patch.object(handler_module, 'get_authorize_url')
    def test_routes_authorize(self, mock_fn, mock_admin):
        """Test routing GET /confluence/authorize."""
        mock_fn.return_value = _response(200, {"auth_url": "https://..."})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/confluence/authorize"}},
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = lambda_handler(event, None)
        mock_fn.assert_called_once_with("ws_test")

    def test_authorize_missing_workspace_id(self):
        """Test authorize returns 401 without user email."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/confluence/authorize"}},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_authorize_none_query_params(self):
        """Test authorize returns 401 without user email."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/confluence/authorize"}},
            "queryStringParameters": None,
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_get(self, mock_fn):
        """Test routing GET /confluence/callback."""
        mock_fn.return_value = _response(200, {"redirect": "/"})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/confluence/callback"}},
            "queryStringParameters": {"code": "abc", "state": "{}"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_post(self, mock_fn):
        """Test routing POST /confluence/callback."""
        mock_fn.return_value = _response(200, {"redirect": "/"})

        event = {
            "requestContext": {"http": {"method": "POST", "path": "/confluence/callback"}},
            "body": json.dumps({"code": "abc", "state": "{}"}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    def test_authenticated_route_requires_email(self):
        """Test authenticated routes return 401 without user email."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/confluence/connections"}},
            "headers": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    def test_authenticated_route_no_headers(self):
        """Test authenticated routes return 401 when headers missing entirely."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/confluence/connections"}},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'list_connections')
    def test_routes_list_connections(self, mock_fn):
        """Test routing GET /workspaces/{id}/confluence/connections."""
        mock_fn.return_value = _response(200, {"connections": []})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/confluence/connections"}},
            "headers": {"x-user-email": "user@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("user@example.com", "ws1")

    @patch.object(handler_module, 'list_spaces')
    def test_routes_list_spaces(self, mock_fn):
        """Test routing GET /workspaces/{id}/confluence/connections/{connId}/spaces."""
        mock_fn.return_value = _response(200, {"spaces": []})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/confluence/connections/conn1/spaces"}},
            "headers": {"x-user-email": "user@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("user@example.com", "ws1", "conn1")

    @patch.object(handler_module, 'update_spaces')
    def test_routes_update_spaces(self, mock_fn):
        """Test routing PUT /workspaces/{id}/confluence/connections/{connId}/spaces."""
        mock_fn.return_value = _response(200, {"message": "Updated"})

        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/workspaces/ws1/confluence/connections/conn1/spaces"}},
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"selected_spaces": []}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'update_spaces')
    def test_routes_update_spaces_with_body(self, mock_fn):
        """Test update spaces receives parsed body."""
        mock_fn.return_value = _response(200, {"message": "Updated"})
        body_data = {"selected_spaces": [{"id": "s1"}], "include_attachments": True}

        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/workspaces/ws1/confluence/connections/conn1/spaces"}},
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps(body_data),
        }

        lambda_handler(event, None)
        call_args = mock_fn.call_args
        assert call_args[0] == ("admin@example.com", "ws1", "conn1", body_data)

    @patch.object(handler_module, 'trigger_sync')
    def test_routes_trigger_sync(self, mock_fn):
        """Test routing POST /workspaces/{id}/confluence/connections/{connId}/sync."""
        mock_fn.return_value = _response(200, {"status": "queued"})

        event = {
            "requestContext": {"http": {"method": "POST", "path": "/workspaces/ws1/confluence/connections/conn1/sync"}},
            "headers": {"x-user-email": "admin@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("admin@example.com", "ws1", "conn1")

    @patch.object(handler_module, 'delete_connection')
    def test_routes_delete_connection(self, mock_fn):
        """Test routing DELETE /workspaces/{id}/confluence/connections/{connId}."""
        mock_fn.return_value = _response(200, {"message": "Deleted"})

        event = {
            "requestContext": {"http": {"method": "DELETE", "path": "/workspaces/ws1/confluence/connections/conn1"}},
            "headers": {"x-user-email": "admin@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("admin@example.com", "ws1", "conn1")

    def test_unknown_route_returns_404(self):
        """Test unknown routes return 404."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/unknown/route"}},
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_invalid_body_json_handled(self):
        """Test handler handles invalid JSON body gracefully."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/unknown"}},
            "headers": {"x-user-email": "user@example.com"},
            "body": "not valid json",
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_routes_with_httpmethod_format(self):
        """Test handler supports httpMethod format (API Gateway v1)."""
        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/confluence/connections",
            "headers": {"x-user-email": "user@example.com"},
        }
        with patch.object(handler_module, 'list_connections', return_value=_response(200, {"connections": []})) as mock_fn:
            lambda_handler(event, None)
            mock_fn.assert_called_once_with("user@example.com", "ws1")

    def test_wrong_method_returns_404(self):
        """Test wrong HTTP method for a route returns 404."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/confluence/authorize"}},
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_delete_on_connections_list_returns_404(self):
        """Test DELETE on /workspaces/{id}/confluence/connections (no connId) returns 404."""
        event = {
            "requestContext": {"http": {"method": "DELETE", "path": "/workspaces/ws1/confluence/connections"}},
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_null_body_handled(self):
        """Test handler handles null body."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/unknown"}},
            "headers": {"x-user-email": "user@example.com"},
            "body": None,
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

