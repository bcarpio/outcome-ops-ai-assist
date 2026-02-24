"""Unit tests for outlook-integration Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_handler_module():
    """Load or reload the outlook-integration handler module."""
    if 'outlook_integration_handler' in sys.modules:
        del sys.modules['outlook_integration_handler']

    # Add outlook-integration dir to path so handler can resolve its sibling imports
    outlook_dir = os.path.join(os.path.dirname(__file__), '../../outlook-integration')
    if outlook_dir not in sys.path:
        sys.path.insert(0, outlook_dir)

    handler_path = os.path.join(outlook_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("outlook_integration_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['outlook_integration_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
get_authorize_url = handler_module.get_authorize_url
handle_callback = handler_module.handle_callback
list_connections = handler_module.list_connections
get_connection = handler_module.get_connection
list_folders = handler_module.list_folders
update_folders = handler_module.update_folders
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
        "callback_url": "https://example.com/api/outlook/callback",
    }


@pytest.fixture
def sample_connection():
    """Sample Outlook connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "OUTLOOK_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "user_email": "user@example.com",
        "display_name": "Test User",
        "selected_folders": [
            {"id": "folder1", "displayName": "Inbox"},
            {"id": "folder2", "displayName": "Sent Items"},
        ],
        "delta_links": {},
        "last_sync_status": "completed",
        "email_count": 42,
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
        assert json.loads(result["body"]) == {"key": "value"}

    def test_response_with_different_status_codes(self):
        """Test _response works with various status codes."""
        for code in [200, 400, 401, 403, 404, 500]:
            result = _response(code, {"status": "test"})
            assert result["statusCode"] == code

    def test_error_builds_error_response(self):
        """Test _error returns response with error field."""
        result = _error(400, "Bad request")
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "Bad request"

    def test_redirect_builds_302_response(self):
        """Test _redirect builds redirect response."""
        result = _redirect("https://example.com")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com"

    def test_get_user_from_request_extracts_email(self):
        """Test extracting user email from headers."""
        event = {"headers": {"x-user-email": "test@example.com"}}
        assert _get_user_from_request(event) == "test@example.com"

    def test_get_user_from_request_missing_headers(self):
        """Test returns None when headers missing."""
        assert _get_user_from_request({}) is None
        assert _get_user_from_request({"headers": {}}) is None

    def test_parse_path_splits_correctly(self):
        """Test path parsing into components."""
        assert _parse_path("/outlook/authorize") == ("outlook", "authorize")
        assert _parse_path("/workspaces/ws1/outlook/connections") == ("workspaces", "ws1", "outlook", "connections")
        assert _parse_path("//extra//slashes//") == ("extra", "slashes")

    def test_generate_id_returns_unique_ids(self):
        """Test that generate_id returns unique IDs."""
        ids = [generate_id() for _ in range(50)]
        assert len(set(ids)) == 50


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
        """Test generates valid Microsoft OAuth URL."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body

        auth_url = body["auth_url"]
        assert "login.microsoftonline.com" in auth_url
        assert "client_id=test-client-id" in auth_url
        assert "response_type=code" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_scopes_in_url(self, mock_config, oauth_config):
        """Test auth URL includes OAuth scopes."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "Mail.Read" in auth_url
        assert "offline_access" in auth_url

    @patch.object(handler_module, '_load_oauth_config')
    def test_includes_workspace_in_state(self, mock_config, oauth_config):
        """Test auth URL encodes workspace_id in state."""
        mock_config.return_value = oauth_config

        result = get_authorize_url("ws_test")
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "workspace_id" in auth_url


class TestHandleCallback:
    """Test OAuth callback handling."""

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_user_profile')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_success(self, mock_db, mock_config, mock_exchange, mock_profile, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test successful OAuth callback creates connection."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_profile.return_value = {"mail": "user@example.com", "displayName": "Test User"}

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
        mock_table.put_item.assert_called_once()
        mock_save.assert_called_once()

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

    @patch.object(handler_module, 'get_user_profile')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    def test_callback_api_error(self, mock_config, mock_exchange, mock_profile, oauth_config):
        """Test callback handles profile API error."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_profile.side_effect = handler_module.OutlookAPIError("API error")

        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws_test"}),
            }
        }

        result = handle_callback(event)
        body = json.loads(result["body"])
        assert "error=api_error" in body["redirect"]

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'get_user_profile')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_callback_url_encoded_state(self, mock_db, mock_config, mock_exchange, mock_profile, mock_save, oauth_config, mock_dynamodb, mock_table):
        """Test callback handles URL-encoded state parameter."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_token", "refresh_token", 3600)
        mock_profile.return_value = {"mail": "user@example.com", "displayName": "Test"}

        import urllib.parse
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


# ============================================================================
# Connection Operations
# ============================================================================

class TestListConnections:
    """Test listing Outlook connections."""

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
        assert "delta_links" not in conns[0]

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


class TestListFolders:
    """Test listing mail folders for a connection."""

    @patch.object(handler_module, 'list_mail_folders')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_folders_success(self, mock_get_conn, mock_config, mock_refresh, mock_list):
        """Test successfully listing mail folders."""
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_folders": [{"id": "f1"}],
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list.return_value = [
            {"id": "f1", "displayName": "Inbox", "totalItemCount": 100, "unreadItemCount": 5},
            {"id": "f2", "displayName": "Sent Items", "totalItemCount": 50, "unreadItemCount": 0},
        ]

        result = list_folders("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["folders"]) == 2
        # f1 should be marked as selected
        f1 = next(f for f in body["folders"] if f["id"] == "f1")
        assert f1["selected"] is True
        f2 = next(f for f in body["folders"] if f["id"] == "f2")
        assert f2["selected"] is False

    @patch.object(handler_module, 'get_connection')
    def test_list_folders_connection_not_found(self, mock_get_conn):
        """Test returns 404 when connection not found."""
        mock_get_conn.return_value = None

        result = list_folders("user@example.com", "ws_test", "bad_id")
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'get_connection')
    def test_list_folders_token_refresh_fails(self, mock_get_conn, mock_config, mock_refresh):
        """Test returns 401 when token refresh fails."""
        mock_get_conn.return_value = {"connection_id": "conn_test", "selected_folders": []}
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = None

        result = list_folders("user@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 401


class TestUpdateFolders:
    """Test updating selected folders."""

    @patch.object(handler_module, '_queue_sync')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_folders_success(self, mock_db, mock_get_conn, mock_queue, mock_dynamodb, mock_table):
        """Test updating folders triggers sync."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {"connection_id": "conn_test"}

        body = {"selected_folders": [{"id": "f1", "displayName": "Inbox"}]}
        handler_module.OUTLOOK_SYNC_QUEUE = "https://sqs.example.com/queue"

        result = update_folders("admin@example.com", "ws_test", "conn_test", body)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["selected_count"] == 1
        mock_queue.assert_called_once_with("ws_test", "conn_test")

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_folders_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test non-admin gets 403."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        result = update_folders("user@example.com", "ws_test", "conn_test", {})
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_folders_connection_not_found(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        result = update_folders("admin@example.com", "ws_test", "bad_id", {})
        assert result["statusCode"] == 404


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
            "selected_folders": [{"id": "f1"}],
        }

        result = trigger_sync("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "queued"
        mock_queue.assert_called_once()

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_trigger_sync_no_folders(self, mock_db, mock_get_conn, mock_dynamodb, mock_table):
        """Test trigger fails when no folders selected."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = {
            "connection_id": "conn_test",
            "selected_folders": [],
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


class TestDeleteConnection:
    """Test deleting an Outlook connection."""

    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_success(self, mock_db, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table, sample_connection):
        """Test deleting connection removes record and tokens."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
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
    def test_delete_connection_queues_folder_deletion(self, mock_db, mock_sqs_fn, mock_get_conn, mock_del_tokens, mock_dynamodb, mock_table, sample_connection):
        """Test deleting connection queues folder deletion via SQS."""
        mock_db.return_value = mock_dynamodb
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        result = delete_connection("admin@example.com", "ws_test", "conn_test")
        assert result["statusCode"] == 200
        # Should queue deletion for each folder
        assert mock_sqs_client.send_message.call_count == 2


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
        handler_module.OUTLOOK_SYNC_QUEUE = "https://sqs.example.com/queue"

        _queue_sync("ws_test", "conn_test")
        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args
        msg_body = json.loads(call_args[1]["MessageBody"])
        assert msg_body["workspace_id"] == "ws_test"
        assert msg_body["connection_id"] == "conn_test"

    def test_queue_sync_no_queue_configured(self):
        """Test no-op when queue not configured."""
        handler_module.OUTLOOK_SYNC_QUEUE = None
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


# ============================================================================
# Lambda Handler Routing
# ============================================================================

class TestLambdaHandlerRouting:
    """Test main handler routes requests correctly."""

    @patch.object(handler_module, 'get_authorize_url')
    def test_routes_authorize(self, mock_fn):
        """Test routing GET /outlook/authorize."""
        mock_fn.return_value = _response(200, {"auth_url": "https://..."})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/outlook/authorize"}},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = lambda_handler(event, None)
        mock_fn.assert_called_once_with("ws_test")

    def test_authorize_missing_workspace_id(self):
        """Test authorize returns 400 without workspace_id."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/outlook/authorize"}},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_get(self, mock_fn):
        """Test routing GET /outlook/callback."""
        mock_fn.return_value = _response(200, {"redirect": "/"})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/outlook/callback"}},
            "queryStringParameters": {"code": "abc", "state": "{}"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_post(self, mock_fn):
        """Test routing POST /outlook/callback."""
        mock_fn.return_value = _response(200, {"redirect": "/"})

        event = {
            "requestContext": {"http": {"method": "POST", "path": "/outlook/callback"}},
            "body": json.dumps({"code": "abc", "state": "{}"}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    def test_authenticated_route_requires_email(self):
        """Test authenticated routes return 401 without user email."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/outlook/connections"}},
            "headers": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'list_connections')
    def test_routes_list_connections(self, mock_fn):
        """Test routing GET /workspaces/{id}/outlook/connections."""
        mock_fn.return_value = _response(200, {"connections": []})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/outlook/connections"}},
            "headers": {"x-user-email": "user@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("user@example.com", "ws1")

    @patch.object(handler_module, 'list_folders')
    def test_routes_list_folders(self, mock_fn):
        """Test routing GET /workspaces/{id}/outlook/connections/{connId}/folders."""
        mock_fn.return_value = _response(200, {"folders": []})

        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/outlook/connections/conn1/folders"}},
            "headers": {"x-user-email": "user@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("user@example.com", "ws1", "conn1")

    @patch.object(handler_module, 'update_folders')
    def test_routes_update_folders(self, mock_fn):
        """Test routing PUT /workspaces/{id}/outlook/connections/{connId}/folders."""
        mock_fn.return_value = _response(200, {"message": "Updated"})

        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/workspaces/ws1/outlook/connections/conn1/folders"}},
            "headers": {"x-user-email": "admin@example.com"},
            "body": json.dumps({"selected_folders": []}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'trigger_sync')
    def test_routes_trigger_sync(self, mock_fn):
        """Test routing POST /workspaces/{id}/outlook/connections/{connId}/sync."""
        mock_fn.return_value = _response(200, {"status": "queued"})

        event = {
            "requestContext": {"http": {"method": "POST", "path": "/workspaces/ws1/outlook/connections/conn1/sync"}},
            "headers": {"x-user-email": "admin@example.com"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once_with("admin@example.com", "ws1", "conn1")

    @patch.object(handler_module, 'delete_connection')
    def test_routes_delete_connection(self, mock_fn):
        """Test routing DELETE /workspaces/{id}/outlook/connections/{connId}."""
        mock_fn.return_value = _response(200, {"message": "Deleted"})

        event = {
            "requestContext": {"http": {"method": "DELETE", "path": "/workspaces/ws1/outlook/connections/conn1"}},
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
