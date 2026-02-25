"""Unit tests for workspace-management/main.py FastAPI entry point.

Tests cover:
- Health check endpoint
- Workspace CRUD routes (create, list, get, update, delete)
- Member routes (add, list, update, remove)
- Repo routes (list available, list, add, remove)
- Sharing routes (get, update, list available)
- Org management routes (list, add, update, remove members)
- Chat routes (searchable workspaces)
- Document routes (upload URL, list, delete)
- Request parsing (headers, body, query params forwarded correctly)
- Error handling (handler error status codes)
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
        _mod_name = "workspace_management_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(
            os.path.dirname(__file__), "../../workspace-management/main.py"
        )
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load workspace-management/main.py: {exc}"

if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)


# ============================================================================
# Helpers
# ============================================================================


def _mock_lambda_response(status_code=200, body=None):
    """Build a mock Lambda handler response dict."""
    if body is None:
        body = {"message": "ok"}
    return {"statusCode": status_code, "body": json.dumps(body)}


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
# Health Check
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


# ============================================================================
# _build_event / _parse_response helpers
# ============================================================================


class TestBuildEvent:
    """Test _build_event helper through route invocations."""

    def test_event_contains_body(self):
        """POST events include the request body."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            body = {"name": "My Workspace"}
            client.post("/workspaces", json=body)

            event = mock_handler.call_args[0][0]
            parsed_body = json.loads(event["body"])
            assert parsed_body["name"] == "My Workspace"
        finally:
            _cleanup_handler()

    def test_event_contains_headers(self):
        """Events include request headers."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": []})
        )
        try:
            client = TestClient(app)
            client.get(
                "/workspaces",
                headers={"Authorization": "Bearer tok-123"},
            )

            event = mock_handler.call_args[0][0]
            assert event["headers"]["authorization"] == "Bearer tok-123"
        finally:
            _cleanup_handler()

    def test_event_method_and_path(self):
        """Event has correct method and path."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "GET"
            assert event["requestContext"]["http"]["path"] == "/workspaces"
        finally:
            _cleanup_handler()

    def test_get_event_has_empty_body(self):
        """GET events have empty body string."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces")

            event = mock_handler.call_args[0][0]
            assert event["body"] == ""
        finally:
            _cleanup_handler()


class TestParseResponse:
    """Test _parse_response helper function."""

    def test_parses_200_response(self):
        """200 response is parsed correctly."""
        result = main_module._parse_response(
            {"statusCode": 200, "body": json.dumps({"data": "val"})}
        )
        assert result.status_code == 200

    def test_parses_error_response(self):
        """Error status codes are forwarded."""
        result = main_module._parse_response(
            {"statusCode": 403, "body": json.dumps({"error": "forbidden"})}
        )
        assert result.status_code == 403

    def test_defaults_to_200(self):
        """Defaults to 200 when statusCode is missing."""
        result = main_module._parse_response({"body": json.dumps({})})
        assert result.status_code == 200

    def test_defaults_to_empty_body(self):
        """Defaults to empty JSON object when body is missing."""
        result = main_module._parse_response({"statusCode": 200})
        assert result.status_code == 200


# ============================================================================
# Workspace CRUD Routes
# ============================================================================


class TestCreateWorkspace:
    """Test POST /workspaces endpoint."""

    def test_creates_workspace(self):
        """Create workspace forwards body and returns handler response."""
        body = {"name": "Test Workspace", "description": "A test"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"id": "ws-new", "name": "Test Workspace"})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces", json=body)
            assert response.status_code == 200
            assert response.json()["id"] == "ws-new"

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert event["requestContext"]["http"]["path"] == "/workspaces"
            parsed_body = json.loads(event["body"])
            assert parsed_body["name"] == "Test Workspace"
        finally:
            _cleanup_handler()

    def test_handler_error_propagates(self):
        """Handler error status is returned to client."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(400, {"error": "Name required"})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces", json={})
            assert response.status_code == 400
        finally:
            _cleanup_handler()


class TestListWorkspaces:
    """Test GET /workspaces endpoint."""

    def test_returns_workspaces(self):
        """List returns workspaces from handler."""
        workspaces = [{"id": "ws-1", "name": "Workspace 1"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": workspaces})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces")
            assert response.status_code == 200
            assert response.json()["workspaces"] == workspaces
        finally:
            _cleanup_handler()

    def test_handler_called_with_none_context(self):
        """lambda_handler is called with None as the context arg."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces")
            context_arg = mock_handler.call_args[0][1]
            assert context_arg is None
        finally:
            _cleanup_handler()


class TestGetWorkspace:
    """Test GET /workspaces/{workspace_id} endpoint."""

    def test_returns_workspace(self):
        """Get workspace returns workspace details."""
        ws = {"id": "ws-123", "name": "My WS", "members": []}
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, ws))
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-123")
            assert response.status_code == 200
            assert response.json()["id"] == "ws-123"
        finally:
            _cleanup_handler()

    def test_event_path_includes_workspace_id(self):
        """Event path includes the workspace ID."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"] == "/workspaces/ws-abc"
            )
        finally:
            _cleanup_handler()

    def test_not_found(self):
        """404 when workspace does not exist."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(404, {"error": "Not found"})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-nope")
            assert response.status_code == 404
        finally:
            _cleanup_handler()


class TestUpdateWorkspace:
    """Test PATCH /workspaces/{workspace_id} endpoint."""

    def test_updates_workspace(self):
        """Update workspace forwards body and returns handler response."""
        body = {"name": "Updated Name"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"updated": True})
        )
        try:
            client = TestClient(app)
            response = client.patch("/workspaces/ws-1", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "PATCH"
            assert event["requestContext"]["http"]["path"] == "/workspaces/ws-1"
            parsed_body = json.loads(event["body"])
            assert parsed_body["name"] == "Updated Name"
        finally:
            _cleanup_handler()


class TestDeleteWorkspace:
    """Test DELETE /workspaces/{workspace_id} endpoint."""

    def test_deletes_workspace(self):
        """Delete workspace returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"deleted": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1")
            assert response.status_code == 200
            assert response.json()["deleted"] is True
        finally:
            _cleanup_handler()

    def test_delete_event_method(self):
        """Delete event uses DELETE method."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
        finally:
            _cleanup_handler()


# ============================================================================
# Member Routes
# ============================================================================


class TestAddMember:
    """Test POST /workspaces/{workspace_id}/members endpoint."""

    def test_adds_member(self):
        """Add member forwards body and returns handler response."""
        body = {"email": "user@example.com", "role": "editor"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"added": True})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces/ws-1/members", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-1/members"
            )
            parsed_body = json.loads(event["body"])
            assert parsed_body["email"] == "user@example.com"
        finally:
            _cleanup_handler()


class TestListMembers:
    """Test GET /workspaces/{workspace_id}/members endpoint."""

    def test_returns_members(self):
        """List returns members from handler."""
        members = [{"email": "user@example.com", "role": "admin"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"members": members})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/members")
            assert response.status_code == 200
            assert response.json()["members"] == members
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path includes workspace ID."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"members": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-xyz/members")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-xyz/members"
            )
        finally:
            _cleanup_handler()


class TestUpdateMember:
    """Test PATCH /workspaces/{workspace_id}/members/{member_email} endpoint."""

    def test_updates_member_role(self):
        """Update member role forwards body and returns handler response."""
        body = {"role": "viewer"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"updated": True})
        )
        try:
            client = TestClient(app)
            response = client.patch(
                "/workspaces/ws-1/members/user@example.com", json=body
            )
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "PATCH"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-1/members/user@example.com"
            )
        finally:
            _cleanup_handler()


class TestRemoveMember:
    """Test DELETE /workspaces/{workspace_id}/members/{member_email} endpoint."""

    def test_removes_member(self):
        """Remove member returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"removed": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/members/user@example.com")
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-1/members/user@example.com"
            )
        finally:
            _cleanup_handler()


# ============================================================================
# Repo Routes
# ============================================================================


class TestListAvailableRepos:
    """Test GET /workspaces/{workspace_id}/repos/available endpoint."""

    def test_returns_available_repos(self):
        """Returns list of available repos from handler."""
        repos = [{"name": "org/repo-1", "private": False}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"repos": repos})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/repos/available")
            assert response.status_code == 200
            assert response.json()["repos"] == repos
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path is correct."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc/repos/available")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/repos/available"
            )
        finally:
            _cleanup_handler()


class TestListRepos:
    """Test GET /workspaces/{workspace_id}/repos endpoint."""

    def test_returns_repos(self):
        """Returns list of workspace repos."""
        repos = [{"name": "org/repo-1", "type": "application"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"repos": repos})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/repos")
            assert response.status_code == 200
            assert response.json()["repos"] == repos
        finally:
            _cleanup_handler()


class TestAddRepo:
    """Test POST /workspaces/{workspace_id}/repos endpoint."""

    def test_adds_repo(self):
        """Add repo forwards body and returns handler response."""
        body = {"name": "org/repo-1", "type": "application"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"added": True})
        )
        try:
            client = TestClient(app)
            response = client.post("/workspaces/ws-1/repos", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            parsed_body = json.loads(event["body"])
            assert parsed_body["name"] == "org/repo-1"
        finally:
            _cleanup_handler()


class TestRemoveRepo:
    """Test DELETE /workspaces/{workspace_id}/repos/{repo_name} endpoint."""

    def test_removes_repo(self):
        """Remove repo returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"removed": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/repos/org/my-repo")
            assert response.status_code == 200
        finally:
            _cleanup_handler()

    def test_repo_name_with_slash(self):
        """Repo name containing slashes is preserved in path."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1/repos/my-org/my-repo")

            event = mock_handler.call_args[0][0]
            assert "my-org/my-repo" in event["requestContext"]["http"]["path"]
        finally:
            _cleanup_handler()

    def test_delete_event_method(self):
        """Delete event uses DELETE method."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1/repos/org/repo")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
        finally:
            _cleanup_handler()


# ============================================================================
# Sharing Routes
# ============================================================================


class TestGetSharing:
    """Test GET /workspaces/{workspace_id}/sharing endpoint."""

    def test_returns_sharing_config(self):
        """Returns sharing configuration from handler."""
        sharing = {"shared_with": ["ws-2"], "mode": "read"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, sharing)
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/sharing")
            assert response.status_code == 200
            assert response.json()["shared_with"] == ["ws-2"]
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path is correct."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc/sharing")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/sharing"
            )
        finally:
            _cleanup_handler()


class TestUpdateSharing:
    """Test PUT /workspaces/{workspace_id}/sharing endpoint."""

    def test_updates_sharing(self):
        """Update sharing forwards body and returns handler response."""
        body = {"shared_with": ["ws-2", "ws-3"]}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"updated": True})
        )
        try:
            client = TestClient(app)
            response = client.put("/workspaces/ws-1/sharing", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "PUT"
            parsed_body = json.loads(event["body"])
            assert parsed_body["shared_with"] == ["ws-2", "ws-3"]
        finally:
            _cleanup_handler()


class TestListSharingAvailable:
    """Test GET /workspaces/{workspace_id}/sharing/available endpoint."""

    def test_returns_available_workspaces(self):
        """Returns available workspaces for sharing."""
        available = [{"id": "ws-other", "name": "Other WS"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": available})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/sharing/available")
            assert response.status_code == 200
            assert response.json()["workspaces"] == available
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path is correct."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-abc/sharing/available")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-abc/sharing/available"
            )
        finally:
            _cleanup_handler()


# ============================================================================
# Org Management Routes
# ============================================================================


class TestListOrgMembers:
    """Test GET /org/members endpoint."""

    def test_returns_org_members(self):
        """Returns list of org members."""
        members = [{"email": "admin@example.com", "role": "org_admin"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"members": members})
        )
        try:
            client = TestClient(app)
            response = client.get("/org/members")
            assert response.status_code == 200
            assert response.json()["members"] == members
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path is /org/members."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/org/members")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["path"] == "/org/members"
            assert event["requestContext"]["http"]["method"] == "GET"
        finally:
            _cleanup_handler()


class TestAddOrgMember:
    """Test POST /org/members endpoint."""

    def test_adds_org_member(self):
        """Add org member forwards body and returns handler response."""
        body = {"email": "new@example.com", "role": "member"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"added": True})
        )
        try:
            client = TestClient(app)
            response = client.post("/org/members", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            parsed_body = json.loads(event["body"])
            assert parsed_body["email"] == "new@example.com"
        finally:
            _cleanup_handler()


class TestUpdateOrgMember:
    """Test PATCH /org/members/{member_email} endpoint."""

    def test_updates_org_member(self):
        """Update org member role forwards body and returns handler response."""
        body = {"role": "org_admin"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"updated": True})
        )
        try:
            client = TestClient(app)
            response = client.patch("/org/members/user@example.com", json=body)
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "PATCH"
            assert (
                event["requestContext"]["http"]["path"]
                == "/org/members/user@example.com"
            )
        finally:
            _cleanup_handler()


class TestRemoveOrgMember:
    """Test DELETE /org/members/{member_email} endpoint."""

    def test_removes_org_member(self):
        """Remove org member returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"removed": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/org/members/user@example.com")
            assert response.status_code == 200

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
            assert (
                event["requestContext"]["http"]["path"]
                == "/org/members/user@example.com"
            )
        finally:
            _cleanup_handler()

    def test_forbidden_error(self):
        """Non-admin gets 403."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(403, {"error": "Not authorized"})
        )
        try:
            client = TestClient(app)
            response = client.delete("/org/members/user@example.com")
            assert response.status_code == 403
        finally:
            _cleanup_handler()


# ============================================================================
# Chat Routes
# ============================================================================


class TestSearchableWorkspaces:
    """Test GET /chat/searchable-workspaces endpoint."""

    def test_returns_searchable_workspaces(self):
        """Returns list of searchable workspaces."""
        workspaces = [{"id": "ws-1", "name": "WS 1"}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"workspaces": workspaces})
        )
        try:
            client = TestClient(app)
            response = client.get("/chat/searchable-workspaces")
            assert response.status_code == 200
            assert response.json()["workspaces"] == workspaces
        finally:
            _cleanup_handler()

    def test_event_path(self):
        """Event path is /chat/searchable-workspaces."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.get("/chat/searchable-workspaces")

            event = mock_handler.call_args[0][0]
            assert (
                event["requestContext"]["http"]["path"]
                == "/chat/searchable-workspaces"
            )
        finally:
            _cleanup_handler()


# ============================================================================
# Document Routes
# ============================================================================


class TestGetUploadUrl:
    """Test POST /workspaces/{workspace_id}/documents/upload endpoint."""

    def test_returns_upload_url(self):
        """Returns presigned URL for document upload."""
        body = {"filename": "doc.pdf", "content_type": "application/pdf"}
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"upload_url": "https://s3.example.com/upload"})
        )
        try:
            client = TestClient(app)
            response = client.post(
                "/workspaces/ws-1/documents/upload", json=body
            )
            assert response.status_code == 200
            assert response.json()["upload_url"] == "https://s3.example.com/upload"

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert (
                event["requestContext"]["http"]["path"]
                == "/workspaces/ws-1/documents/upload"
            )
            parsed_body = json.loads(event["body"])
            assert parsed_body["filename"] == "doc.pdf"
        finally:
            _cleanup_handler()


class TestListDocuments:
    """Test GET /workspaces/{workspace_id}/documents endpoint."""

    def test_returns_documents(self):
        """Returns list of documents from handler."""
        docs = [{"name": "doc.pdf", "size": 1024}]
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"documents": docs})
        )
        try:
            client = TestClient(app)
            response = client.get("/workspaces/ws-1/documents")
            assert response.status_code == 200
            assert response.json()["documents"] == docs
        finally:
            _cleanup_handler()

    def test_query_params_forwarded(self):
        """Query parameters are added to the event."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"documents": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-1/documents?prefix=adrs/&limit=10")

            event = mock_handler.call_args[0][0]
            assert event["queryStringParameters"]["prefix"] == "adrs/"
            assert event["queryStringParameters"]["limit"] == "10"
        finally:
            _cleanup_handler()

    def test_path_includes_query_string(self):
        """Path in event includes query string when params present."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"documents": []})
        )
        try:
            client = TestClient(app)
            client.get("/workspaces/ws-1/documents?prefix=foo")

            event = mock_handler.call_args[0][0]
            path = event["requestContext"]["http"]["path"]
            assert "prefix=foo" in path
        finally:
            _cleanup_handler()


class TestDeleteDocument:
    """Test DELETE /workspaces/{workspace_id}/documents/{document_path} endpoint."""

    def test_deletes_document(self):
        """Delete document returns handler response."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(200, {"deleted": True})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/documents/adrs/adr-001.md")
            assert response.status_code == 200
        finally:
            _cleanup_handler()

    def test_document_path_with_slashes(self):
        """Document path containing slashes is preserved."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1/documents/folder/sub/doc.pdf")

            event = mock_handler.call_args[0][0]
            assert (
                "folder/sub/doc.pdf"
                in event["requestContext"]["http"]["path"]
            )
        finally:
            _cleanup_handler()

    def test_delete_event_method(self):
        """Delete event uses DELETE method."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            client.delete("/workspaces/ws-1/documents/doc.txt")

            event = mock_handler.call_args[0][0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
        finally:
            _cleanup_handler()

    def test_not_found(self):
        """Delete of nonexistent document returns 404."""
        mock_handler = _setup_mock_handler(
            _mock_lambda_response(404, {"error": "Document not found"})
        )
        try:
            client = TestClient(app)
            response = client.delete("/workspaces/ws-1/documents/nope.txt")
            assert response.status_code == 404
        finally:
            _cleanup_handler()


# ============================================================================
# Cross-cutting: handler always receives None context
# ============================================================================


class TestHandlerCalledWithNoneContext:
    """Verify all major routes pass None as the Lambda context."""

    def _assert_none_context(self, method, path, json_body=None):
        """Helper: call a route and assert the handler received None context."""
        mock_handler = _setup_mock_handler(_mock_lambda_response(200, {}))
        try:
            client = TestClient(app)
            if method == "GET":
                client.get(path)
            elif method == "POST":
                client.post(path, json=json_body or {})
            elif method == "PATCH":
                client.patch(path, json=json_body or {})
            elif method == "PUT":
                client.put(path, json=json_body or {})
            elif method == "DELETE":
                client.delete(path)

            context_arg = mock_handler.call_args[0][1]
            assert context_arg is None, f"Context should be None for {method} {path}"
        finally:
            _cleanup_handler()

    def test_create_workspace(self):
        self._assert_none_context("POST", "/workspaces", {"name": "test"})

    def test_list_workspaces(self):
        self._assert_none_context("GET", "/workspaces")

    def test_get_workspace(self):
        self._assert_none_context("GET", "/workspaces/ws-1")

    def test_delete_workspace(self):
        self._assert_none_context("DELETE", "/workspaces/ws-1")

    def test_list_members(self):
        self._assert_none_context("GET", "/workspaces/ws-1/members")

    def test_add_member(self):
        self._assert_none_context(
            "POST", "/workspaces/ws-1/members", {"email": "u@e.com"}
        )

    def test_list_org_members(self):
        self._assert_none_context("GET", "/org/members")

    def test_searchable_workspaces(self):
        self._assert_none_context("GET", "/chat/searchable-workspaces")

    def test_list_documents(self):
        self._assert_none_context("GET", "/workspaces/ws-1/documents")
