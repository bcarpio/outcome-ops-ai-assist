# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sharepoint-integration/main.py FastAPI shim."""

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
        _mod_name = "sharepoint_int_main"
        if _mod_name in sys.modules:
            del sys.modules[_mod_name]
        _saved_handler = sys.modules.pop("handler", None)
        _main_path = os.path.join(
            os.path.dirname(__file__), "../../sharepoint-integration/main.py"
        )
        _spec = importlib.util.spec_from_file_location(_mod_name, _main_path)
        main_module = importlib.util.module_from_spec(_spec)
        sys.modules[_mod_name] = main_module
        _spec.loader.exec_module(main_module)
        app = main_module.app
        if _saved_handler is not None:
            sys.modules["handler"] = _saved_handler
    except Exception as exc:
        _SKIP_REASON = f"Could not load sharepoint-integration/main.py: {exc}"

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
        assert client.get("/health").status_code == 200


class TestOAuth:
    def test_authorize(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"url": "u"}))
            resp = client.get("/sharepoint/authorize?workspace_id=ws1")
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_callback_302_redirect(self, client):
        try:
            _setup_mock_handler(_lambda_response(302, {}, {"Location": "/ws1"}))
            resp = client.get("/sharepoint/callback?code=c", follow_redirects=False)
            assert resp.status_code == 302
        finally:
            _cleanup_handler()

    def test_callback_post_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"connected": True}))
            resp = client.post("/sharepoint/callback", json={"code": "c"})
            assert resp.status_code == 200
            assert json.loads(mock_h.call_args.args[0]["body"]) == {"code": "c"}
        finally:
            _cleanup_handler()


class TestConnectionsAndSites:
    def test_list_connections(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"connections": []}))
            assert client.get("/workspaces/ws1/sharepoint/connections").status_code == 200
        finally:
            _cleanup_handler()

    def test_list_sites(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"sites": []}))
            resp = client.get(
                "/workspaces/ws1/sharepoint/connections/conn1/sites"
            )
            assert resp.status_code == 200
            assert "conn1" in mock_h.call_args.args[0]["requestContext"]["http"]["path"]
        finally:
            _cleanup_handler()

    def test_update_sites_forwards_body(self, client):
        try:
            mock_h = _setup_mock_handler(_lambda_response(200, {"updated": True}))
            resp = client.put(
                "/workspaces/ws1/sharepoint/connections/conn1/sites",
                json={"site_ids": ["site-a"]},
            )
            assert resp.status_code == 200
            body = json.loads(mock_h.call_args.args[0]["body"])
            assert body["site_ids"] == ["site-a"]
        finally:
            _cleanup_handler()

    def test_trigger_sync(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"queued": 1}))
            resp = client.post(
                "/workspaces/ws1/sharepoint/connections/conn1/sync"
            )
            assert resp.status_code == 200
        finally:
            _cleanup_handler()

    def test_delete_connection(self, client):
        try:
            _setup_mock_handler(_lambda_response(200, {"deleted": True}))
            resp = client.delete("/workspaces/ws1/sharepoint/connections/conn1")
            assert resp.status_code == 200
        finally:
            _cleanup_handler()


class TestErrorPropagation:
    def test_500_forwarded(self, client):
        try:
            _setup_mock_handler(_lambda_response(500, {"error": "boom"}))
            resp = client.get("/workspaces/ws1/sharepoint/connections")
            assert resp.status_code == 500
        finally:
            _cleanup_handler()
