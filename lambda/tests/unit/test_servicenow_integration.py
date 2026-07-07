"""Unit tests for servicenow-integration Lambda handler (chunk 02)."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ============================================================================
# Module loading
# ============================================================================


def _load_handler_module():
    if "servicenow_integration_handler" in sys.modules:
        del sys.modules["servicenow_integration_handler"]

    sn_dir = os.path.join(os.path.dirname(__file__), "../../servicenow-integration")
    if sn_dir not in sys.path:
        sys.path.insert(0, sn_dir)

    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)

    handler_path = os.path.join(sn_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("servicenow_integration_handler", handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["servicenow_integration_handler"] = module
    spec.loader.exec_module(module)
    return module


# Set env before import so module-init caches match test expectations.
os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("AUDIT_TABLE", "test-audit")
os.environ.setdefault("SERVICENOW_SYNC_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/sn-sync")
os.environ.setdefault("DELETE_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/delete")
os.environ.setdefault("SERVICENOW_CALLBACK_URL", "https://ui.example.com/api/servicenow/callback")

handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_globals():
    handler_module._dynamodb = None
    handler_module._sqs = None
    saved = handler_module.report_repos_snapshot
    handler_module.report_repos_snapshot = None
    yield
    handler_module.report_repos_snapshot = saved


@pytest.fixture
def mock_table():
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    handler_module._dynamodb = resource
    return table


@pytest.fixture
def mock_sqs():
    client = MagicMock()
    handler_module._sqs = client
    return client


def _admin(table):
    def get_item(Key, **_):
        if Key["SK"].startswith("WS_MEMBER#"):
            return {"Item": {"role": "workspace_admin"}}
        return {}
    table.get_item.side_effect = get_item


def _non_admin(table):
    table.get_item.return_value = {}


def _event(method, path, *, user_email="admin@example.com", query=None, body=None):
    ev = {
        "httpMethod": method,
        "path": path,
        "headers": {"x-user-email": user_email} if user_email else {},
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"http": {"method": method, "path": path}},
    }
    return ev


# ============================================================================
# handle_setup
# ============================================================================


class TestSetup:
    @patch.object(handler_module.servicenow_oauth, "save_client_secret")
    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_admin_can_setup_new_connection(
        self, mock_put, mock_save_secret, mock_table, mock_sqs
    ):
        _admin(mock_table)
        mock_table.query.return_value = {"Items": []}

        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/setup",
                body={
                    "instance_url": "https://acme.service-now.com",
                    "client_id": "abc123",
                    "client_secret": "secret-x",
                },
            ),
            None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["next"] == "authorize"
        assert "connection_id" in body

        # Persisted connection row + client_secret
        mock_put.assert_called_once()
        put_item = mock_put.call_args.args[1]
        assert put_item["instance_url"] == "https://acme.service-now.com"
        assert put_item["client_id"] == "abc123"
        assert put_item["last_sync_status"] == "needs_oauth"
        mock_save_secret.assert_called_once_with(body["connection_id"], "secret-x")

    def test_non_admin_gets_403(self, mock_table):
        _non_admin(mock_table)
        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/setup",
                body={
                    "instance_url": "https://acme.service-now.com",
                    "client_id": "abc",
                    "client_secret": "secret",
                },
            ),
            None,
        )
        assert resp["statusCode"] == 403

    def test_bad_instance_url_400(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/setup",
                body={
                    "instance_url": "http://not-https.example.com",
                    "client_id": "abc",
                    "client_secret": "secret",
                },
            ),
            None,
        )
        assert resp["statusCode"] == 400
        assert "instance_url" in json.loads(resp["body"])["error"]

    def test_legacy_servicenow_dot_com_accepted(self, mock_table, mock_sqs):
        _admin(mock_table)
        mock_table.query.return_value = {"Items": []}
        with patch.object(handler_module.dual_write, "ddb_put_item"), \
             patch.object(handler_module.servicenow_oauth, "save_client_secret"):
            resp = lambda_handler(
                _event(
                    "POST",
                    "/workspaces/ws1/servicenow/setup",
                    body={
                        "instance_url": "https://acme.servicenow.com",
                        "client_id": "abc",
                        "client_secret": "secret",
                    },
                ),
                None,
            )
        assert resp["statusCode"] == 200

    @patch.object(handler_module.servicenow_oauth, "save_client_secret")
    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_resetup_same_instance_reuses_connection_id(
        self, mock_put, mock_save_secret, mock_table, mock_sqs
    ):
        _admin(mock_table)
        mock_table.query.return_value = {
            "Items": [
                {
                    "instance_url": "https://acme.service-now.com",
                    "connection_id": "existing-uuid",
                }
            ]
        }

        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/setup",
                body={
                    "instance_url": "https://acme.service-now.com",
                    "client_id": "abc-new",
                    "client_secret": "secret-new",
                },
            ),
            None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["connection_id"] == "existing-uuid"


# ============================================================================
# handle_authorize
# ============================================================================


class TestAuthorize:
    @patch.object(handler_module.servicenow_oauth, "build_authorize_url", return_value="https://acme.service-now.com/oauth_auth.do?state=XYZ")
    def test_admin_gets_authorize_url(self, mock_build, mock_table):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("SERVICENOW_CONNECTION#"):
                return {
                    "Item": {
                        "instance_url": "https://acme.service-now.com",
                        "client_id": "abc",
                    }
                }
            return {}
        mock_table.get_item.side_effect = side_effect

        with patch.object(handler_module, "_put_oauth_state"):
            resp = lambda_handler(
                _event(
                    "GET",
                    "/workspaces/ws1/servicenow/authorize",
                    query={"connection_id": "conn-1"},
                ),
                None,
            )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["authorize_url"].startswith("https://acme.service-now.com/oauth_auth.do")

    def test_missing_connection_id_400(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/servicenow/authorize"),
            None,
        )
        assert resp["statusCode"] == 400

    def test_unknown_connection_404(self, mock_table):
        # Need get_item to return {} for BOTH the membership check AND the
        # connection lookup. The admin fixture short-circuits on WS_MEMBER#.
        table_calls = {"n": 0}
        def side_effect(Key, **_):
            table_calls["n"] += 1
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("SERVICENOW_CONNECTION#"):
                return {}
            return {}
        mock_table.get_item.side_effect = side_effect
        resp = lambda_handler(
            _event(
                "GET",
                "/workspaces/ws1/servicenow/authorize",
                query={"connection_id": "missing"},
            ),
            None,
        )
        assert resp["statusCode"] == 404

    def test_missing_callback_env_500(self, mock_table):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("SERVICENOW_CONNECTION#"):
                return {"Item": {"instance_url": "https://acme.service-now.com", "client_id": "abc"}}
            return {}
        mock_table.get_item.side_effect = side_effect
        saved = handler_module.SERVICENOW_CALLBACK_URL
        handler_module.SERVICENOW_CALLBACK_URL = ""
        try:
            resp = lambda_handler(
                _event(
                    "GET",
                    "/workspaces/ws1/servicenow/authorize",
                    query={"connection_id": "conn-1"},
                ),
                None,
            )
            assert resp["statusCode"] == 500
        finally:
            handler_module.SERVICENOW_CALLBACK_URL = saved


# ============================================================================
# handle_callback
# ============================================================================


class TestCallback:
    def test_missing_code_400(self):
        resp = lambda_handler(
            _event("GET", "/servicenow/callback", user_email=None),
            None,
        )
        assert resp["statusCode"] == 400

    def test_denied_by_user_redirects(self):
        resp = lambda_handler(
            _event(
                "GET",
                "/servicenow/callback",
                user_email=None,
                query={"error": "access_denied"},
            ),
            None,
        )
        assert resp["statusCode"] == 302
        assert "servicenow_error=denied" in resp["headers"]["Location"]

    def test_bad_state_400(self, mock_table):
        with patch.object(handler_module, "_consume_oauth_state", return_value=None):
            resp = lambda_handler(
                _event(
                    "GET",
                    "/servicenow/callback",
                    user_email=None,
                    query={"code": "c", "state": "s"},
                ),
                None,
            )
        assert resp["statusCode"] == 400

    @patch.object(handler_module.servicenow_oauth, "exchange_code_for_tokens", return_value=("access", "refresh", 1800))
    @patch.object(handler_module.servicenow_oauth, "save_refresh_token")
    @patch.object(handler_module.dual_write, "ddb_update_item")
    @patch.object(handler_module, "log_admin_action")
    @patch.object(handler_module, "get_current_user", return_value={"sys_id": "u1", "user_name": "svc"})
    def test_happy_path_redirects_and_queues(
        self, mock_user, mock_audit, mock_upd, mock_save, mock_exchange,
        mock_table, mock_sqs,
    ):
        with patch.object(
            handler_module,
            "_consume_oauth_state",
            return_value={
                "workspace_id": "ws1",
                "connection_id": "conn-1",
                "user_email": "admin@example.com",
            },
        ):
            mock_table.get_item.return_value = {"Item": {"instance_url": "https://acme.service-now.com"}}
            resp = lambda_handler(
                _event(
                    "GET",
                    "/servicenow/callback",
                    user_email=None,
                    query={"code": "c", "state": "s"},
                ),
                None,
            )
        assert resp["statusCode"] == 302
        assert "servicenow=connected" in resp["headers"]["Location"]
        mock_save.assert_called_once_with("conn-1", "refresh")
        mock_upd.assert_called_once()
        assert mock_upd.call_args.kwargs["ExpressionAttributeValues"][":s"] == "queued"
        mock_sqs.send_message.assert_called_once()


# ============================================================================
# list_connections + trigger_sync + delete
# ============================================================================


class TestConnections:
    def test_list_paginates_and_scrubs(self, mock_table):
        _admin(mock_table)
        mock_table.query.side_effect = [
            {
                "Items": [
                    {
                        "PK": "WS#ws1",
                        "SK": "SERVICENOW_CONNECTION#c1",
                        "GSI2PK": "SERVICENOW#connections",
                        "connection_id": "c1",
                        "instance_url": "https://acme.service-now.com",
                        "entity_type": "servicenow_connection",
                    }
                ],
                "LastEvaluatedKey": {"PK": "WS#ws1"},
            },
            {"Items": [{"PK": "WS#ws1", "SK": "SERVICENOW_CONNECTION#c2", "connection_id": "c2"}]},
        ]

        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/servicenow/connections"),
            None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        ids = [c["connection_id"] for c in body["connections"]]
        assert ids == ["c1", "c2"]
        # Internals scrubbed
        assert "PK" not in body["connections"][0]
        assert "entity_type" not in body["connections"][0]

    def test_trigger_sync_admin(self, mock_table, mock_sqs):
        # Membership check + connection get_item
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {"Item": {"config": {"cursor": "2026-05-01 00:00:00"}}}
        mock_table.get_item.side_effect = side_effect

        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/connections/conn-1/sync",
            ),
            None,
        )
        assert resp["statusCode"] == 202
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["cursor"] == "2026-05-01 00:00:00"

    def test_trigger_sync_missing_connection_404(self, mock_table, mock_sqs):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {}
        mock_table.get_item.side_effect = side_effect

        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/servicenow/connections/missing/sync"),
            None,
        )
        assert resp["statusCode"] == 404

    @patch.object(handler_module.dual_write, "ddb_delete_item")
    @patch.object(handler_module.servicenow_oauth, "delete_tokens")
    def test_delete_cascades(
        self, mock_del_tokens, mock_del_conn, mock_table, mock_sqs
    ):
        _admin(mock_table)
        resp = lambda_handler(
            _event("DELETE", "/workspaces/ws1/servicenow/connections/conn-1"),
            None,
        )
        assert resp["statusCode"] == 200
        mock_del_conn.assert_called_once()
        mock_del_tokens.assert_called_once_with("conn-1")
        mock_sqs.send_message.assert_called_once()  # DELETE_QUEUE fan-out


# ============================================================================
# Auth gating
# ============================================================================


class TestAuthGating:
    def test_missing_email_401(self):
        ev = _event("GET", "/workspaces/ws1/servicenow/connections", user_email=None)
        resp = lambda_handler(ev, None)
        assert resp["statusCode"] == 401

    def test_unknown_route_404(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(_event("GET", "/servicenow/nope"), None)
        assert resp["statusCode"] == 404
