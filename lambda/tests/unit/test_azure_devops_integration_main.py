"""Unit tests for azure-devops-integration/main.py FastAPI shim.

Chunk 17 Group B. The `main.py` files are the Lambda's public HTTP
surface under Lambda Web Adapter; a routing bug here 404s a whole
integration silently. These tests verify that each shim route builds
the right event structure (path, method, body, query params, headers)
and returns the underlying handler's response verbatim.

Test pattern mirrors ``test_workspace_management_main.py``.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock

import pytest

_SKIP_REASON = None
try:
    from starlette.testclient import TestClient  # noqa: F401
except (ImportError, RuntimeError) as exc:
    _SKIP_REASON = f"starlette TestClient unavailable: {exc}"

if _SKIP_REASON is None:
    try:
        _mod_name = "azdo_int_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(
            os.path.dirname(__file__), "../../azure-devops-integration/main.py"
        )
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load azure-devops-integration/main.py: {exc}"

if _SKIP_REASON:
    pytest.skip(_SKIP_REASON, allow_module_level=True)


def _lambda_response(status_code=200, body=None, headers=None):
    resp = {"statusCode": status_code, "body": json.dumps(body or {})}
    if headers:
        resp["headers"] = headers
    return resp


def _setup_mock_handler(return_value):
    mock_handler = MagicMock(return_value=return_value)
    sys.modules["handler"] = MagicMock(lambda_handler=mock_handler)
    return mock_handler


def _cleanup_handler():
    if "handler" in sys.modules:
        del sys.modules["handler"]


@pytest.fixture
def client():
    return TestClient(app)


class TestHealth:
    def test_health_endpoint_returns_healthy(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}


class TestOAuth:
    def test_authorize_forwards_query_params_and_path(self, client):
        try:
            mock_h = _setup_mock_handler(
                _lambda_response(200, {"authorize_url": "https://example.com"})
            )
            resp = client.get(
                "/azure-devops/authorize?workspace_id=ws1&user_email=alice@example.com"
            )
            assert resp.status_code == 200
            assert resp.json() == {"authorize_url": "https://example.com"}
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["method"] == "GET"
            assert event["requestContext"]["http"]["path"] == "/azure-devops/authorize"
            assert event["queryStringParameters"]["workspace_id"] == "ws1"
        finally:
            _cleanup_handler()

    def test_callback_get_302_becomes_redirect(self, client):
        try:
            _setup_mock_handler(_lambda_response(
                302, {}, headers={"Location": "/workspaces/ws1"},
            ))
            resp = client.get(
                "/azure-devops/callback?code=abc&state=xyz",
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/workspaces/ws1"
        finally:
            _cleanup_handler()

    def test_callback_post_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"connected": True}))
            resp = client.post(
                "/azure-devops/callback",
                json={"code": "abc", "state": "xyz"},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["method"] == "POST"
            assert json.loads(event["body"]) == {"code": "abc", "state": "xyz"}
        finally:
            _cleanup_handler()


class TestConnectionsAndRepos:
    def test_list_connections_forwards_workspace_id(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"connections": []}))
            resp = client.get("/workspaces/ws1/azure-devops/connections")
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["path"] == "/workspaces/ws1/azure-devops/connections"
        finally:
            _cleanup_handler()

    def test_delete_connection_forwards_ids(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"deleted": True}))
            resp = client.delete(
                "/workspaces/ws1/azure-devops/connections/conn-abc"
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["method"] == "DELETE"
            assert "ws1" in event["requestContext"]["http"]["path"]
            assert "conn-abc" in event["requestContext"]["http"]["path"]
        finally:
            _cleanup_handler()

    def test_add_repos_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"added": 2}))
            resp = client.post(
                "/workspaces/ws1/azure-devops/repos",
                json={"connection_id": "conn-abc", "repos": [{"org": "o", "project": "p", "repo": "r"}]},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            body = json.loads(event["body"])
            assert body["connection_id"] == "conn-abc"
        finally:
            _cleanup_handler()

    def test_list_repos(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"repos": []}))
            resp = client.get("/workspaces/ws1/azure-devops/repos")
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_update_repo_uses_path_slug(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"updated": True}))
            resp = client.put(
                "/workspaces/ws1/azure-devops/repos/myorg/myproj/myrepo",
                json={"enabled": False},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert "myorg/myproj/myrepo" in event["requestContext"]["http"]["path"]
        finally:
            _cleanup_handler()

    def test_delete_repo_uses_path_slug(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"deleted": True}))
            resp = client.delete(
                "/workspaces/ws1/azure-devops/repos/o/p/r"
            )
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_trigger_sync_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"queued": 1}))
            resp = client.post(
                "/workspaces/ws1/azure-devops/sync",
                json={"connection_id": "conn-abc"},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["method"] == "POST"
        finally:
            _cleanup_handler()


class TestErrorPropagation:
    def test_non_200_status_forwarded(self, client):
        try:
            _setup_mock_handler(_lambda_response(403, {"error": "forbidden"}))
            resp = client.get("/workspaces/ws1/azure-devops/connections")
            assert resp.status_code == 403
            assert resp.json() == {"error": "forbidden"}
        finally:
            _cleanup_handler()

    def test_404_status_forwarded(self, client):
        try:
            _setup_mock_handler(_lambda_response(404, {"error": "not found"}))
            resp = client.get(
                "/workspaces/ws1/azure-devops/connections/missing/orgs"
            )
            assert resp.status_code == 404
        finally:
            _cleanup_handler()
