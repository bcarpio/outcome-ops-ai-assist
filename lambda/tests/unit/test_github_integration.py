# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for github-integration Lambda handler."""

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
    """Load or reload the github-integration handler module."""
    # Pre-register jwt module if not installed (used by github_app.py)
    if 'jwt' not in sys.modules:
        mock_jwt = MagicMock()
        mock_jwt.encode.return_value = "mock.jwt.token"
        sys.modules['jwt'] = mock_jwt

    # Clear cached sibling modules to avoid stale imports
    for mod_name in ['github_integration_handler', 'github_app']:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Add github-integration dir to path so handler can resolve its sibling imports
    github_dir = os.path.join(os.path.dirname(__file__), '../../github-integration')
    if github_dir in sys.path:
        sys.path.remove(github_dir)
    sys.path.insert(0, github_dir)

    handler_path = os.path.join(github_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("github_integration_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['github_integration_handler'] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
handle_authorize = handler_module.handle_authorize
handle_callback = handler_module.handle_callback
handle_list_installations = handler_module.handle_list_installations
handle_list_installation_repos = handler_module.handle_list_installation_repos
handle_add_repos = handler_module.handle_add_repos
handle_list_repos = handler_module.handle_list_repos
handle_update_repo = handler_module.handle_update_repo
handle_delete_repo = handler_module.handle_delete_repo
handle_trigger_sync = handler_module.handle_trigger_sync
save_installation = handler_module.save_installation
get_installation = handler_module.get_installation
list_installations = handler_module.list_installations
delete_installation = handler_module.delete_installation
save_repo = handler_module.save_repo
get_repo = handler_module.get_repo
list_repos = handler_module.list_repos
update_repo = handler_module.update_repo
update_repo_sync_status = handler_module.update_repo_sync_status
delete_repo = handler_module.delete_repo
queue_sync_job = handler_module.queue_sync_job
queue_delete_job = handler_module.queue_delete_job
_response = handler_module._response
_error = handler_module._error
_redirect = handler_module._redirect
_get_user_from_request = handler_module._get_user_from_request
_is_workspace_admin = handler_module._is_workspace_admin
_can_access_workspace = handler_module._can_access_workspace
_parse_path = handler_module._parse_path
_now_iso = handler_module._now_iso


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
def github_config():
    """Sample GitHub App configuration."""
    return {
        "app_id": "12345",
        "private_key": "fake-private-key",
        "client_id": "Iv1.test-client-id",
        "client_secret": "test-client-secret",
        "callback_url": "https://app.example.com/api/github/callback",
        "app_slug": "outcome-ops-test",
    }


@pytest.fixture
def sample_installation():
    """Sample GitHub installation record."""
    return {
        "PK": "WS#ws_test",
        "SK": "GITHUB_INSTALLATION#12345",
        "installation_id": "12345",
        "workspace_id": "ws_test",
        "account_login": "test-org",
        "account_type": "Organization",
        "permissions": {"contents": "read", "metadata": "read"},
        "created_by": "admin@example.com",
        "created_at": "2025-01-01T00:00:00+00:00",
    }


@pytest.fixture
def sample_repo():
    """Sample GitHub repo record."""
    return {
        "PK": "WS#ws_test",
        "SK": "GITHUB_REPO#test-org/my-repo",
        "workspace_id": "ws_test",
        "repo_name": "test-org/my-repo",
        "installation_id": "12345",
        "enabled": True,
        "include_code_maps": True,
        "repo_type": "application",
        "last_sync_at": None,
        "last_sync_status": "pending",
        "last_sync_error": None,
        "last_commit_sha": None,
        "added_at": "2025-01-01T00:00:00+00:00",
        "added_by": "admin@example.com",
    }


def _make_event(method="GET", path="/", headers=None, query=None, body=None, path_params=None):
    """Helper to build a Lambda event dict."""
    event = {
        "httpMethod": method,
        "path": path,
        "headers": headers or {},
        "queryStringParameters": query or {},
    }
    if body is not None:
        event["body"] = json.dumps(body) if isinstance(body, dict) else body
    if path_params:
        event["pathParameters"] = path_params
    return event


def _authed_event(method="GET", path="/", query=None, body=None, path_params=None, email="user@example.com"):
    """Helper to build an authenticated Lambda event."""
    return _make_event(
        method=method,
        path=path,
        headers={"x-user-email": email},
        query=query,
        body=body,
        path_params=path_params,
    )


# ============================================================================
# Response Helpers
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
        for code in [200, 201, 400, 401, 403, 404, 500, 502]:
            result = _response(code, {"status": "test"})
            assert result["statusCode"] == code

    def test_response_serializes_datetime(self):
        """Test _response serializes datetime objects via default=str."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        result = _response(200, {"timestamp": now})
        body = json.loads(result["body"])
        assert isinstance(body["timestamp"], str)

    def test_error_builds_error_response(self):
        """Test _error returns response with error field."""
        result = _error(400, "Bad request")
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert body["error"] == "Bad request"

    def test_error_different_messages(self):
        """Test _error with various error messages."""
        result = _error(500, "Internal server error")
        body = json.loads(result["body"])
        assert body["error"] == "Internal server error"

    def test_redirect_builds_302_response(self):
        """Test _redirect builds redirect response."""
        result = _redirect("https://example.com/path")
        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == "https://example.com/path"
        body = json.loads(result["body"])
        assert body["redirect"] == "https://example.com/path"

    def test_redirect_includes_json_body(self):
        """Test _redirect includes redirect URL in JSON body for Express proxy."""
        url = "https://app.example.com/workspaces/ws1/settings"
        result = _redirect(url)
        assert result["headers"]["Content-Type"] == "application/json"
        body = json.loads(result["body"])
        assert body["redirect"] == url


# ============================================================================
# Auth Helpers
# ============================================================================

class TestAuthHelpers:
    """Test authentication helper functions."""

    def test_get_user_from_request_lowercase_header(self):
        """Test extracting user email from lowercase header."""
        event = {"headers": {"x-user-email": "User@Example.com"}}
        assert _get_user_from_request(event) == "user@example.com"

    def test_get_user_from_request_mixed_case_header(self):
        """Test extracting user email from mixed-case header."""
        event = {"headers": {"X-User-Email": "ADMIN@EXAMPLE.COM"}}
        assert _get_user_from_request(event) == "admin@example.com"

    def test_get_user_from_request_missing_headers(self):
        """Test returns None when headers missing."""
        assert _get_user_from_request({}) is None
        assert _get_user_from_request({"headers": {}}) is None

    def test_get_user_from_request_empty_email(self):
        """Test returns None when email header is empty string."""
        event = {"headers": {"x-user-email": ""}}
        assert _get_user_from_request(event) is None

    def test_is_workspace_admin_true_for_ws_admin(self, mock_table):
        """Test admin check returns True for workspace_admin role."""
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        assert _is_workspace_admin(mock_table, "admin@example.com", "ws_test") is True

    def test_is_workspace_admin_false_for_member(self, mock_table):
        """Test admin check returns falsy for regular member."""
        # First call: workspace membership check (member role)
        # Second call: org admin check (no item)
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]
        assert not _is_workspace_admin(mock_table, "user@example.com", "ws_test")

    def test_is_workspace_admin_false_when_not_member(self, mock_table):
        """Test admin check returns falsy when not a member."""
        mock_table.get_item.side_effect = [
            {},  # No workspace membership
            {},  # No org membership
        ]
        assert not _is_workspace_admin(mock_table, "stranger@example.com", "ws_test")

    def test_is_workspace_admin_true_for_org_admin(self, mock_table):
        """Test admin check returns True for org_admin even without workspace membership."""
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},  # workspace member but not admin
            {"Item": {"org_role": "org_admin"}},  # org admin
        ]
        assert _is_workspace_admin(mock_table, "orgadmin@example.com", "ws_test") is True

    def test_can_access_workspace_as_member(self, mock_table):
        """Test access check returns True for direct member."""
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        assert _can_access_workspace(mock_table, "user@example.com", "ws_test") is True

    def test_can_access_workspace_as_org_admin(self, mock_table):
        """Test access check returns True for org_admin without direct membership."""
        mock_table.get_item.side_effect = [
            {},  # No direct membership
            {"Item": {"org_role": "org_admin"}},
        ]
        assert _can_access_workspace(mock_table, "orgadmin@example.com", "ws_test") is True

    def test_can_access_workspace_denied(self, mock_table):
        """Test access check returns falsy when not member and not org admin."""
        mock_table.get_item.side_effect = [
            {},  # No direct membership
            {},  # No org membership
        ]
        assert not _can_access_workspace(mock_table, "stranger@example.com", "ws_test")

    def test_can_access_workspace_org_member_not_admin(self, mock_table):
        """Test access check returns False for org member who is not org_admin."""
        mock_table.get_item.side_effect = [
            {},  # No direct membership
            {"Item": {"org_role": "member"}},  # org member but not admin
        ]
        assert _can_access_workspace(mock_table, "orgmember@example.com", "ws_test") is False


# ============================================================================
# Path Parsing
# ============================================================================

class TestParsePath:
    """Test path parsing utility."""

    def test_parse_path_simple(self):
        """Test simple path parsing."""
        assert _parse_path("/github/authorize") == ("github", "authorize")

    def test_parse_path_workspace_route(self):
        """Test workspace path parsing."""
        assert _parse_path("/workspaces/ws1/github/repos") == ("workspaces", "ws1", "github", "repos")

    def test_parse_path_with_trailing_slash(self):
        """Test path with trailing slash."""
        assert _parse_path("/github/callback/") == ("github", "callback")

    def test_parse_path_deep_route(self):
        """Test parsing deep nested path."""
        assert _parse_path("/workspaces/ws1/github/installations/123/repos") == (
            "workspaces", "ws1", "github", "installations", "123", "repos"
        )

    def test_parse_path_repo_with_slash(self):
        """Test parsing path where repo name contains slash (owner/repo)."""
        result = _parse_path("/workspaces/ws1/github/repos/test-org/my-repo")
        assert result == ("workspaces", "ws1", "github", "repos", "test-org", "my-repo")


# ============================================================================
# Timestamp Helper
# ============================================================================

class TestNowIso:
    """Test timestamp helper."""

    def test_now_iso_returns_iso_format(self):
        """Test _now_iso returns ISO format string."""
        result = _now_iso()
        assert isinstance(result, str)
        assert "T" in result
        # Should contain timezone info
        assert "+" in result or "Z" in result


# ============================================================================
# DynamoDB Operations - Installations
# ============================================================================

class TestInstallationOperations:
    """Test DynamoDB operations for GitHub installations."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_save_installation(self, mock_db, mock_dynamodb, mock_table):
        """Test saving a GitHub installation to DynamoDB."""
        mock_db.return_value = mock_dynamodb

        save_installation(
            workspace_id="ws_test",
            installation_id="12345",
            account_login="test-org",
            account_type="Organization",
            permissions={"contents": "read"},
            created_by="admin@example.com",
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws_test"
        assert item["SK"] == "GITHUB_INSTALLATION#12345"
        assert item["installation_id"] == "12345"
        assert item["account_login"] == "test-org"
        assert item["account_type"] == "Organization"
        assert item["permissions"] == {"contents": "read"}
        assert item["created_by"] == "admin@example.com"

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_installation_found(self, mock_db, mock_dynamodb, mock_table, sample_installation):
        """Test getting an existing installation."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": sample_installation}

        result = get_installation("ws_test", "12345")
        assert result["installation_id"] == "12345"
        assert result["account_login"] == "test-org"

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_installation_not_found(self, mock_db, mock_dynamodb, mock_table):
        """Test getting a non-existent installation."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_installation("ws_test", "nonexistent")
        assert result is None

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installations_returns_items(self, mock_db, mock_dynamodb, mock_table, sample_installation):
        """Test listing installations for a workspace."""
        mock_db.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": [sample_installation]}

        result = list_installations("ws_test")
        assert len(result) == 1
        assert result[0]["installation_id"] == "12345"

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installations_empty(self, mock_db, mock_dynamodb, mock_table):
        """Test listing installations when none exist."""
        mock_db.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": []}

        result = list_installations("ws_test")
        assert result == []

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_installation(self, mock_db, mock_dynamodb, mock_table):
        """Test deleting an installation."""
        mock_db.return_value = mock_dynamodb

        delete_installation("ws_test", "12345")

        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "GITHUB_INSTALLATION#12345"}
        )


# ============================================================================
# DynamoDB Operations - Repos
# ============================================================================

class TestRepoOperations:
    """Test DynamoDB operations for GitHub repos."""

    @patch.object(handler_module, '_get_dynamodb')
    def test_save_repo_default_type(self, mock_db, mock_dynamodb, mock_table):
        """Test saving a repo with default type (application)."""
        mock_db.return_value = mock_dynamodb

        result = save_repo(
            workspace_id="ws_test",
            repo_name="test-org/my-repo",
            installation_id="12345",
            include_code_maps=True,
            added_by="admin@example.com",
        )

        mock_table.put_item.assert_called_once()
        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "WS#ws_test"
        assert item["SK"] == "GITHUB_REPO#test-org/my-repo"
        assert item["repo_name"] == "test-org/my-repo"
        assert item["installation_id"] == "12345"
        assert item["enabled"] is True
        assert item["include_code_maps"] is True
        assert item["repo_type"] == "application"
        assert item["last_sync_status"] == "pending"
        assert result["repo_name"] == "test-org/my-repo"

    @patch.object(handler_module, '_get_dynamodb')
    def test_save_repo_standards_type(self, mock_db, mock_dynamodb, mock_table):
        """Test saving a standards repo."""
        mock_db.return_value = mock_dynamodb

        result = save_repo(
            workspace_id="ws_test",
            repo_name="test-org/standards-repo",
            installation_id="12345",
            include_code_maps=False,
            added_by="admin@example.com",
            repo_type="standards",
        )

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["repo_type"] == "standards"
        assert item["include_code_maps"] is False

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_repo_found(self, mock_db, mock_dynamodb, mock_table, sample_repo):
        """Test getting an existing repo."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": sample_repo}

        result = get_repo("ws_test", "test-org/my-repo")
        assert result["repo_name"] == "test-org/my-repo"
        assert result["installation_id"] == "12345"

    @patch.object(handler_module, '_get_dynamodb')
    def test_get_repo_not_found(self, mock_db, mock_dynamodb, mock_table):
        """Test getting a non-existent repo."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_repo("ws_test", "nonexistent/repo")
        assert result is None

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_returns_items(self, mock_db, mock_dynamodb, mock_table, sample_repo):
        """Test listing repos for a workspace."""
        mock_db.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": [sample_repo]}

        result = list_repos("ws_test")
        assert len(result) == 1
        assert result[0]["repo_name"] == "test-org/my-repo"

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_empty(self, mock_db, mock_dynamodb, mock_table):
        """Test listing repos when none exist."""
        mock_db.return_value = mock_dynamodb
        mock_table.query.return_value = {"Items": []}

        result = list_repos("ws_test")
        assert result == []

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_include_code_maps(self, mock_db, mock_dynamodb, mock_table):
        """Test updating repo include_code_maps setting."""
        mock_db.return_value = mock_dynamodb

        update_repo("ws_test", "test-org/my-repo", include_code_maps=False)

        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert "include_code_maps = :icm" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":icm"] is False

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_enabled(self, mock_db, mock_dynamodb, mock_table):
        """Test updating repo enabled setting."""
        mock_db.return_value = mock_dynamodb

        update_repo("ws_test", "test-org/my-repo", enabled=False)

        call_kwargs = mock_table.update_item.call_args[1]
        assert "enabled = :enabled" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":enabled"] is False

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_both_fields(self, mock_db, mock_dynamodb, mock_table):
        """Test updating both include_code_maps and enabled."""
        mock_db.return_value = mock_dynamodb

        update_repo("ws_test", "test-org/my-repo", include_code_maps=True, enabled=True)

        call_kwargs = mock_table.update_item.call_args[1]
        expr = call_kwargs["UpdateExpression"]
        assert "include_code_maps = :icm" in expr
        assert "enabled = :enabled" in expr

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_sync_status_queued(self, mock_db, mock_dynamodb, mock_table):
        """Test updating sync status to queued."""
        mock_db.return_value = mock_dynamodb

        update_repo_sync_status("ws_test", "test-org/my-repo", "queued")

        call_kwargs = mock_table.update_item.call_args[1]
        assert ":status" in call_kwargs["ExpressionAttributeValues"]
        assert call_kwargs["ExpressionAttributeValues"][":status"] == "queued"

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_sync_status_success_removes_error(self, mock_db, mock_dynamodb, mock_table):
        """Test updating sync status to success removes last_sync_error."""
        mock_db.return_value = mock_dynamodb

        update_repo_sync_status("ws_test", "test-org/my-repo", "success", commit_sha="abc123")

        call_kwargs = mock_table.update_item.call_args[1]
        assert "REMOVE" in call_kwargs["UpdateExpression"]
        assert "last_sync_error" in call_kwargs["UpdateExpression"]
        assert call_kwargs["ExpressionAttributeValues"][":sha"] == "abc123"

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_sync_status_failed_with_error(self, mock_db, mock_dynamodb, mock_table):
        """Test updating sync status to failed with error message."""
        mock_db.return_value = mock_dynamodb

        update_repo_sync_status("ws_test", "test-org/my-repo", "failed", error="Rate limit exceeded")

        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["ExpressionAttributeValues"][":error"] == "Rate limit exceeded"
        assert "last_sync_at" in call_kwargs["UpdateExpression"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo(self, mock_db, mock_dynamodb, mock_table):
        """Test deleting a repo."""
        mock_db.return_value = mock_dynamodb

        delete_repo("ws_test", "test-org/my-repo")

        mock_table.delete_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "GITHUB_REPO#test-org/my-repo"}
        )


# ============================================================================
# SQS Operations
# ============================================================================

class TestSqsOperations:
    """Test SQS queue operations."""

    @patch.object(handler_module, '_get_sqs')
    def test_queue_sync_job_sends_message(self, mock_sqs_fn, mock_sqs):
        """Test queuing a sync job sends SQS message."""
        mock_sqs_fn.return_value = mock_sqs
        handler_module.GITHUB_SYNC_QUEUE = "https://sqs.example.com/sync-queue"

        queue_sync_job("ws_test", "test-org/my-repo", "12345")

        mock_sqs.send_message.assert_called_once()
        call_kwargs = mock_sqs.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/sync-queue"
        msg_body = json.loads(call_kwargs["MessageBody"])
        assert msg_body["workspace_id"] == "ws_test"
        assert msg_body["repo_name"] == "test-org/my-repo"
        assert msg_body["installation_id"] == "12345"
        assert msg_body["trigger"] == "manual"

    def test_queue_sync_job_no_queue_configured(self):
        """Test no-op when sync queue not configured."""
        handler_module.GITHUB_SYNC_QUEUE = None
        # Should not raise
        queue_sync_job("ws_test", "test-org/my-repo", "12345")

    @patch.object(handler_module, '_get_sqs')
    def test_queue_delete_job_sends_message(self, mock_sqs_fn, mock_sqs):
        """Test queuing a delete job sends SQS message."""
        mock_sqs_fn.return_value = mock_sqs
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete-queue"

        queue_delete_job("ws_test", "test-org/my-repo")

        # Full cleanup: owner-prefix + bare-name delete_prefix + symbol-graph
        # (delete-bug 01/03 — a single owner-prefix delete left bare-name
        # KB objects and ~46k symbol-graph facts behind).
        bodies = [
            json.loads(c.kwargs["MessageBody"])
            for c in mock_sqs.send_message.call_args_list
        ]
        # Each message is sent twice: immediately and as a delayed recheck.
        assert len(bodies) == 6
        calls = mock_sqs.send_message.call_args_list
        immediate = [c for c in calls if "DelaySeconds" not in c.kwargs]
        delayed = [c for c in calls if c.kwargs.get("DelaySeconds") == 900]
        assert len(immediate) == 3 and len(delayed) == 3
        imm_bodies = [json.loads(c.kwargs["MessageBody"]) for c in immediate]
        del_bodies = [json.loads(c.kwargs["MessageBody"]) for c in delayed]
        assert all("recheck" not in b for b in imm_bodies)
        assert all(b.pop("recheck") is True for b in del_bodies)
        assert del_bodies == imm_bodies  # same cleanup, re-run later
        bodies = imm_bodies
        assert all(b["workspace_id"] == "ws_test" for b in bodies)
        prefixes = {b["prefix"] for b in bodies if b["action"] == "delete_prefix"}
        assert prefixes == {"test-org/my-repo/", "my-repo/"}  # owner + bare
        sg = [b for b in bodies if b["action"] == "delete_symbol_graph"]
        assert len(sg) == 1 and sg[0]["repo_bare"] == "my-repo"

    def test_queue_delete_job_no_queue_configured(self):
        """Test no-op when delete queue not configured."""
        handler_module.DELETE_QUEUE = None
        # Should not raise
        queue_delete_job("ws_test", "test-org/my-repo")


# ============================================================================
# OAuth Flow - Authorize
# ============================================================================

class TestHandleAuthorize:
    """Test GitHub App authorization URL generation."""

    @patch.object(handler_module, 'get_github_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_success(self, mock_db, mock_config, mock_dynamodb, mock_table, github_config):
        """Test successful authorize returns install URL."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = github_config
        # workspace admin check
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            method="GET",
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
            email="admin@example.com",
        )

        result = handle_authorize(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "install_url" in body
        assert "github.com/apps/outcome-ops-test/installations/new" in body["install_url"]
        # State is an opaque one-time token; the workspace id never rides in the URL.
        assert "state=" in body["install_url"]
        assert "ws_test" not in body["install_url"]
        # Regression guard 2026-06-04: when multiple GitHub App callback
        # URLs are registered (one per environment / workspace stack),
        # GitHub uses the FIRST one unless redirect_uri is explicit.
        # The install URL must carry this env's callback_url so the
        # post-install redirect lands back on the calling FQDN, not
        # whichever env's URL happens to be registered first on the
        # App.
        import urllib.parse as _up
        assert "redirect_uri=" in body["install_url"]
        # The Lambda fully URL-encodes the redirect_uri via quote(..., safe='').
        # GitHub's exact-match validation against registered Callback URLs
        # requires this; partial encoding (the default safe='/') leaves
        # `https%3A//host/...` which doesn't round-trip-match the
        # registered `https://host/...` and triggers silent fallback to
        # the FIRST registered URL.
        assert _up.quote(
            "https://app.example.com/api/github/callback", safe=""
        ) in body["install_url"]

    def test_authorize_missing_user_email(self):
        """Test authorize returns 401 without user email."""
        event = _make_event(path="/github/authorize", query={"workspace_id": "ws_test"})
        result = handle_authorize(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_missing_workspace_id(self, mock_db, mock_dynamodb, mock_table):
        """Test authorize returns 400 without workspace_id."""
        mock_db.return_value = mock_dynamodb

        event = _authed_event(path="/github/authorize", query={})
        result = handle_authorize(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "workspace_id" in body["error"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test authorize returns 403 for non-admin user."""
        mock_db.return_value = mock_dynamodb
        # Not a workspace admin, not an org admin
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]

        event = _authed_event(
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
        )
        result = handle_authorize(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_github_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_app_not_configured(self, mock_db, mock_config, mock_dynamodb, mock_table):
        """Test authorize returns 500 when GitHub App not configured."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = {"app_id": "PLACEHOLDER_CONFIGURE_ME", "app_slug": "test-app"}
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
            email="admin@example.com",
        )
        result = handle_authorize(event)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'get_github_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_slug_not_configured(self, mock_db, mock_config, mock_dynamodb, mock_table):
        """Test authorize returns 500 when app slug not configured."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = {"app_id": "12345", "app_slug": "PLACEHOLDER_CONFIGURE_ME"}
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
            email="admin@example.com",
        )
        result = handle_authorize(event)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'get_github_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_empty_app_id(self, mock_db, mock_config, mock_dynamodb, mock_table):
        """Test authorize returns 500 when app_id is empty."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = {"app_id": "", "app_slug": "test-app"}
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
            email="admin@example.com",
        )
        result = handle_authorize(event)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'get_github_config')
    @patch.object(handler_module, '_get_dynamodb')
    def test_authorize_state_is_opaque_server_side_token(self, mock_db, mock_config, mock_dynamodb, mock_table, github_config, oauth_states):
        """Test authorize stores workspace_id and user_email server-side and puts only an opaque token in the URL."""
        mock_db.return_value = mock_dynamodb
        mock_config.return_value = github_config
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            path="/github/authorize",
            query={"workspace_id": "ws_test"},
            email="admin@example.com",
        )

        result = handle_authorize(event)
        body = json.loads(result["body"])
        install_url = body["install_url"]

        # Extract state parameter from URL
        parsed = urllib.parse.urlparse(install_url)
        query_params = urllib.parse.parse_qs(parsed.query)
        token = query_params["state"][0]
        assert token in oauth_states.rows
        row = oauth_states.rows[token]
        assert row["provider"] == "github"
        assert row["workspace_id"] == "ws_test"
        assert row["user_email"] == "admin@example.com"
        assert "ws_test" not in install_url
        assert "admin@example.com" not in install_url
        assert "admin%40example.com" not in install_url


# ============================================================================
# OAuth Flow - Callback
# ============================================================================

class TestHandleCallback:
    """Test GitHub App installation callback handling."""

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_success(self, mock_config, mock_installations, mock_save, github_config, oauth_states):
        """Test successful callback saves installation and redirects."""
        mock_config.return_value = github_config
        mock_installations.return_value = [
            {
                "id": 12345,
                "account": {"login": "test-org", "type": "Organization"},
                "permissions": {"contents": "read"},
            }
        ]

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={
                "installation_id": "12345",
                "setup_action": "install",
                "state": state,
            },
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "/workspaces/ws_test/settings" in result["headers"]["Location"]
        assert "github_connected=true" in result["headers"]["Location"]
        mock_save.assert_called_once_with(
            workspace_id="ws_test",
            installation_id="12345",
            account_login="test-org",
            account_type="Organization",
            permissions={"contents": "read"},
            created_by="admin@example.com",
        )

    @patch.object(handler_module, 'get_github_config')
    def test_callback_missing_installation_id(self, mock_config, github_config, oauth_states):
        """Test callback redirects with error when installation_id missing."""
        mock_config.return_value = github_config

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=missing_installation_id" in result["headers"]["Location"]

    @patch.object(handler_module, 'get_github_config')
    def test_callback_missing_state(self, mock_config, github_config):
        """Test callback redirects with error when state missing."""
        mock_config.return_value = github_config

        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345"},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=missing_state" in result["headers"]["Location"]

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_rejects_forged_json_state(self, mock_config, mock_installations, mock_save, github_config):
        """Test callback rejects a hand-built JSON state naming a workspace."""
        mock_config.return_value = github_config

        event = _make_event(
            method="GET",
            path="/github/callback",
            query={
                "installation_id": "12345",
                "state": json.dumps({"workspace_id": "ws-x", "user_email": "x@y"}),
            },
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=invalid_state" in result["headers"]["Location"]
        mock_installations.assert_not_called()
        mock_save.assert_not_called()

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_rejects_unknown_token(self, mock_config, mock_save, github_config):
        """Test callback rejects a state token that was never issued."""
        mock_config.return_value = github_config

        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": "never-issued-token"},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=invalid_state" in result["headers"]["Location"]
        mock_save.assert_not_called()

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_rejects_token_for_other_provider(self, mock_config, mock_save, github_config, oauth_states):
        """Test callback rejects a state token issued for a different provider."""
        mock_config.return_value = github_config
        token = oauth_states.issue("jira", "ws_test", "admin@example.com")

        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": token},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=invalid_state" in result["headers"]["Location"]
        mock_save.assert_not_called()

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_state_token_is_single_use(self, mock_config, mock_installations, mock_save, github_config, oauth_states):
        """Test a state token cannot be replayed after a successful callback."""
        mock_config.return_value = github_config
        mock_installations.return_value = [
            {
                "id": 12345,
                "account": {"login": "test-org", "type": "Organization"},
                "permissions": {"contents": "read"},
            }
        ]
        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        first = handle_callback(event)
        assert "github_connected=true" in first["headers"]["Location"]

        second = handle_callback(event)
        assert second["statusCode"] == 302
        assert "github_error=invalid_state" in second["headers"]["Location"]
        mock_save.assert_called_once()

    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_installation_not_found(self, mock_config, mock_installations, github_config, oauth_states):
        """Test callback handles installation not found in GitHub API."""
        mock_config.return_value = github_config
        mock_installations.return_value = [
            {"id": 99999, "account": {"login": "other-org", "type": "Organization"}},
        ]

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=installation_not_found" in result["headers"]["Location"]

    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_github_api_error(self, mock_config, mock_installations, github_config, oauth_states):
        """Test callback handles GitHub API error."""
        from github_app import GitHubAppError
        mock_config.return_value = github_config
        mock_installations.side_effect = GitHubAppError("API rate limit exceeded")

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=" in result["headers"]["Location"]

    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_unexpected_error(self, mock_config, mock_installations, github_config, oauth_states):
        """Test callback handles unexpected errors gracefully."""
        mock_config.return_value = github_config
        mock_installations.side_effect = RuntimeError("Unexpected error")

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_error=callback_failed" in result["headers"]["Location"]

    @patch.object(handler_module, 'save_installation')
    @patch.object(handler_module, 'get_app_installations')
    @patch.object(handler_module, 'get_github_config')
    def test_callback_reads_from_body_params(self, mock_config, mock_installations, mock_save, github_config, oauth_states):
        """Test callback can read params from POST body (Express proxy)."""
        mock_config.return_value = github_config
        mock_installations.return_value = [
            {
                "id": 12345,
                "account": {"login": "test-org", "type": "Organization"},
                "permissions": {"contents": "read"},
            }
        ]

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="POST",
            path="/github/callback",
            body={
                "installation_id": "12345",
                "setup_action": "install",
                "state": state,
            },
            query={},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 302
        assert "github_connected=true" in result["headers"]["Location"]

    @patch.object(handler_module, 'get_github_config')
    def test_callback_url_not_configured(self, mock_config, oauth_states):
        """Test callback returns error when callback_url not configured."""
        mock_config.return_value = {"callback_url": "PLACEHOLDER_CONFIGURE_ME"}

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'get_github_config')
    def test_callback_empty_callback_url(self, mock_config, oauth_states):
        """Test callback returns error when callback_url is empty."""
        mock_config.return_value = {"callback_url": ""}

        state = oauth_states.issue("github", "ws_test", "admin@example.com")
        event = _make_event(
            method="GET",
            path="/github/callback",
            query={"installation_id": "12345", "state": state},
        )

        result = handle_callback(event)
        assert result["statusCode"] == 500


# ============================================================================
# List Installations Handler
# ============================================================================

class TestHandleListInstallations:
    """Test listing GitHub installations for a workspace."""

    @patch.object(handler_module, 'list_installations')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installations_success(self, mock_db, mock_list, mock_dynamodb, mock_table, sample_installation):
        """Test listing installations returns sanitized data."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [sample_installation]

        event = _authed_event(
            path="/workspaces/ws_test/github/installations",
            path_params={"id": "ws_test"},
        )

        result = handle_list_installations(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["installations"]) == 1
        inst = body["installations"][0]
        assert inst["installation_id"] == "12345"
        assert inst["account_login"] == "test-org"
        assert inst["account_type"] == "Organization"
        # Should not have PK/SK
        assert "PK" not in inst
        assert "SK" not in inst

    def test_list_installations_no_auth(self):
        """Test listing installations returns 401 without auth."""
        event = _make_event(
            path="/workspaces/ws_test/github/installations",
            path_params={"id": "ws_test"},
        )
        result = handle_list_installations(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installations_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test listing installations returns 403 for non-member."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = _authed_event(
            path="/workspaces/ws_test/github/installations",
            path_params={"id": "ws_test"},
        )
        result = handle_list_installations(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'list_installations')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installations_empty(self, mock_db, mock_list, mock_dynamodb, mock_table):
        """Test listing installations when none exist."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = []

        event = _authed_event(
            path="/workspaces/ws_test/github/installations",
            path_params={"id": "ws_test"},
        )

        result = handle_list_installations(event)
        body = json.loads(result["body"])
        assert body["installations"] == []


# ============================================================================
# List Installation Repos Handler
# ============================================================================

class TestHandleListInstallationRepos:
    """Test listing repos available via a GitHub installation."""

    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, 'get_installation_repos')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installation_repos_success(self, mock_db, mock_get_inst, mock_gh_repos, mock_list_repos,
                                              mock_dynamodb, mock_table, sample_installation):
        """Test listing repos from a GitHub installation."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_inst.return_value = sample_installation
        mock_gh_repos.return_value = [
            {
                "id": 1,
                "name": "my-repo",
                "full_name": "test-org/my-repo",
                "description": "A test repo",
                "private": True,
                "default_branch": "main",
            },
            {
                "id": 2,
                "name": "other-repo",
                "full_name": "test-org/other-repo",
                "description": "",
                "private": False,
                "default_branch": "develop",
            },
        ]
        mock_list_repos.return_value = [{"repo_name": "test-org/my-repo"}]

        event = _authed_event(
            path="/workspaces/ws_test/github/installations/12345/repos",
            path_params={"id": "ws_test", "installationId": "12345"},
        )

        result = handle_list_installation_repos(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["repos"]) == 2
        # First repo is already added
        r1 = next(r for r in body["repos"] if r["full_name"] == "test-org/my-repo")
        assert r1["already_added"] is True
        # Second repo is not added
        r2 = next(r for r in body["repos"] if r["full_name"] == "test-org/other-repo")
        assert r2["already_added"] is False

    def test_list_installation_repos_no_auth(self):
        """Test listing installation repos returns 401 without auth."""
        event = _make_event(
            path="/workspaces/ws_test/github/installations/12345/repos",
            path_params={"id": "ws_test", "installationId": "12345"},
        )
        result = handle_list_installation_repos(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installation_repos_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test listing installation repos returns 403 for non-member."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = _authed_event(
            path="/workspaces/ws_test/github/installations/12345/repos",
            path_params={"id": "ws_test", "installationId": "12345"},
        )
        result = handle_list_installation_repos(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installation_repos_installation_not_found(self, mock_db, mock_get_inst, mock_dynamodb, mock_table):
        """Test listing repos returns 404 when installation not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_inst.return_value = None

        event = _authed_event(
            path="/workspaces/ws_test/github/installations/99999/repos",
            path_params={"id": "ws_test", "installationId": "99999"},
        )
        result = handle_list_installation_repos(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'get_installation_repos')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_installation_repos_github_error(self, mock_db, mock_get_inst, mock_gh_repos,
                                                   mock_dynamodb, mock_table, sample_installation):
        """Test listing repos handles GitHub API error."""
        from github_app import GitHubAppError
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_inst.return_value = sample_installation
        mock_gh_repos.side_effect = GitHubAppError("Rate limit exceeded")

        event = _authed_event(
            path="/workspaces/ws_test/github/installations/12345/repos",
            path_params={"id": "ws_test", "installationId": "12345"},
        )
        result = handle_list_installation_repos(event)
        assert result["statusCode"] == 502


# ============================================================================
# Add Repos Handler
# ============================================================================

class TestHandleAddRepos:
    """Test adding repos to a workspace."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_success(self, mock_db, mock_get_inst, mock_get_repo, mock_save_repo,
                                mock_update_status, mock_queue_sync,
                                mock_dynamodb, mock_table, sample_installation):
        """Test adding repos to a workspace."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None  # Not already added

        repo_item = {
            "repo_name": "test-org/my-repo",
            "installation_id": "12345",
            "include_code_maps": True,
            "repo_type": "application",
        }
        mock_save_repo.return_value = repo_item

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {
                        "repo_name": "test-org/my-repo",
                        "installation_id": "12345",
                        "include_code_maps": True,
                    }
                ]
            },
        )

        with patch.object(
            handler_module, "get_repo_default_branch", return_value="main",
        ):
            result = handle_add_repos(event)
        assert result["statusCode"] == 201
        body = json.loads(result["body"])
        assert body["added"] == 1
        assert len(body["repos"]) == 1
        assert body["repos"][0]["repo_name"] == "test-org/my-repo"
        mock_save_repo.assert_called_once()
        mock_update_status.assert_called_once_with("ws_test", "test-org/my-repo", "queued")
        mock_queue_sync.assert_called_once()

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_resolves_default_branch_via_github_api(
        self, mock_db, mock_get_inst, mock_get_repo, mock_save_repo,
        mock_update_status, mock_queue_sync,
        mock_dynamodb, mock_table, sample_installation,
    ):
        """When the UI doesn't pass an explicit branch, resolve via
        the GitHub App's default-branch helper. This is the on-add
        detection that closes the "hardcoded main" gap for repos
        still on master / develop / trunk."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None
        mock_save_repo.return_value = {
            "repo_name": "corp/legacy-app",
            "installation_id": "12345",
            "include_code_maps": True,
            "repo_type": "application",
            "default_branch": "master",
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={"repos": [{
                "repo_name": "corp/legacy-app",
                "installation_id": "12345",
                "include_code_maps": True,
            }]},
        )

        with patch.object(
            handler_module, "get_repo_default_branch", return_value="master",
        ) as mock_resolve:
            result = handle_add_repos(event)

        assert result["statusCode"] == 201
        # The helper must have been called with (installation_id, owner, repo)
        mock_resolve.assert_called_once_with("12345", "corp", "legacy-app")
        # save_repo receives the resolved value.
        _, kwargs = mock_save_repo.call_args
        assert kwargs["default_branch"] == "master"
        # Response surfaces the branch so the UI can display it.
        body = json.loads(result["body"])
        assert body["repos"][0]["default_branch"] == "master"

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_ui_override_branch_wins_over_resolution(
        self, mock_db, mock_get_inst, mock_get_repo, mock_save_repo,
        mock_update_status, mock_queue_sync,
        mock_dynamodb, mock_table, sample_installation,
    ):
        """UI's confirmable-override widget: customer typed `develop`
        into the branch field before submit. Resolver must NOT run;
        UI value passes through."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None
        mock_save_repo.return_value = {
            "repo_name": "corp/app",
            "installation_id": "12345",
            "include_code_maps": True,
            "repo_type": "application",
            "default_branch": "develop",
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={"repos": [{
                "repo_name": "corp/app",
                "installation_id": "12345",
                "include_code_maps": True,
                "default_branch": "develop",
            }]},
        )

        with patch.object(
            handler_module, "get_repo_default_branch",
        ) as mock_resolve:
            handle_add_repos(event)
        mock_resolve.assert_not_called()
        _, kwargs = mock_save_repo.call_args
        assert kwargs["default_branch"] == "develop"

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_falls_back_to_main_on_resolution_failure(
        self, mock_db, mock_get_inst, mock_get_repo, mock_save_repo,
        mock_update_status, mock_queue_sync,
        mock_dynamodb, mock_table, sample_installation,
    ):
        """If the GitHub API call fails during on-add resolution, we
        don't reject the add -- we fall back to 'main'. A wrong
        branch surfaces loudly on first sync, which is a better
        failure mode than blocking add-repo on transient API errors."""
        from github_app import GitHubAppError

        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None
        mock_save_repo.return_value = {
            "repo_name": "corp/app",
            "installation_id": "12345",
            "include_code_maps": True,
            "repo_type": "application",
            "default_branch": "main",
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={"repos": [{
                "repo_name": "corp/app",
                "installation_id": "12345",
                "include_code_maps": True,
            }]},
        )
        with patch.object(
            handler_module, "get_repo_default_branch",
            side_effect=GitHubAppError("500 server error"),
        ):
            result = handle_add_repos(event)

        assert result["statusCode"] == 201
        _, kwargs = mock_save_repo.call_args
        assert kwargs["default_branch"] == "main"

    def test_add_repos_no_auth(self):
        """Test adding repos returns 401 without auth."""
        event = _make_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        )
        result = handle_add_repos(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test adding repos returns 403 for non-admin."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            body={"repos": [{"repo_name": "x", "installation_id": "1"}]},
        )
        result = handle_add_repos(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_invalid_json(self, mock_db, mock_dynamodb, mock_table):
        """Test adding repos returns 400 for invalid JSON body."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
        )
        event["body"] = "not valid json"

        result = handle_add_repos(event)
        assert result["statusCode"] == 400
        body = json.loads(result["body"])
        assert "Invalid JSON" in body["error"]

    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_empty_list(self, mock_db, mock_dynamodb, mock_table):
        """Test adding repos returns 400 when repos list is empty."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={"repos": []},
        )

        result = handle_add_repos(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_skips_already_added(self, mock_db, mock_get_inst, mock_get_repo,
                                            mock_dynamodb, mock_table, sample_installation, sample_repo):
        """Test adding repos skips repos that are already added."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = sample_repo  # Already exists

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {"repo_name": "test-org/my-repo", "installation_id": "12345"}
                ]
            },
        )

        result = handle_add_repos(event)
        body = json.loads(result["body"])
        assert body["added"] == 0

    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_skips_invalid_installation(self, mock_db, mock_get_inst, mock_dynamodb, mock_table):
        """Test adding repos skips repos with invalid installation."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = None  # Installation not found

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {"repo_name": "test-org/my-repo", "installation_id": "bad-inst"}
                ]
            },
        )

        result = handle_add_repos(event)
        body = json.loads(result["body"])
        assert body["added"] == 0

    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_skips_missing_fields(self, mock_db, mock_dynamodb, mock_table):
        """Test adding repos skips repos without required fields."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {"repo_name": "test-org/my-repo"},  # Missing installation_id
                    {"installation_id": "12345"},  # Missing repo_name
                ]
            },
        )

        result = handle_add_repos(event)
        body = json.loads(result["body"])
        assert body["added"] == 0

    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_no_sync_when_code_maps_disabled(self, mock_db, mock_get_inst, mock_get_repo,
                                                         mock_save_repo, mock_dynamodb, mock_table,
                                                         sample_installation):
        """Test adding repos does not queue sync when include_code_maps is False."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None

        repo_item = {
            "repo_name": "test-org/my-repo",
            "installation_id": "12345",
            "include_code_maps": False,
            "repo_type": "standards",
        }
        mock_save_repo.return_value = repo_item

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {
                        "repo_name": "test-org/my-repo",
                        "installation_id": "12345",
                        "include_code_maps": False,
                        "repo_type": "standards",
                    }
                ]
            },
        )

        with patch.object(handler_module, 'queue_sync_job') as mock_queue, \
             patch.object(handler_module, 'update_repo_sync_status') as mock_status:
            result = handle_add_repos(event)
            body = json.loads(result["body"])
            assert body["added"] == 1
            mock_queue.assert_not_called()
            mock_status.assert_not_called()

    @patch.object(handler_module, 'save_repo')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'get_installation')
    @patch.object(handler_module, '_get_dynamodb')
    def test_add_repos_accepts_full_name_key(self, mock_db, mock_get_inst, mock_get_repo,
                                              mock_save_repo, mock_dynamodb, mock_table,
                                              sample_installation):
        """Test adding repos accepts full_name as alternate key for repo_name."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_inst.return_value = sample_installation
        mock_get_repo.return_value = None

        repo_item = {
            "repo_name": "test-org/my-repo",
            "installation_id": "12345",
            "include_code_maps": True,
            "repo_type": "application",
        }
        mock_save_repo.return_value = repo_item

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
            email="admin@example.com",
            body={
                "repos": [
                    {
                        "full_name": "test-org/my-repo",
                        "installation_id": "12345",
                    }
                ]
            },
        )

        with patch.object(handler_module, 'queue_sync_job'), \
             patch.object(handler_module, 'update_repo_sync_status'):
            result = handle_add_repos(event)
            body = json.loads(result["body"])
            assert body["added"] == 1


# ============================================================================
# List Repos Handler
# ============================================================================

class TestHandleListRepos:
    """Test listing workspace repos."""

    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_success(self, mock_db, mock_list, mock_dynamodb, mock_table, sample_repo):
        """Test listing repos returns all fields."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [sample_repo]

        event = _authed_event(
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        )

        result = handle_list_repos(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["repos"]) == 1
        repo = body["repos"][0]
        assert repo["repo_name"] == "test-org/my-repo"
        assert repo["installation_id"] == "12345"
        assert repo["enabled"] is True
        assert repo["include_code_maps"] is True
        assert repo["repo_type"] == "application"
        assert repo["last_sync_status"] == "pending"

    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_includes_branch_sha_and_failure_reason(
        self, mock_db, mock_list, mock_dynamodb, mock_table, sample_repo,
    ):
        """The UI shows the sync branch chip, the commit, and why a repo
        is stalled; all three must come back from the list endpoint."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [{
            **sample_repo,
            "default_branch": "master",
            "last_commit_sha": "a" * 40,
            "last_sync_status": "stalled",
            "last_failure_reason": "units_failed",
        }]

        result = handle_list_repos(_authed_event(
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        ))
        repo = json.loads(result["body"])["repos"][0]
        assert repo["default_branch"] == "master"
        assert repo["last_commit_sha"] == "a" * 40
        assert repo["last_sync_status"] == "stalled"
        assert repo["last_failure_reason"] == "units_failed"

    def test_list_repos_no_auth(self):
        """Test listing repos returns 401 without auth."""
        event = _make_event(
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        )
        result = handle_list_repos(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test listing repos returns 403 for non-member."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = _authed_event(
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        )
        result = handle_list_repos(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, '_get_dynamodb')
    def test_list_repos_empty(self, mock_db, mock_list, mock_dynamodb, mock_table):
        """Test listing repos when none exist."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = []

        event = _authed_event(
            path="/workspaces/ws_test/github/repos",
            path_params={"id": "ws_test"},
        )

        result = handle_list_repos(event)
        body = json.loads(result["body"])
        assert body["repos"] == []


# ============================================================================
# Update Repo Handler
# ============================================================================

class TestHandleUpdateRepo:
    """Test updating repo settings."""

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'update_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_success(self, mock_db, mock_update, mock_get_repo,
                                  mock_dynamodb, mock_table, sample_repo):
        """Test updating repo settings."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        # First call: get_repo to check existence, second call: get_repo after update
        mock_get_repo.side_effect = [sample_repo, {**sample_repo, "include_code_maps": False}]

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="admin@example.com",
            body={"include_code_maps": False},
        )

        result = handle_update_repo(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert "repo" in body
        mock_update.assert_called_once()

    def test_update_repo_no_auth(self):
        """Test updating repo returns 401 without auth."""
        event = _make_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
        )
        result = handle_update_repo(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'update_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_accepts_default_branch(
        self, mock_db, mock_update, mock_get_repo,
        mock_dynamodb, mock_table, sample_repo,
    ):
        """The PATCH allow-list is widened to accept default_branch.
        Admin retargets a repo's sync branch (e.g. master -> main after
        an org-wide cutover) without remove/re-add."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.side_effect = [
            sample_repo,
            {**sample_repo, "default_branch": "develop"},
        ]

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="admin@example.com",
            body={"default_branch": "develop"},
        )
        result = handle_update_repo(event)
        assert result["statusCode"] == 200
        _, kwargs = mock_update.call_args
        assert kwargs["default_branch"] == "develop"
        body = json.loads(result["body"])
        assert body["repo"]["default_branch"] == "develop"

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'update_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_default_branch_strips_whitespace_and_treats_empty_as_none(
        self, mock_db, mock_update, mock_get_repo,
        mock_dynamodb, mock_table, sample_repo,
    ):
        # Whitespace-only submissions should not overwrite a real
        # branch name with "" -- treat as "no change" for safety.
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.side_effect = [sample_repo, sample_repo]
        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="admin@example.com",
            body={"default_branch": "   "},
        )
        handle_update_repo(event)
        _, kwargs = mock_update.call_args
        assert kwargs["default_branch"] is None


class TestHandleListRepoBranches:
    """New GET /repos/{name}/branches endpoint. Returns available
    branches + provider's current default so the edit-branch modal
    can render a dropdown + drift-detect badge without a client-side
    GitHub call."""

    @patch.object(handler_module, 'get_repo_default_branch')
    @patch.object(handler_module, 'list_repo_branches')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_success_returns_branches_current_default_and_stored(
        self, mock_db, mock_get_repo, mock_list, mock_default,
        mock_dynamodb, mock_table,
    ):
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = {
            "repo_name": "corp/legacy-app",
            "installation_id": "12345",
            "default_branch": "master",  # what we stored
        }
        mock_list.return_value = [
            {"name": "master", "protected": True},
            {"name": "main", "protected": False},
            {"name": "develop", "protected": False},
        ]
        mock_default.return_value = "main"  # what GitHub says now

        event = _authed_event(
            method="GET",
            path="/workspaces/ws_test/github/repos/corp/legacy-app/branches",
            path_params={"id": "ws_test", "repoName": "corp/legacy-app"},
            email="admin@example.com",
        )
        result = handler_module.handle_list_repo_branches(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["branches"]) == 3
        assert body["current_default"] == "main"
        assert body["stored_default"] == "master"  # drift visible to UI
        mock_list.assert_called_once_with("12345", "corp", "legacy-app")

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_returns_404_when_repo_not_in_workspace(
        self, mock_db, mock_get_repo, mock_dynamodb, mock_table,
    ):
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = None
        event = _authed_event(
            method="GET",
            path="/workspaces/ws_test/github/repos/corp/missing/branches",
            path_params={"id": "ws_test", "repoName": "corp/missing"},
            email="admin@example.com",
        )
        result = handler_module.handle_list_repo_branches(event)
        assert result["statusCode"] == 404

    def test_returns_401_without_auth(self):
        event = _make_event(
            method="GET",
            path="/workspaces/ws_test/github/repos/corp/x/branches",
            path_params={"id": "ws_test", "repoName": "corp/x"},
        )
        result = handler_module.handle_list_repo_branches(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test updating repo returns 403 for non-admin."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            body={"enabled": False},
        )
        result = handle_update_repo(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_not_found(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test updating repo returns 404 when not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = None

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/nonexistent",
            path_params={"id": "ws_test", "repoName": "test-org/nonexistent"},
            email="admin@example.com",
            body={"enabled": False},
        )
        result = handle_update_repo(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_missing_name(self, mock_db, mock_dynamodb, mock_table):
        """Test updating repo returns 400 when repo name is missing."""
        mock_db.return_value = mock_dynamodb

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/",
            path_params={"id": "ws_test", "repoName": ""},
            email="admin@example.com",
            body={"enabled": False},
        )
        result = handle_update_repo(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_invalid_json(self, mock_db, mock_dynamodb, mock_table):
        """Test updating repo returns 400 for invalid JSON."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="admin@example.com",
        )
        event["body"] = "not json"

        with patch.object(handler_module, 'get_repo', return_value={"repo_name": "test-org/my-repo"}):
            result = handle_update_repo(event)
            assert result["statusCode"] == 400

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, 'update_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_update_repo_url_decodes_name(self, mock_db, mock_update, mock_get_repo,
                                           mock_dynamodb, mock_table, sample_repo):
        """Test updating repo URL-decodes the repo name."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.side_effect = [sample_repo, sample_repo]

        event = _authed_event(
            method="PUT",
            path="/workspaces/ws_test/github/repos/test-org%2Fmy-repo",
            path_params={"id": "ws_test", "repoName": "test-org%2Fmy-repo"},
            email="admin@example.com",
            body={"enabled": True},
        )

        result = handle_update_repo(event)
        assert result["statusCode"] == 200
        # Verify URL-decoded name was used
        mock_get_repo.assert_any_call("ws_test", "test-org/my-repo")


# ============================================================================
# Delete Repo Handler
# ============================================================================

class TestHandleDeleteRepo:
    """Test deleting a repo from workspace."""

    @patch.object(handler_module, 'delete_repo')
    @patch.object(handler_module, 'queue_delete_job')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo_success(self, mock_db, mock_get_repo, mock_queue_del,
                                  mock_del_repo, mock_dynamodb, mock_table, sample_repo):
        """Test deleting a repo queues deletion and removes record."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = sample_repo

        event = _authed_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="admin@example.com",
        )

        result = handle_delete_repo(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["message"] == "Repo removed"
        # Should use full name (owner/repo) matching how github-sync stores files
        mock_queue_del.assert_called_once_with("ws_test", "test-org/my-repo")
        mock_del_repo.assert_called_once_with("ws_test", "test-org/my-repo")

    @patch.object(handler_module, 'delete_repo')
    @patch.object(handler_module, 'queue_delete_job')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo_without_slash_uses_full_name(self, mock_db, mock_get_repo, mock_queue_del,
                                                       mock_del_repo, mock_dynamodb, mock_table):
        """Test deleting a repo without slash uses full name for deletion."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = {"repo_name": "simple-repo"}

        event = _authed_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/simple-repo",
            path_params={"id": "ws_test", "repoName": "simple-repo"},
            email="admin@example.com",
        )

        result = handle_delete_repo(event)
        assert result["statusCode"] == 200
        # Without slash, uses full name
        mock_queue_del.assert_called_once_with("ws_test", "simple-repo")

    def test_delete_repo_no_auth(self):
        """Test deleting repo returns 401 without auth."""
        event = _make_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
        )
        result = handle_delete_repo(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo_not_admin(self, mock_db, mock_dynamodb, mock_table):
        """Test deleting repo returns 403 for non-admin."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [
            {"Item": {"role": "member"}},
            {},
        ]

        event = _authed_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/test-org/my-repo",
            path_params={"id": "ws_test", "repoName": "test-org/my-repo"},
            email="user@example.com",
        )
        result = handle_delete_repo(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo_not_found(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test deleting repo returns 404 when not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "workspace_admin"}}
        mock_get_repo.return_value = None

        event = _authed_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/test-org/nonexistent",
            path_params={"id": "ws_test", "repoName": "test-org/nonexistent"},
            email="admin@example.com",
        )
        result = handle_delete_repo(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, '_get_dynamodb')
    def test_delete_repo_missing_name(self, mock_db, mock_dynamodb, mock_table):
        """Test deleting repo returns 400 when name is missing."""
        mock_db.return_value = mock_dynamodb

        event = _authed_event(
            method="DELETE",
            path="/workspaces/ws_test/github/repos/",
            path_params={"id": "ws_test", "repoName": ""},
            email="admin@example.com",
        )
        result = handle_delete_repo(event)
        assert result["statusCode"] == 400


# ============================================================================
# Trigger Sync Handler
# ============================================================================

class TestHandleTriggerSync:
    """Test triggering manual sync."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_all_repos(self, mock_db, mock_list, mock_update_status,
                             mock_queue, mock_dynamodb, mock_table):
        """Test syncing all repos in a workspace."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [
            {
                "repo_name": "test-org/repo1",
                "installation_id": "12345",
                "include_code_maps": True,
                "last_sync_status": "success",
            },
            {
                "repo_name": "test-org/repo2",
                "installation_id": "12345",
                "include_code_maps": True,
                "last_sync_status": "pending",
            },
            {
                "repo_name": "test-org/standards",
                "installation_id": "12345",
                "include_code_maps": False,
                "last_sync_status": "success",
            },
        ]

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["repos"] == 2  # Only repos with code maps enabled
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_single_repo(self, mock_db, mock_get_repo, mock_update_status,
                               mock_queue, mock_dynamodb, mock_table, sample_repo):
        """Test syncing a single repo."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_repo.return_value = sample_repo

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={"repo_name": "test-org/my-repo"},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["repos"] == 1
        mock_queue.assert_called_once()

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_single_repo_not_found(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test syncing a single repo returns 404 when not found."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_repo.return_value = None

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={"repo_name": "test-org/nonexistent"},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 404

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_single_repo_code_maps_disabled(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test syncing a single repo returns 400 when code maps disabled."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_repo.return_value = {
            "repo_name": "test-org/standards",
            "include_code_maps": False,
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={"repo_name": "test-org/standards"},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_single_repo_already_in_progress(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test syncing a repo that is already syncing returns 200 with status."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_repo.return_value = {
            "repo_name": "test-org/my-repo",
            "include_code_maps": True,
            "last_sync_status": "in_progress",
            "installation_id": "12345",
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={"repo_name": "test-org/my-repo"},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "in_progress"

    @patch.object(handler_module, 'get_repo')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_single_repo_already_queued(self, mock_db, mock_get_repo, mock_dynamodb, mock_table):
        """Test syncing a repo that is already queued returns 200 with status."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_get_repo.return_value = {
            "repo_name": "test-org/my-repo",
            "include_code_maps": True,
            "last_sync_status": "queued",
            "installation_id": "12345",
        }

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={"repo_name": "test-org/my-repo"},
        )

        result = handle_trigger_sync(event)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["status"] == "queued"

    def test_sync_no_auth(self):
        """Test syncing returns 401 without auth."""
        event = _make_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
        )
        result = handle_trigger_sync(event)
        assert result["statusCode"] == 401

    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_not_member(self, mock_db, mock_dynamodb, mock_table):
        """Test syncing returns 403 for non-member."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.side_effect = [{}, {}]

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={},
        )
        result = handle_trigger_sync(event)
        assert result["statusCode"] == 403

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'update_repo_sync_status')
    @patch.object(handler_module, 'list_repos')
    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_all_skips_already_syncing(self, mock_db, mock_list, mock_update_status,
                                             mock_queue, mock_dynamodb, mock_table):
        """Test sync all skips repos already queued or in_progress."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}
        mock_list.return_value = [
            {
                "repo_name": "test-org/repo1",
                "installation_id": "12345",
                "include_code_maps": True,
                "last_sync_status": "queued",
            },
            {
                "repo_name": "test-org/repo2",
                "installation_id": "12345",
                "include_code_maps": True,
                "last_sync_status": "in_progress",
            },
            {
                "repo_name": "test-org/repo3",
                "installation_id": "12345",
                "include_code_maps": True,
                "last_sync_status": "success",
            },
        ]

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
            body={},
        )

        result = handle_trigger_sync(event)
        body = json.loads(result["body"])
        assert body["repos"] == 1  # Only repo3 is eligible
        mock_queue.assert_called_once()

    @patch.object(handler_module, '_get_dynamodb')
    def test_sync_invalid_json_body_uses_empty(self, mock_db, mock_dynamodb, mock_table):
        """Test sync with invalid JSON body defaults to empty dict (sync all)."""
        mock_db.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {"role": "member"}}

        event = _authed_event(
            method="POST",
            path="/workspaces/ws_test/github/sync",
            path_params={"id": "ws_test"},
        )
        event["body"] = "not json"

        with patch.object(handler_module, 'list_repos', return_value=[]):
            result = handle_trigger_sync(event)
            assert result["statusCode"] == 200
            body = json.loads(result["body"])
            assert body["repos"] == 0


# ============================================================================
# Lambda Handler Routing
# ============================================================================

class TestLambdaHandlerRouting:
    """Test main handler routes requests correctly."""

    @patch.object(handler_module, 'handle_authorize')
    def test_routes_authorize(self, mock_fn):
        """Test routing GET /github/authorize."""
        mock_fn.return_value = _response(200, {"install_url": "https://..."})

        event = {
            "httpMethod": "GET",
            "path": "/github/authorize",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_get(self, mock_fn):
        """Test routing GET /github/callback."""
        mock_fn.return_value = _redirect("https://app.example.com/workspaces")

        event = {
            "httpMethod": "GET",
            "path": "/github/callback",
            "headers": {},
            "queryStringParameters": {"installation_id": "123", "state": "{}"},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_callback')
    def test_routes_callback_post(self, mock_fn):
        """Test routing POST /github/callback."""
        mock_fn.return_value = _redirect("https://app.example.com/workspaces")

        event = {
            "httpMethod": "POST",
            "path": "/github/callback",
            "headers": {},
            "queryStringParameters": {},
            "body": json.dumps({"installation_id": "123", "state": "{}"}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_list_installations')
    def test_routes_list_installations(self, mock_fn):
        """Test routing GET /workspaces/{id}/github/installations."""
        mock_fn.return_value = _response(200, {"installations": []})

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/github/installations",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()
        # Verify pathParameters were set
        assert event["pathParameters"]["id"] == "ws1"

    @patch.object(handler_module, 'handle_list_installation_repos')
    def test_routes_list_installation_repos(self, mock_fn):
        """Test routing GET /workspaces/{id}/github/installations/{instId}/repos."""
        mock_fn.return_value = _response(200, {"repos": []})

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/github/installations/inst123/repos",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()
        assert event["pathParameters"]["id"] == "ws1"
        assert event["pathParameters"]["installationId"] == "inst123"

    @patch.object(handler_module, 'handle_list_repos')
    def test_routes_list_repos_get(self, mock_fn):
        """Test routing GET /workspaces/{id}/github/repos."""
        mock_fn.return_value = _response(200, {"repos": []})

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/github/repos",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_add_repos')
    def test_routes_add_repos_post(self, mock_fn):
        """Test routing POST /workspaces/{id}/github/repos."""
        mock_fn.return_value = _response(201, {"added": 1, "repos": []})

        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/github/repos",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {},
            "body": json.dumps({"repos": []}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_update_repo')
    def test_routes_update_repo_put(self, mock_fn):
        """Test routing PUT /workspaces/{id}/github/repos/{name}."""
        mock_fn.return_value = _response(200, {"repo": {}})

        event = {
            "httpMethod": "PUT",
            "path": "/workspaces/ws1/github/repos/test-org/my-repo",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {},
            "body": json.dumps({"enabled": False}),
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()
        # Repo name should include the slash-separated parts
        assert event["pathParameters"]["repoName"] == "test-org/my-repo"

    @patch.object(handler_module, 'handle_delete_repo')
    def test_routes_delete_repo(self, mock_fn):
        """Test routing DELETE /workspaces/{id}/github/repos/{name}."""
        mock_fn.return_value = _response(200, {"message": "Repo removed"})

        event = {
            "httpMethod": "DELETE",
            "path": "/workspaces/ws1/github/repos/test-org/my-repo",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()

    @patch.object(handler_module, 'handle_trigger_sync')
    def test_routes_trigger_sync(self, mock_fn):
        """Test routing POST /workspaces/{id}/github/sync."""
        mock_fn.return_value = _response(200, {"message": "Sync queued", "repos": 1})

        event = {
            "httpMethod": "POST",
            "path": "/workspaces/ws1/github/sync",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        lambda_handler(event, None)
        mock_fn.assert_called_once()
        assert event["pathParameters"]["id"] == "ws1"

    def test_unknown_route_returns_404(self):
        """Test unknown routes return 404."""
        event = {
            "httpMethod": "GET",
            "path": "/unknown/route",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_wrong_method_returns_404(self):
        """Test wrong HTTP method for known path returns 404."""
        event = {
            "httpMethod": "DELETE",
            "path": "/github/authorize",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404

    def test_routing_with_request_context_http(self):
        """Test routing works with requestContext.http format (Lambda Function URL)."""
        event = {
            "requestContext": {"http": {"method": "GET", "path": "/unknown/path"}},
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 404


# ============================================================================
# Error Handling in Main Handler
# ============================================================================

class TestLambdaHandlerErrorHandling:
    """Test error handling in the main Lambda handler."""

    @patch.object(handler_module, 'handle_authorize')
    def test_dynamodb_client_error_returns_500(self, mock_fn):
        """Test DynamoDB ClientError returns 500."""
        mock_fn.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "GetItem"
        )

        event = {
            "httpMethod": "GET",
            "path": "/github/authorize",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {"workspace_id": "ws_test"},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert body["error"] == "Database error"

    @patch.object(handler_module, 'handle_list_installations')
    def test_github_app_error_returns_error(self, mock_fn):
        """Test GitHubAppError returns server error."""
        from github_app import GitHubAppError
        mock_fn.side_effect = GitHubAppError("API rate limit exceeded")

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/github/installations",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] >= 500
        body = json.loads(result["body"])
        assert "error" in body

    @patch.object(handler_module, 'handle_list_repos')
    def test_unhandled_error_returns_500(self, mock_fn):
        """Test unhandled exceptions return 500."""
        mock_fn.side_effect = RuntimeError("Something unexpected")

        event = {
            "httpMethod": "GET",
            "path": "/workspaces/ws1/github/repos",
            "headers": {"x-user-email": "user@example.com"},
            "queryStringParameters": {},
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "error" in body

