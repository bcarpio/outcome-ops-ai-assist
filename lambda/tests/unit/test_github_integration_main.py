# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for github-integration/main.py FastAPI entry point.

Tests cover:
- Health check endpoint
- OAuth routes (authorize, callback GET/POST)
- Installation routes (list installations, list repos)
- Repo CRUD routes (list, add, update, delete)
- Sync route (trigger sync)
- Redirect handling in _parse_response
- Request parsing (headers, query params, body forwarded correctly)
- Error handling (handler exceptions and error status codes)
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Guard: skip entire module if FastAPI / starlette / httpx are unavailable
_SKIP_REASON = None
try:
    from starlette.testclient import TestClient  # noqa: F401
except (ImportError, RuntimeError) as exc:
    _SKIP_REASON = f"starlette TestClient unavailable: {exc}"

if _SKIP_REASON is None:
    try:
        _mod_name = "github_integration_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(
            os.path.dirname(__file__), "../../github-integration/main.py"
        )
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load github-integration/main.py: {exc}"

if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)


# ============================================================================
# Helpers
# ============================================================================


def _mock_lambda_response(status_code=200, body=None, headers=None):
    """Build a mock Lambda handler response dict."""
    if body is None:
        body = {"message": "ok"}
    result = {"statusCode": status_code, "body": json.dumps(body)}
    if headers:
        result["headers"] = headers
    return result


def _mock_redirect_response(location="/"):
    """Build a mock Lambda redirect response."""
    return {
        "statusCode": 302,
        "headers": {"Location": location},
        "body": json.dumps({}),
    }


def _setup_mock_handler(return_value):
    """Register a mock handler module in sys.modules and return the mock."""
    mock_handler = MagicMock(return_value=return_value)
    sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
    return mock_handler


def _cleanup_handler():
    """Remove mock handler from sys.modules."""
    if "handler" in sys.modules:
        del sys.modules["handler"]


# ============================================================================
# Tests
# ============================================================================


class TestHealthCheck:
    """Test the /health endpoint."""

    def test_health_returns_200(self):
        """Health check returns 200 with healthy status."""
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "healthy"}

    def test_health_no_handler_dependency(self):
        """Health check works without handler module loaded."""
        _cleanup_handler()
        client = TestClient(app)
        response = client.get("/health")
        assert response.status_code == 200


class TestBuildEvent:
    """Test the _build_event helper function."""

    def test_builds_event_with_all_fields(self):
        """Event contains body, headers, queryStringParameters, and requestContext."""
        mock_response = _mock_lambda_response(200, {"url": "https://github.com"})
        mock_handler = _setup_mock_handler(mock_response)
        try:
            client = TestClient(app)
            client.get("/github/authorize")

            event = mock_handler.call_args[0][0]
            assert "body" in event
            assert "headers" in event
            assert "queryStringParameters" in event
            assert "requestContext" in event
            assert event["requestContext"]["http"]["method"] == "GET"
            assert event["requestContext"]["http"]["path"] == "/github/authorize"
        finally:
            _cleanup_handler()

    def test_query_params_forwarded(self):
        """Query parameters are forwarded into the event."""
        mock_response = _mock_lambda_response(200, {})
        mock_handler = _setup_mock_handler(mock_response)
        try:
            client = TestClient(app)
            client.get("/github/authorize?state=abc&code=xyz")

            event = mock_handler.call_args[0][0]
            assert event["queryStringParameters"]["state"] == "abc"
            assert event["queryStringParameters"]["code"] == "xyz"
        finally:
            _cleanup_handler()

    def test_headers_forwarded(self):
        """Request headers are forwarded into the event."""
        mock_response = _mock_lambda_response(200, {})
        mock_handler = _setup_mock_handler(mock_response)
        try:
            client = TestClient(app)
            client.get(
                "/github/authorize",
                headers={"Authorization": "Bearer test-token"},
            )

            event = mock_handler.call_args[0][0]
            assert event["headers"]["authorization"] == "Bearer test-token"
        finally:
            _cleanup_handler()


class TestParseResponse:
    """Test the _parse_response helper function."""

    def test_parses_json_response(self):
        """Standard JSON response is parsed correctly."""
        result = main_module._parse_response(
            {"statusCode": 200, "body": json.dumps({"data": "value"})}
        )
        assert result.status_code == 200

    def test_parses_error_status(self):
        """Error status codes are forwarded."""
        result = main_module._parse_response(
            {"statusCode": 400, "body": json.dumps({"error": "bad request"})}
        )
        assert result.status_code == 400

    def test_handles_redirect(self):
        """302 responses return a RedirectResponse."""
        result = main_module._parse_response(
            _mock_redirect_response("https://github.com/apps/install")
        )
        assert result.status_code == 302

    def test_redirect_preserves_location(self):
        """Redirect response uses the Location header from Lambda."""
        result = main_module._parse_response(
            _mock_redirect_response("https://example.com/callback")
        )
        assert result.headers.get("location") == "https://example.com/callback"

    def test_defaults_to_200(self):
        """Defaults to 200 when statusCode is missing."""
        result = main_module._parse_response({"body": json.dumps({})})
        assert result.status_code == 200


class TestGitHubAuthorize:
    """Test GET /github/authorize endpoint."""

    def test_returns_authorization_url(self):
        """Authorize returns the GitHub App installation URL."""
        body = {"url": "https://github.com/apps/my-app/installations/new"}
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, body))
        try:
            client = TestClient(app)
            response = client.get("/github/authorize")
            assert response.status_code == 200
            assert response.json()["url"] == body["url"]
        finally:
            _cleanup_handler()

    def test_event_method_and_path(self):
        """Event has GET method and /github/authorize path."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/github/authorize")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "GET"
            assert event["requestContext"]["http"]["path"] == "/github/authorize"
        finally:
            _cleanup_handler()


class TestGitHubCallbackGet:
    """Test GET /github/callback endpoint."""

    def test_callback_with_query_params(self):
        """Callback passes installation_id and setup_action as query params."""
        mock_handler = _setup_mock_handler(
            _mock_redirect_response("https://myapp.com/dashboard")
        )
        try:
            client = TestClient(app, follow_redirects=False)
            response = client.get(
                "/github/callback?installation_id=12345&setup_action=install"
            )
            assert response.status_code == 302

            event = mock_handler.call_args[0][0]
            assert event["queryStringParameters"]["installation_id"] == "12345"
            assert event["queryStringParameters"]["setup_action"] == "install"
        finally:
            _cleanup_handler()

    def test_callback_event_path(self):
        """Callback event has correct path."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/github/callback")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["path"] == "/github/callback"
        finally:
            _cleanup_handler()


class TestGitHubCallbackPost:
    """Test POST /github/callback endpoint."""

    def test_post_callback_forwards_body(self):
        """POST callback forwards request body in event."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {"ok": True}))
        try:
            client = TestClient(app)
            body = {"installation_id": "12345", "state": "abc"}
            client.post("/github/callback", json=body)

            event = mock_handler.call_args[0][0]
            parsed_body = json.loads(event["body"])
            assert parsed_body["installation_id"] == "12345"
        finally:
            _cleanup_handler()

    def test_post_callback_uses_post_method(self):
        """POST callback event uses POST method."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.post("/github/callback", json={})

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
        finally:
            _cleanup_handler()


class TestListInstallations:
    """Test GET /workspaces/{workspace_id}/github/installations endpoint."""

    def test_returns_installations(self):
        """Returns list of installations from handler."""
        installations = [{"id": "inst-1", "account": "org-1"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"installations": installations})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/github/installations")
            assert response.status_code == 200
            assert response.json()["installations"] == installations
        finally:
            _cleanup_handler()

    def test_event_path_includes_workspace_id(self):
        """Event path includes the workspace ID."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc/github/installations")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/github/installations"
            )
        finally:
            _cleanup_handler()


class TestListInstallationRepos:
    """Test GET /workspaces/{workspace_id}/github/installations/{installation_id}/repos."""

    def test_returns_repos(self):
        """Returns list of repos for an installation."""
        repos = [{"name": "my-repo", "full_name": "org/my-repo"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"repos": repos})
        )
        try:
            client = TestClient(app)
            response = client.get(
                "/workspaces/ws-1/github/installations/inst-1/repos"
            )
            assert response.status_code == 200
            assert response.json()["repos"] == repos
        finally:
            _cleanup_handler()

    def test_event_path_includes_both_ids(self):
        """Event path includes both workspace and installation IDs."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc/github/installations/inst-xyz/repos")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/github/installations/inst-xyz/repos"
            )
        finally:
            _cleanup_handler()


class TestListRepos:
    """Test GET /workspaces/{workspace_id}/github/repos endpoint."""

    def test_returns_repos(self):
        """Returns list of repos for workspace."""
        repos = [{"name": "org/repo-1", "type": "application"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"repos": repos})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/github/repos")
            assert response.status_code == 200
            assert response.json()["repos"] == repos
        finally:
            _cleanup_handler()

    def test_handler_error_propagates(self):
        """Handler error status is returned to client."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(403, {"error": "Not authorized"})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/github/repos")
            assert response.status_code == 403
        finally:
            _cleanup_handler()


class TestAddRepos:
    """Test POST /workspaces/{workspace_id}/github/repos endpoint."""

    def test_adds_repos(self):
        """Add repos forwards body and returns handler response."""
        body = {"repos": [{"name": "org/repo-1", "type": "application"}]}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"added": 1})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces/ws-1/github/repos", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            parsed_body = json.loads(event["body"])
            assert parsed_body["repos"][0]["name"] == "org/repo-1"
        finally:
            _cleanup_handler()

    def test_event_path_includes_workspace_id(self):
        """Event path includes the workspace ID."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.post("/workspaces/ws-xyz/github/repos", json={"repos": []})

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-xyz/github/repos"
            )
        finally:
            _cleanup_handler()


class TestUpdateRepo:
    """Test PUT /workspaces/{workspace_id}/github/repos/{repo_name} endpoint."""

    def test_updates_repo(self):
        """Update repo forwards body and returns handler response."""
        body = {"type": "standards"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"updated": True})
        )
        try:
            client = TestClient(app)
            response = client.put(
                "/workspaces/ws-1/github/repos/org/my-repo", json=body
            )
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "PUT"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-1/github/repos/org/my-repo"
            )
        finally:
            _cleanup_handler()

    def test_repo_name_with_slash(self):
        """Repo name containing slashes is preserved in path."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.put(
                "/workspaces/ws-1/github/repos/my-org/my-repo", json={"type": "app"}
            )

            event = mock_handler.call_args[0][0]
            assert "my-org/my-repo" in event["requestContext"]["http"]["path"]
        finally:
            _cleanup_handler()


class TestDeleteRepo:
    """Test DELETE /workspaces/{workspace_id}/github/repos/{repo_name} endpoint."""

    def test_deletes_repo(self):
        """Delete repo returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"deleted": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/github/repos/org/my-repo")
            assert response.status_code == 200
        finally:
            _cleanup_handler()

    def test_delete_event_method(self):
        """Delete event uses DELETE method."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1/github/repos/org/my-repo")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
        finally:
            _cleanup_handler()

    def test_delete_not_found(self):
        """Delete of nonexistent repo returns 404."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(404, {"error": "Repo not found"})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/github/repos/org/nope")
            assert response.status_code == 404
        finally:
            _cleanup_handler()


class TestTriggerSync:
    """Test POST /workspaces/{workspace_id}/github/sync endpoint."""

    def test_trigger_sync_success(self):
        """Trigger sync returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"synced": 3})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces/ws-1/github/sync", json={"repos": ["org/repo-1"]})
            assert response.status_code == 200
            assert response.json()["synced"] == 3
        finally:
            _cleanup_handler()

    def test_trigger_sync_event(self):
        """Sync event has POST method and correct path."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.post("/workspaces/ws-abc/github/sync", json={})

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/github/sync"
            )
        finally:
            _cleanup_handler()

    def test_trigger_sync_with_empty_body(self):
        """Sync with no body sends empty string."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.post("/workspaces/ws-1/github/sync")

            event = mock_handler.call_args[0][0]
            # Body should be empty string or the raw bytes
            assert event["body"] is not None
        finally:
            _cleanup_handler()

    def test_trigger_sync_handler_error(self):
        """Sync handler error status is returned."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(500, {"error": "Sync failed"})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces/ws-1/github/sync", json={})
            assert response.status_code == 500
        finally:
            _cleanup_handler()


class TestHandlerCalledWithNoneContext:
    """Verify all routes pass None as the Lambda context."""

    def test_authorize_passes_none_context(self):
        """Authorize passes None as context."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/github/authorize")
            assert mock_handler.call_args[0][1] is None
        finally:
            _cleanup_handler()

    def test_list_repos_passes_none_context(self):
        """List repos passes None as context."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-1/github/repos")
            assert mock_handler.call_args[0][1] is None
        finally:
            _cleanup_handler()

    def test_add_repos_passes_none_context(self):
        """Add repos passes None as context."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.post("/workspaces/ws-1/github/repos", json={"repos": []})
            assert mock_handler.call_args[0][1] is None
        finally:
            _cleanup_handler()
