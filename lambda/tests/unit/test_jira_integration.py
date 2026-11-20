# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for jira-integration Lambda handler."""

import importlib.util
import json
import os
import sys
import urllib.parse
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_handler_module():
    """Load or reload the jira-integration handler module."""
    # Clear any cached modules that might conflict with sibling imports
    for mod_name in ['jira_integration_handler', 'token_manager', 'jira_api']:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Add jira-integration dir to FRONT of path so its token_manager/jira_api are found first
    jira_dir = os.path.join(os.path.dirname(__file__), '../../jira-integration')
    if jira_dir in sys.path:
        sys.path.remove(jira_dir)
    sys.path.insert(0, jira_dir)

    handler_path = os.path.join(jira_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("jira_integration_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['jira_integration_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
handle_authorize = handler_module.handle_authorize
handle_callback = handler_module.handle_callback
handle_list_connections = handler_module.handle_list_connections
handle_list_projects = handler_module.handle_list_projects
handle_update_projects = handler_module.handle_update_projects
handle_sync = handler_module.handle_sync
handle_disconnect = handler_module.handle_disconnect
save_connection = handler_module.save_connection
get_connection = handler_module.get_connection
list_connections = handler_module.list_connections
delete_connection = handler_module.delete_connection
update_connection_projects = handler_module.update_connection_projects
update_sync_status = handler_module.update_sync_status
queue_sync_job = handler_module.queue_sync_job
queue_delete_job = handler_module.queue_delete_job
_response = handler_module._response
_error = handler_module._error
_redirect = handler_module._redirect
_get_user_from_request = handler_module._get_user_from_request
_is_workspace_admin = handler_module._is_workspace_admin
_can_access_workspace = handler_module._can_access_workspace
_parse_path = handler_module._parse_path
_generate_connection_id = handler_module._generate_connection_id


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_module_globals():
    """Reset cached module globals between tests."""
    handler_module._dynamodb = None
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
def mock_sqs():
    """Create a mock SQS client."""
    return MagicMock()


@pytest.fixture
def oauth_config():
    """Sample OAuth configuration."""
    return {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "callback_url": "https://example.com/api/jira/callback",
    }


@pytest.fixture
def sample_connection():
    """Sample Jira connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "JIRA_CONNECTION#jira_conn_test",
        "connection_id": "jira_conn_test",
        "workspace_id": "ws_test",
        "cloud_id": "cloud_abc123",
        "site_name": "Test Site",
        "site_url": "https://testsite.atlassian.net",
        "selected_projects": [
            {"id": "10001", "key": "PROJ", "name": "Test Project"},
        ],
        "include_attachments": True,
        "include_comments": True,
        "last_sync_at": "2026-01-01T00:00:00+00:00",
        "last_sync_status": "success",
        "last_sync_error": None,
        "issue_count": 42,
        "created_by": "admin@example.com",
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


@pytest.fixture
def sample_projects():
    """Sample list of Jira projects."""
    return [
        {"id": "10001", "key": "PROJ", "name": "Test Project", "projectTypeKey": "software"},
        {"id": "10002", "key": "OPS", "name": "Ops Project", "projectTypeKey": "service_desk"},
        {"id": "10003", "key": "DEV", "name": "Dev Project", "projectTypeKey": "software"},
    ]


@pytest.fixture
def sample_sites():
    """Sample list of Atlassian accessible resources."""
    return [
        {"id": "cloud_abc123", "name": "Test Site", "url": "https://testsite.atlassian.net"},
    ]


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
        for code in [200, 400, 401, 403, 404, 500, 502]:
            result = _response(code, {"status": "test"})
            assert result["statusCode"] == code

    def test_response_serializes_datetime(self):
        """Test _response handles datetime serialization via default=str."""
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

    def test_error_different_messages(self):
        """Test _error with different error messages."""
        result = _error(500, "Internal server error")
        body = json.loads(result["body"])
        assert body["error"] == "Internal server error"

    def test_redirect_builds_302_response(self):
        """Test _redirect builds redirect response."""
        result = _redirect("https://example.com/callback")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com/callback"
        assert result["body"] == ""


class TestGetUserFromRequest:
    """Test user extraction from request headers."""

    def test_get_user_from_request_lowercase_header(self):
        """Test extracting user email from lowercase header."""
        event = {"headers": {"x-user-email": "test@example.com"}}
        assert _get_user_from_request(event) == "test@example.com"

    def test_get_user_from_request_titlecase_header(self):
        """Test extracting user email from title-case header."""
        event = {"headers": {"X-User-Email": "test@example.com"}}
        assert _get_user_from_request(event) == "test@example.com"

    def test_get_user_from_request_missing_headers(self):
        """Test returns None when headers missing."""
        assert _get_user_from_request({}) is None

    def test_get_user_from_request_empty_headers(self):
        """Test returns None when headers dict is empty."""
        assert _get_user_from_request({"headers": {}}) is None

    def test_get_user_from_request_no_email_header(self):
        """Test returns None when email header is absent."""
        event = {"headers": {"content-type": "application/json"}}
        assert _get_user_from_request(event) is None


class TestParsePath:
    """Test path parsing."""

    def test_parse_path_simple(self):
        """Test parsing a simple path."""
        assert _parse_path("/jira/authorize") == ("jira", "authorize")

    def test_parse_path_workspaces(self):
        """Test parsing workspace-level path."""
        assert _parse_path("/workspaces/ws1/jira/connections") == ("workspaces", "ws1", "jira", "connections")

    def test_parse_path_deep(self):
        """Test parsing deep nested path."""
        result = _parse_path("/workspaces/ws1/jira/connections/conn1/projects")
        assert result == ("workspaces", "ws1", "jira", "connections", "conn1", "projects")

    def test_parse_path_trailing_slash(self):
        """Test parsing path with trailing slash."""
        result = _parse_path("/jira/authorize/")
        assert result == ("jira", "authorize")


class TestGenerateConnectionId:
    """Test connection ID generation."""

    def test_generate_connection_id_prefix(self):
        """Test connection ID starts with jira_ prefix."""
        conn_id = _generate_connection_id()
        assert conn_id.startswith("jira_")

    def test_generate_connection_id_uniqueness(self):
        """Test connection IDs are unique."""
        ids = [_generate_connection_id() for _ in range(50)]
        assert len(set(ids)) == 50


# ============================================================================
# Auth Helpers
# ============================================================================

class TestIsWorkspaceAdmin:
    """Test workspace admin check."""

    def test_is_workspace_admin_true_for_workspace_admin(self, mock_table):
        """Test returns True for workspace_admin role."""
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        assert _is_workspace_admin(mock_table, "admin@example.com", "ws_test") is True

    def test_is_workspace_admin_true_for_org_admin(self, mock_table):
        """Test returns True for org_admin role."""
        mock_table.get_item.return_value = {"Item": {"role": "org_admin"}}
        assert _is_workspace_admin(mock_table, "orgadmin@example.com", "ws_test") is True

    def test_is_workspace_admin_false_for_member(self, mock_table):
        """Test returns False for regular member role."""
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        assert _is_workspace_admin(mock_table, "user@example.com", "ws_test") is False

    def test_is_workspace_admin_false_for_viewer(self, mock_table):
        """Test returns False for viewer role."""
        mock_table.get_item.return_value = {"Item": {"role": "viewer"}}
        assert _is_workspace_admin(mock_table, "user@example.com", "ws_test") is False

    def test_is_workspace_admin_false_when_not_member(self, mock_table):
        """Test returns False when user is not a member."""
        mock_table.get_item.return_value = {}
        assert _is_workspace_admin(mock_table, "stranger@example.com", "ws_test") is False

    def test_is_workspace_admin_queries_correct_key(self, mock_table):
        """Test correct DynamoDB key is used."""
        mock_table.get_item.return_value = {}
        _is_workspace_admin(mock_table, "user@example.com", "ws_123")
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "USER#user@example.com", "SK": "WS_MEMBER#ws_123"}
        )


class TestCanAccessWorkspace:
    """Test workspace access check."""

    def test_can_access_workspace_direct_member(self, mock_table):
        """Test access granted for direct workspace member."""
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        assert _can_access_workspace(mock_table, "user@example.com", "ws_test") is True

    def test_can_access_workspace_org_admin(self, mock_table):
        """Test access granted for org admin even without direct membership."""
        # First call: no direct membership
        # Second call: org admin
        mock_table.get_item.side_effect = [
            {},
            {"Item": {"org_role": "org_admin"}},
        ]
        assert _can_access_workspace(mock_table, "orgadmin@example.com", "ws_test") is True

    def test_can_access_workspace_denied_no_membership(self, mock_table):
        """Test access denied when not a member and not org admin."""
        mock_table.get_item.side_effect = [
            {},
            {},
        ]
        assert not _can_access_workspace(mock_table, "stranger@example.com", "ws_test")

    def test_can_access_workspace_denied_non_admin_org_member(self, mock_table):
        """Test access denied for non-admin org member without direct membership."""
        mock_table.get_item.side_effect = [
            {},
            {"Item": {"org_role": "member"}},
        ]
        assert _can_access_workspace(mock_table, "user@example.com", "ws_test") is False

    def test_can_access_workspace_checks_direct_membership_first(self, mock_table):
        """Test direct membership is checked before org admin."""
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        _can_access_workspace(mock_table, "user@example.com", "ws_test")
        # Only one call needed if direct member
        mock_table.get_item.assert_called_once()


# ============================================================================
# DynamoDB Operations
# ============================================================================

class TestSaveConnection:
    """Test saving a Jira connection to DynamoDB."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_save_connection_puts_correct_item(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test save_connection writes correct DynamoDB item."""
        mock_db_fn.return_value = mock_dynamodb

        save_connection(
            connection_id="jira_abc",
            workspace_id="ws_test",
            cloud_id="cloud_123",
            site_name="My Site",
            site_url="https://mysite.atlassian.net",
            created_by="admin@example.com",
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws_test"
        assert item["SK"] == "JIRA_CONNECTION#jira_abc"
        assert item["connection_id"] == "jira_abc"
        assert item["workspace_id"] == "ws_test"
        assert item["cloud_id"] == "cloud_123"
        assert item["site_name"] == "My Site"
        assert item["site_url"] == "https://mysite.atlassian.net"
        assert item["selected_projects"] == []
        assert item["include_attachments"] is True
        assert item["include_comments"] is True
        assert item["last_sync_at"] is None
        assert item["last_sync_status"] == "needs_configuration"
        assert item["issue_count"] == 0
        assert item["created_by"] == "admin@example.com"
        assert "created_at" in item
        assert "updated_at" in item


class TestGetConnection:
    """Test getting a Jira connection from DynamoDB."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_found(self, mock_db_fn, mock_dynamodb, mock_table, sample_connection):
        """Test getting an existing connection."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": sample_connection}

        result = get_connection("ws_test", "jira_conn_test")
        assert result == sample_connection
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "JIRA_CONNECTION#jira_conn_test"}
        )

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_connection_not_found(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when connection not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_connection("ws_test", "jira_nonexistent")
        assert result is None


class TestListConnections:
    """Test listing Jira connections from DynamoDB."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_returns_items(self, mock_db_fn, mock_dynamodb, mock_table, sample_connection):
        """Test listing connections returns items."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": [sample_connection]}

        result = list_connections("ws_test")
        assert len(result) == 1
        assert result[0]["connection_id"] == "jira_conn_test"

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_empty(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test listing returns empty list when no connections."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": []}

        result = list_connections("ws_test")
        assert result == []

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_connections_query_params(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test correct query key expression is used."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": []}

        list_connections("ws_test")
        call_kwargs = mock_table.query.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":pk"] == "WS#ws_test"
        assert call_kwargs["ExpressionAttributeValues"][":sk"] == "JIRA_CONNECTION#"


class TestDeleteConnection:
    """Test deleting a Jira connection from DynamoDB."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_connection_calls_delete_item(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test delete_connection removes the DynamoDB item."""
        mock_db_fn.return_value = mock_dynamodb

        delete_connection("ws_test", "jira_conn_test")
        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "JIRA_CONNECTION#jira_conn_test"}
        )


class TestUpdateConnectionProjects:
    """Test updating selected projects for a connection."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_connection_projects_success(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test update_connection_projects updates DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb
        projects = [{"id": "10001", "key": "PROJ", "name": "Test"}]

        update_connection_projects("ws_test", "jira_conn_test", projects, True, False)
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":projects"] == projects
        assert call_kwargs["ExpressionAttributeValues"][":attachments"] is True
        assert call_kwargs["ExpressionAttributeValues"][":comments"] is False


class TestUpdateSyncStatus:
    """Test updating sync status for a connection."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_success(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test updating sync status to success sets sync_at."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "success")
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":sync_at" in call_kwargs["ExpressionAttributeValues"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_failed_sets_sync_at(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test updating sync status to failed also sets sync_at."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "failed", error="Something broke")
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":sync_at" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":error"] == "Something broke"

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_auth_error_sets_sync_at(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test updating sync status to auth_error sets sync_at."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "auth_error", error="Token expired")
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":sync_at" in call_kwargs["ExpressionAttributeValues"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_queued_no_sync_at(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test updating sync status to queued does not set sync_at."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "queued")
        call_kwargs = mock_table.update_item.call_args[1]
        assert ":sync_at" not in call_kwargs["ExpressionAttributeValues"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_with_issue_count(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test updating sync status with issue count."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "success", issue_count=150)
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":count"] == 150

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_sync_status_no_error_removes_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test that passing no error removes the last_sync_error field."""
        mock_db_fn.return_value = mock_dynamodb

        update_sync_status("ws_test", "jira_conn_test", "success")
        call_kwargs = mock_table.update_item.call_args[1]
        assert "REMOVE last_sync_error" in call_kwargs["UpdateExpression"]


# ============================================================================
# SQS Operations
# ============================================================================

class TestQueueSyncJob:
    """Test SQS sync job queuing."""

    @patch.object(handler_module, '_get_sqs')
    def test_queue_sync_job_sends_message(self, mock_sqs_fn):
        """Test queuing a sync job sends correct SQS message."""
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        handler_module.JIRA_SYNC_QUEUE = "https://sqs.example.com/jira-sync"

        queue_sync_job("ws_test", "jira_conn_test")
        mock_sqs_client.send_message.assert_called_once()
        call_kwargs = mock_sqs_client.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/jira-sync"
        msg = json.loads(call_kwargs["MessageBody"])
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "jira_conn_test"
        assert msg["triggered_by"] == "manual"


class TestQueueDeleteJob:
    """Test SQS delete job queuing."""

    @patch.object(handler_module, '_get_sqs')
    def test_queue_delete_job_sends_message(self, mock_sqs_fn):
        """Test queuing a delete job sends correct SQS message."""
        mock_sqs_client = MagicMock()
        mock_sqs_fn.return_value = mock_sqs_client
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        queue_delete_job("ws_test", "jira_conn_test")
        mock_sqs_client.send_message.assert_called_once()
        call_kwargs = mock_sqs_client.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/delete-queue"
        msg = json.loads(call_kwargs["MessageBody"])
        assert msg["action"] == "delete_prefix"
        assert msg["workspace_id"] == "ws_test"
        assert msg["source"] == "jira"

    def test_queue_delete_job_no_queue_configured(self):
        """Test no-op when DELETE_QUEUE not configured."""
        handler_module.DELETE_QUEUE = None
        # Should not raise
        queue_delete_job("ws_test", "jira_conn_test")

    @patch.object(handler_module, '_get_sqs')
    def test_queue_delete_job_empty_string_queue(self, mock_sqs_fn):
        """Test no-op when DELETE_QUEUE is empty string."""
        handler_module.DELETE_QUEUE = ""
        queue_delete_job("ws_test", "jira_conn_test")
        mock_sqs_fn.assert_not_called()


# ============================================================================
# OAuth Flow - Authorize
# ============================================================================

class TestHandleAuthorize:
    """Test OAuth authorization URL generation."""

    @patch.object(handler_module, 'get_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_authorize_success(self, mock_db_fn, mock_config, oauth_config, mock_dynamodb, mock_table):
        """Test successful authorization URL generation."""
        mock_db_fn.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = handle_authorize(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "auth_url" in body
        auth_url = body["auth_url"]
        assert "auth.atlassian.com/authorize" in auth_url
        assert "client_id=test-client-id" in auth_url
        assert "response_type=code" in auth_url

    @patch.object(handler_module, 'get_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_authorize_includes_scopes(self, mock_db_fn, mock_config, oauth_config, mock_dynamodb, mock_table):
        """Test auth URL includes Jira scopes."""
        mock_db_fn.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = handle_authorize(event)
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "read%3Ajira-work" in auth_url or "read:jira-work" in auth_url
        assert "offline_access" in auth_url

    @patch.object(handler_module, 'get_oauth_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_authorize_includes_state(self, mock_db_fn, mock_config, oauth_config, mock_dynamodb, mock_table, oauth_states):
        """Test auth URL carries an opaque state token; workspace_id and user_email are stored server-side."""
        mock_db_fn.return_value = mock_dynamodb
        mock_config.return_value = oauth_config
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = handle_authorize(event)
        body = json.loads(result["body"])
        auth_url = body["auth_url"]
        assert "state=" in auth_url
        token = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)["state"][0]
        assert token in oauth_states.rows
        row = oauth_states.rows[token]
        assert row["provider"] == "jira"
        assert row["workspace_id"] == "ws_test"
        assert row["user_email"] == "admin@example.com"
        decoded = urllib.parse.unquote(auth_url)
        assert "ws_test" not in decoded
        assert "admin@example.com" not in decoded
        assert "workspace_id" not in decoded
        assert "user_email" not in decoded

    def test_handle_authorize_no_user_email(self):
        """Test returns 401 when user email is missing."""
        event = {
            "headers": {},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }
        result = handle_authorize(event)
        assert result["statusCode"] == 401

    def test_handle_authorize_no_workspace_id(self):
        """Test returns 400 when workspace_id is missing."""
        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {},
        }
        result = handle_authorize(event)
        assert result["statusCode"] == 400

    def test_handle_authorize_null_query_params(self):
        """Test returns 400 when queryStringParameters is None."""
        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": None,
        }
        result = handle_authorize(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_authorize_not_admin(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user is not workspace admin."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }
        result = handle_authorize(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_authorize_non_member(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user is not a workspace member at all."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        event = {
            "headers": {"x-user-email": "stranger@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }
        result = handle_authorize(event)
        assert result["statusCode"] == 403


# ============================================================================
# OAuth Flow - Callback
# ============================================================================

class TestHandleCallback:
    """Test OAuth callback handling."""

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'save_connection')
    @patch.object(handler_module, '_generate_connection_id', return_value="jira_new_conn")
    @patch.object(handler_module, 'list_connections', return_value=[])
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_success_new_connection(
        self, mock_config, mock_exchange, mock_resources, mock_list,
        mock_gen_id, mock_save_conn, mock_save_tokens, oauth_config, sample_sites, oauth_states,
    ):
        """Test successful OAuth callback creates new connection."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_tok", "refresh_tok", 3600)
        mock_resources.return_value = sample_sites

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }

        result = handle_callback(event)
        assert result["statusCode"] == 302
        location = result["headers"]["Location"]
        assert "/workspaces/ws_test/settings" in location
        assert "tab=jira" in location
        assert "connection=jira_new_conn" in location
        mock_save_conn.assert_called_once()
        # The connection is attributed to the admin stored with the state.
        assert mock_save_conn.call_args.kwargs["created_by"] == "admin@example.com"
        mock_save_tokens.assert_called_once_with("jira_new_conn", "access_tok", "refresh_tok", 3600)

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'list_connections')
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_existing_connection(
        self, mock_config, mock_exchange, mock_resources, mock_list,
        mock_save_tokens, oauth_config, sample_sites, sample_connection,
        oauth_states,
    ):
        """Test callback updates tokens for existing connection with same cloud_id."""
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("new_access", "new_refresh", 3600)
        mock_resources.return_value = sample_sites
        mock_list.return_value = [sample_connection]

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }

        result = handle_callback(event)
        assert result["statusCode"] == 302
        location = result["headers"]["Location"]
        assert f"connection={sample_connection['connection_id']}" in location
        mock_save_tokens.assert_called_once_with(
            sample_connection["connection_id"], "new_access", "new_refresh", 3600
        )

    def test_handle_callback_oauth_error(self):
        """Test callback handles OAuth error parameter."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {"error": "access_denied"},
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=access_denied" in result["headers"]["Location"]

    def test_handle_callback_missing_code(self, oauth_states):
        """Test callback redirects on missing code."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=missing_params" in result["headers"]["Location"]

    def test_handle_callback_missing_state(self):
        """Test callback redirects on missing state."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {"code": "some_code"},
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=missing_params" in result["headers"]["Location"]

    @patch.object(handler_module, 'exchange_code_for_tokens')
    def test_handle_callback_rejects_forged_json_state(self, mock_exchange):
        """Test callback rejects a hand-built JSON state naming a workspace."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": json.dumps({"workspace_id": "ws-x", "user_email": "x@y"}),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=invalid_state" in result["headers"]["Location"]
        mock_exchange.assert_not_called()

    @patch.object(handler_module, 'exchange_code_for_tokens')
    def test_handle_callback_rejects_unknown_token(self, mock_exchange):
        """Test callback rejects a state token that was never issued."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {
                "code": "auth_code",
                "state": "never-issued-token",
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=invalid_state" in result["headers"]["Location"]
        mock_exchange.assert_not_called()

    @patch.object(handler_module, 'save_tokens')
    @patch.object(handler_module, 'save_connection')
    @patch.object(handler_module, '_generate_connection_id', return_value="jira_new_conn")
    @patch.object(handler_module, 'list_connections', return_value=[])
    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_state_token_is_single_use(
        self, mock_config, mock_exchange, mock_resources, mock_list,
        mock_gen_id, mock_save_conn, mock_save_tokens, oauth_config, sample_sites, oauth_states,
    ):
        """Test a state token cannot be replayed after a successful callback."""
        handler_module.ENV = "dev"
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_tok", "refresh_tok", 3600)
        mock_resources.return_value = sample_sites

        event = {
            "queryStringParameters": {
                "code": "auth_code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }

        first = handle_callback(event)
        assert "connection=jira_new_conn" in first["headers"]["Location"]

        second = handle_callback(event)
        assert second["statusCode"] == 302
        assert "jira_error=invalid_state" in second["headers"]["Location"]
        assert mock_exchange.call_count == 1
        mock_save_conn.assert_called_once()

    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_token_exchange_returns_none(self, mock_config, mock_exchange, oauth_config, oauth_states):
        """Test callback handles None tokens from exchange."""
        handler_module.ENV = "dev"
        mock_config.return_value = oauth_config
        mock_exchange.return_value = (None, None, None)

        event = {
            "queryStringParameters": {
                "code": "bad_code",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=token_exchange_failed" in result["headers"]["Location"]

    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_access_token_but_no_refresh(self, mock_config, mock_exchange, oauth_config, oauth_states):
        """Test callback handles access token without refresh token."""
        handler_module.ENV = "dev"
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_tok", None, 3600)

        event = {
            "queryStringParameters": {
                "code": "code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=token_exchange_failed" in result["headers"]["Location"]

    @patch.object(handler_module, 'get_accessible_resources')
    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_no_accessible_sites(self, mock_config, mock_exchange, mock_resources, oauth_config, oauth_states):
        """Test callback handles no accessible Jira sites."""
        handler_module.ENV = "dev"
        mock_config.return_value = oauth_config
        mock_exchange.return_value = ("access_tok", "refresh_tok", 3600)
        mock_resources.return_value = []

        event = {
            "queryStringParameters": {
                "code": "code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=no_sites" in result["headers"]["Location"]

    @patch.object(handler_module, 'exchange_code_for_tokens')
    @patch.object(handler_module, 'get_oauth_config')
    def test_handle_callback_exception(self, mock_config, mock_exchange, oauth_config, oauth_states):
        """Test callback handles unexpected exceptions."""
        handler_module.ENV = "dev"
        mock_config.return_value = oauth_config
        mock_exchange.side_effect = Exception("Unexpected error")

        event = {
            "queryStringParameters": {
                "code": "code_123",
                "state": oauth_states.issue("jira", "ws_test", "admin@example.com"),
            },
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=callback_failed" in result["headers"]["Location"]

    def test_handle_callback_prd_redirect_url(self):
        """Test callback uses production URL when ENV is prd."""
        handler_module.ENV = "prd"
        event = {
            "queryStringParameters": {"error": "access_denied"},
        }
        result = handle_callback(event)
        assert "outcomeops.outcomeops.ai" in result["headers"]["Location"]
        assert "dev." not in result["headers"]["Location"]
        handler_module.ENV = "dev"  # Reset

    def test_handle_callback_dev_redirect_url(self):
        """Test callback uses dev URL when ENV is dev."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {"error": "access_denied"},
        }
        result = handle_callback(event)
        assert "dev.outcomeops.outcomeops.ai" in result["headers"]["Location"]

    def test_handle_callback_post_with_body(self):
        """Test callback reads code/state from POST body when not in query params."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {},
            "body": json.dumps({
                "code": "body_code",
                "state": "not-json",
            }),
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        # Should have tried to parse the state from body
        assert "jira_error=invalid_state" in result["headers"]["Location"]

    def test_handle_callback_post_invalid_body_json(self):
        """Test callback handles invalid JSON in body gracefully."""
        handler_module.ENV = "dev"
        event = {
            "queryStringParameters": {},
            "body": "not-valid-json{{",
        }
        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "jira_error=missing_params" in result["headers"]["Location"]


# ============================================================================
# Connection Operations
# ============================================================================

class TestHandleListConnections:
    """Test listing Jira connections via handler."""

    @patch.object(handler_module, 'list_connections')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_connections_success(self, mock_db_fn, mock_list, mock_dynamodb, mock_table, sample_connection):
        """Test listing connections returns sanitized data."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [sample_connection]

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test"},
        }

        result = handle_list_connections(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        conns = body["connections"]
        assert len(conns) == 1
        conn = conns[0]
        assert conn["connection_id"] == "jira_conn_test"
        assert conn["site_name"] == "Test Site"
        assert conn["site_url"] == "https://testsite.atlassian.net"
        assert conn["issue_count"] == 42
        # Internal fields should not be present
        assert "PK" not in conn
        assert "SK" not in conn
        assert "created_by" not in conn

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_connections_no_user(self, mock_db_fn, mock_dynamodb):
        """Test returns 401 when user email is missing."""
        mock_db_fn.return_value = mock_dynamodb

        event = {
            "headers": {},
            "pathParameters": {"id": "ws_test"},
        }
        result = handle_list_connections(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_connections_access_denied(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user cannot access workspace."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = {
            "headers": {"x-user-email": "stranger@example.com"},
            "pathParameters": {"id": "ws_test"},
        }
        result = handle_list_connections(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'list_connections')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_connections_empty(self, mock_db_fn, mock_list, mock_dynamodb, mock_table):
        """Test listing returns empty array when no connections."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = []

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test"},
        }
        result = handle_list_connections(event)
        body = json.loads(result["body"])
        assert body["connections"] == []


# ============================================================================
# Project Operations
# ============================================================================

class TestHandleListProjects:
    """Test listing Jira projects for a connection."""

    @patch.object(handler_module, 'list_all_projects')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_success(
        self, mock_db_fn, mock_get_conn, mock_refresh, mock_list_projects,
        mock_dynamodb, mock_table, sample_connection, sample_projects,
    ):
        """Test successfully listing projects with selected markers."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "new_access_token"
        mock_list_projects.return_value = sample_projects

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }

        result = handle_list_projects(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        projects = body["projects"]
        assert len(projects) == 3
        # PROJ should be marked as selected
        proj = next(p for p in projects if p["key"] == "PROJ")
        assert proj["selected"] is True
        # OPS should not be selected
        ops = next(p for p in projects if p["key"] == "OPS")
        assert ops["selected"] is False
        assert body["selected_count"] == 1

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_no_user(self, mock_db_fn, mock_dynamodb):
        """Test returns 401 when user email is missing."""
        mock_db_fn.return_value = mock_dynamodb

        event = {
            "headers": {},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_list_projects(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_access_denied(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user cannot access workspace."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = {
            "headers": {"x-user-email": "stranger@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_list_projects(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_connection_not_found(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = None

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "bad_conn"},
        }
        result = handle_list_projects(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_token_refresh_fails(self, mock_db_fn, mock_get_conn, mock_refresh, mock_dynamodb, mock_table, sample_connection):
        """Test returns 401 when token refresh fails."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = None

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_list_projects(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_jira_auth_error(self, mock_db_fn, mock_get_conn, mock_refresh, mock_dynamodb, mock_table, sample_connection):
        """Test returns 401 on JiraAuthError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "token"

        # Import JiraAuthError from the handler module's namespace
        from jira_api import JiraAuthError
        with patch.object(handler_module, 'list_all_projects', side_effect=JiraAuthError("Token expired")):
            event = {
                "headers": {"x-user-email": "user@example.com"},
                "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            }
            result = handle_list_projects(event)
            assert result["statusCode"] == 401

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_list_projects_jira_api_error(self, mock_db_fn, mock_get_conn, mock_refresh, mock_dynamodb, mock_table, sample_connection):
        """Test returns 502 on JiraAPIError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "token"

        from jira_api import JiraAPIError
        with patch.object(handler_module, 'list_all_projects', side_effect=JiraAPIError("Server error", 500)):
            event = {
                "headers": {"x-user-email": "user@example.com"},
                "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            }
            result = handle_list_projects(event)
            assert result["statusCode"] == 502


class TestHandleUpdateProjects:
    """Test updating selected projects for a connection."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'update_connection_projects')
    @patch.object(handler_module, 'list_all_projects')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_success(
        self, mock_db_fn, mock_get_conn, mock_refresh, mock_list_projects,
        mock_update_conn, mock_update_status, mock_queue,
        mock_dynamodb, mock_table, sample_connection, sample_projects,
    ):
        """Test successfully updating projects queues sync."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "access_token"
        mock_list_projects.return_value = sample_projects

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": ["PROJ", "OPS"]}),
        }

        result = handle_update_projects(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["selected_count"] == 2
        assert body["message"] == "Projects updated, sync queued"
        mock_update_conn.assert_called_once()
        mock_update_status.assert_called_once_with("ws_test", "jira_conn_test", "queued")
        mock_queue.assert_called_once_with("ws_test", "jira_conn_test")

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_no_user(self, mock_db_fn, mock_dynamodb):
        """Test returns 401 when user email is missing."""
        mock_db_fn.return_value = mock_dynamodb

        event = {
            "headers": {},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": ["PROJ"]}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_not_admin(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user is not workspace admin."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": ["PROJ"]}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_connection_not_found(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "bad_conn"},
            "body": json.dumps({"project_keys": ["PROJ"]}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_invalid_json_body(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table, sample_connection):
        """Test returns 400 for invalid JSON body."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": "not-valid-json",
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_empty_project_keys(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table, sample_connection):
        """Test returns 400 when no project keys provided."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": []}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'list_all_projects')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_no_valid_keys(
        self, mock_db_fn, mock_get_conn, mock_refresh, mock_list_projects,
        mock_dynamodb, mock_table, sample_connection, sample_projects,
    ):
        """Test returns 400 when no project keys match actual projects."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "access_token"
        mock_list_projects.return_value = sample_projects

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": ["NONEXISTENT"]}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_token_refresh_fails(
        self, mock_db_fn, mock_get_conn, mock_refresh,
        mock_dynamodb, mock_table, sample_connection,
    ):
        """Test returns 401 when token refresh fails."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = None

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({"project_keys": ["PROJ"]}),
        }
        result = handle_update_projects(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_jira_auth_error(
        self, mock_db_fn, mock_get_conn, mock_refresh,
        mock_dynamodb, mock_table, sample_connection,
    ):
        """Test returns 401 on JiraAuthError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "token"

        from jira_api import JiraAuthError
        with patch.object(handler_module, 'list_all_projects', side_effect=JiraAuthError("Auth failed")):
            event = {
                "headers": {"x-user-email": "admin@example.com"},
                "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
                "body": json.dumps({"project_keys": ["PROJ"]}),
            }
            result = handle_update_projects(event)
            assert result["statusCode"] == 401

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_jira_api_error(
        self, mock_db_fn, mock_get_conn, mock_refresh,
        mock_dynamodb, mock_table, sample_connection,
    ):
        """Test returns 502 on JiraAPIError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "token"

        from jira_api import JiraAPIError
        with patch.object(handler_module, 'list_all_projects', side_effect=JiraAPIError("Server error")):
            event = {
                "headers": {"x-user-email": "admin@example.com"},
                "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
                "body": json.dumps({"project_keys": ["PROJ"]}),
            }
            result = handle_update_projects(event)
            assert result["statusCode"] == 502

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'update_connection_projects')
    @patch.object(handler_module, 'list_all_projects')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_update_projects_include_options(
        self, mock_db_fn, mock_get_conn, mock_refresh, mock_list_projects,
        mock_update_conn, mock_update_status, mock_queue,
        mock_dynamodb, mock_table, sample_connection, sample_projects,
    ):
        """Test include_attachments and include_comments options are passed through."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection
        mock_refresh.return_value = "access_token"
        mock_list_projects.return_value = sample_projects

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
            "body": json.dumps({
                "project_keys": ["PROJ"],
                "include_attachments": False,
                "include_comments": False,
            }),
        }

        result = handle_update_projects(event)
        assert result["statusCode"] == 200
        call_args = mock_update_conn.call_args
        assert call_args[0][3] is False  # include_attachments
        assert call_args[0][4] is False  # include_comments


# ============================================================================
# Sync Operations
# ============================================================================

class TestHandleSync:
    """Test manual sync trigger."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_success(self, mock_db_fn, mock_get_conn, mock_update_status, mock_queue, mock_dynamodb, mock_table, sample_connection):
        """Test triggering sync queues job."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = sample_connection

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }

        result = handle_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "queued"
        assert body["message"] == "Sync queued"
        mock_update_status.assert_called_once_with("ws_test", "jira_conn_test", "queued")
        mock_queue.assert_called_once_with("ws_test", "jira_conn_test")

    def test_handle_sync_no_user(self):
        """Test returns 401 when user email is missing."""
        event = {
            "headers": {},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_access_denied(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user cannot access workspace."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = {
            "headers": {"x-user-email": "stranger@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_connection_not_found(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = None

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "bad_conn"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_no_projects_selected(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 400 when no projects are selected."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = {
            "connection_id": "jira_conn_test",
            "selected_projects": [],
            "last_sync_status": "needs_configuration",
        }

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_already_queued(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 200 with message when sync already queued."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = {
            "connection_id": "jira_conn_test",
            "selected_projects": [{"key": "PROJ"}],
            "last_sync_status": "queued",
        }

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "queued"
        assert "already in progress" in body["message"]

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_sync_already_in_progress(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 200 with message when sync is in_progress."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_conn.return_value = {
            "connection_id": "jira_conn_test",
            "selected_projects": [{"key": "PROJ"}],
            "last_sync_status": "in_progress",
        }

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "in_progress"


# ============================================================================
# Disconnect Operations
# ============================================================================

class TestHandleDisconnect:
    """Test disconnecting a Jira connection."""

    @patch.object(handler_module, 'delete_connection')
    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'queue_delete_job')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_disconnect_success(
        self, mock_db_fn, mock_get_conn, mock_queue_delete, mock_del_tokens,
        mock_del_conn, mock_dynamodb, mock_table, sample_connection,
    ):
        """Test successful disconnect removes connection, tokens, and queues deletion."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }

        result = handle_disconnect(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "Disconnected successfully"
        mock_queue_delete.assert_called_once_with("ws_test", "jira_conn_test")
        mock_del_tokens.assert_called_once_with("jira_conn_test")
        mock_del_conn.assert_called_once_with("ws_test", "jira_conn_test")

    def test_handle_disconnect_no_user(self):
        """Test returns 401 when user email is missing."""
        event = {
            "headers": {},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_disconnect(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_disconnect_not_admin(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns 403 when user is not workspace admin."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        event = {
            "headers": {"x-user-email": "user@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        result = handle_disconnect(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_disconnect_connection_not_found(self, mock_db_fn, mock_get_conn, mock_dynamodb, mock_table):
        """Test returns 404 when connection not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = None

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "bad_conn"},
        }
        result = handle_disconnect(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'delete_connection')
    @patch.object(handler_module, 'delete_tokens')
    @patch.object(handler_module, 'queue_delete_job')
    @patch.object(handler_module, 'get_connection')
    @patch.object(handler_module, '_get_dynamodb')
    def test_handle_disconnect_order_of_operations(
        self, mock_db_fn, mock_get_conn, mock_queue_delete, mock_del_tokens,
        mock_del_conn, mock_dynamodb, mock_table, sample_connection,
    ):
        """Test disconnect performs operations in correct order: queue delete, delete tokens, delete connection."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_conn.return_value = sample_connection

        call_order = []
        mock_queue_delete.side_effect = lambda *a: call_order.append("queue_delete")
        mock_del_tokens.side_effect = lambda *a: call_order.append("delete_tokens")
        mock_del_conn.side_effect = lambda *a, **kw: call_order.append("delete_connection")

        event = {
            "headers": {"x-user-email": "admin@example.com"},
            "pathParameters": {"id": "ws_test", "connectionId": "jira_conn_test"},
        }
        handle_disconnect(event)
        assert call_order == ["queue_delete", "delete_tokens", "delete_connection"]


# ============================================================================
# Lambda Handler Routing
# ============================================================================

class TestLambdaHandlerRouting:
    """Test main handler routes requests correctly."""

    def test_routes_authorize_get(self):
        """Test routing GET /jira/authorize."""
        with patch.object(handler_module, 'handle_authorize', return_value=_response(200, {"auth_url": "https://..."})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/jira/authorize"}},
                "headers": {"x-user-email": "admin@example.com"},
                "queryStringParameters": {"workspace_id": "ws_test"},
            }
            result = lambda_handler(event, None)
            mock_fn.assert_called_once()

    def test_routes_callback_get(self):
        """Test routing GET /jira/callback."""
        with patch.object(handler_module, 'handle_callback', return_value=_redirect("https://...")) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/jira/callback"}},
                "queryStringParameters": {"code": "abc", "state": "{}"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()

    def test_routes_callback_post(self):
        """Test routing POST /jira/callback."""
        with patch.object(handler_module, 'handle_callback', return_value=_redirect("https://...")) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "POST", "path": "/jira/callback"}},
                "body": json.dumps({"code": "abc", "state": "{}"}),
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()

    def test_routes_list_connections(self):
        """Test routing GET /workspaces/{id}/jira/connections."""
        with patch.object(handler_module, 'handle_list_connections', return_value=_response(200, {"connections": []})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/jira/connections"}},
                "headers": {"x-user-email": "user@example.com"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()
            # Verify pathParameters were set
            call_event = mock_fn.call_args[0][0]
            assert call_event["pathParameters"] == {"id": "ws1"}

    def test_routes_list_projects(self):
        """Test routing GET /workspaces/{id}/jira/connections/{connId}/projects."""
        with patch.object(handler_module, 'handle_list_projects', return_value=_response(200, {"projects": []})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/jira/connections/conn1/projects"}},
                "headers": {"x-user-email": "user@example.com"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()
            call_event = mock_fn.call_args[0][0]
            assert call_event["pathParameters"] == {"id": "ws1", "connectionId": "conn1"}

    def test_routes_update_projects(self):
        """Test routing PUT /workspaces/{id}/jira/connections/{connId}/projects."""
        with patch.object(handler_module, 'handle_update_projects', return_value=_response(200, {"message": "Updated"})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "PUT", "path": "/workspaces/ws1/jira/connections/conn1/projects"}},
                "headers": {"x-user-email": "admin@example.com"},
                "body": json.dumps({"project_keys": ["PROJ"]}),
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()
            call_event = mock_fn.call_args[0][0]
            assert call_event["pathParameters"] == {"id": "ws1", "connectionId": "conn1"}

    def test_routes_sync(self):
        """Test routing POST /workspaces/{id}/jira/connections/{connId}/sync."""
        with patch.object(handler_module, 'handle_sync', return_value=_response(200, {"status": "queued"})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "POST", "path": "/workspaces/ws1/jira/connections/conn1/sync"}},
                "headers": {"x-user-email": "admin@example.com"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()
            call_event = mock_fn.call_args[0][0]
            assert call_event["pathParameters"] == {"id": "ws1", "connectionId": "conn1"}

    def test_routes_disconnect(self):
        """Test routing DELETE /workspaces/{id}/jira/connections/{connId}."""
        with patch.object(handler_module, 'handle_disconnect', return_value=_response(200, {"message": "Disconnected"})) as mock_fn:
            event = {
                "requestContext": {"http": {"method": "DELETE", "path": "/workspaces/ws1/jira/connections/conn1"}},
                "headers": {"x-user-email": "admin@example.com"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()
            call_event = mock_fn.call_args[0][0]
            assert call_event["pathParameters"] == {"id": "ws1", "connectionId": "conn1"}

    def test_unknown_route_returns_404(self):
        """Test unknown routes return 404."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/unknown/route"}},
            "headers": {"x-user-email": "user@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_wrong_method_returns_404(self):
        """Test wrong HTTP method returns 404."""
        event = {
            "requestContext": {"http": {"method": "POST", "path": "/jira/authorize"}},
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_handler_uses_httpmethod_field(self):
        """Test handler falls back to httpMethod field for API Gateway v1."""
        with patch.object(handler_module, 'handle_authorize', return_value=_response(200, {"auth_url": "test"})) as mock_fn:
            event = {
                "httpMethod": "GET",
                "path": "/jira/authorize",
                "headers": {"x-user-email": "admin@example.com"},
                "queryStringParameters": {"workspace_id": "ws_test"},
            }
            lambda_handler(event, None)
            mock_fn.assert_called_once()

    def test_handler_dynamodb_error_returns_500(self):
        """Test handler returns 500 on DynamoDB ClientError."""
        with patch.object(handler_module, 'handle_list_connections') as mock_fn:
            mock_fn.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "Internal"}}, "Query"
            )
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/jira/connections"}},
                "headers": {"x-user-email": "user@example.com"},
            }
            result = lambda_handler(event, None)
            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert body["error"] == "Database error"

    def test_handler_unhandled_error_returns_500(self):
        """Test handler returns 500 on unhandled exception."""
        with patch.object(handler_module, 'handle_list_connections') as mock_fn:
            mock_fn.side_effect = RuntimeError("Something unexpected")
            event = {
                "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/jira/connections"}},
                "headers": {"x-user-email": "user@example.com"},
            }
            result = lambda_handler(event, None)
            assert result["statusCode"] == 500
            body = json.loads(result["body"])
            assert "error" in body

    def test_delete_method_on_connections_list_returns_404(self):
        """Test DELETE on /workspaces/{id}/jira/connections (without connId) returns 404."""
        event = {
            "requestContext": {"http": {"method": "DELETE", "path": "/workspaces/ws1/jira/connections"}},
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_put_method_on_sync_endpoint_returns_404(self):
        """Test PUT on sync endpoint returns 404 (only POST allowed)."""
        event = {
            "requestContext": {"http": {"method": "PUT", "path": "/workspaces/ws1/jira/connections/conn1/sync"}},
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_get_method_on_disconnect_endpoint_returns_404(self):
        """Test GET on disconnect endpoint returns 404 (only DELETE allowed)."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/workspaces/ws1/jira/connections/conn1"}},
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

