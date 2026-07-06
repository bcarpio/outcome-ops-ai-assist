"""Unit tests for azure-devops-boards-integration/main.py FastAPI shim.

Chunk 17 Group B. Verifies that each route builds the right event
structure and returns the underlying handler's response verbatim.
See test_azure_devops_integration_main.py for the pattern.
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
        _mod_name = "azdo_boards_int_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(
            os.path.dirname(__file__),
            "../../azure-devops-boards-integration/main.py",
        )
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load azure-devops-boards-integration/main.py: {exc}"

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
    def test_health(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "healthy"}


class TestOAuth:
    def test_authorize(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"authorize_url": "u"}))
            resp = client.get("/azure-devops-boards/authorize?workspace_id=ws1")
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["path"] == "/azure-devops-boards/authorize"
        finally:
            _cleanup_handler()

    def test_callback_get_302_redirect(self, client):
        try:
            _setup_mock_handler(_lambda_response(
                302, {}, headers={"Location": "/workspaces/ws1"},
            ))
            resp = client.get(
                "/azure-devops-boards/callback?code=c&state=s",
                follow_redirects=False,
            )
            assert resp.status_code == 302
            assert resp.headers["location"] == "/workspaces/ws1"
        finally:
            _cleanup_handler()

    def test_callback_post_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"connected": True}))
            resp = client.post("/azure-devops-boards/callback", json={"code": "c"})
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert json.loads(event["body"]) == {"code": "c"}
        finally:
            _cleanup_handler()


class TestConnectionsAndProjects:
    def test_list_connections(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"connections": []}))
            resp = client.get("/workspaces/ws1/azure-devops-boards/connections")
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_add_projects_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"added": 1}))
            resp = client.post(
                "/workspaces/ws1/azure-devops-boards/projects",
                json={"connection_id": "c1", "projects": [{"org": "o", "project": "p"}]},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            body = json.loads(event["body"])
            assert body["connection_id"] == "c1"
        finally:
            _cleanup_handler()

    def test_list_projects(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"projects": []}))
            resp = client.get("/workspaces/ws1/azure-devops-boards/projects")
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_trigger_sync(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"queued": 1}))
            resp = client.post(
                "/workspaces/ws1/azure-devops-boards/sync",
                json={"connection_id": "c1"},
            )
            assert resp.status_code == 200
            event = mock_h.call_args.args[0]
            assert event["requestContext"]["http"]["method"] == "POST"
        finally:
            _cleanup_handler()


class TestErrorPropagation:
    def test_403_forwarded(self, client):
        try:
            _setup_mock_handler(_lambda_response(403, {"error": "forbidden"}))
            resp = client.get("/workspaces/ws1/azure-devops-boards/connections")
            assert resp.status_code == 403
        finally:
            _cleanup_handler()
