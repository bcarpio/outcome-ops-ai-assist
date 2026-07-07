"""Unit tests for servicenow-integration Lambda handler (env-SSM OAuth)."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


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


os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("AUDIT_TABLE", "test-audit")
os.environ.setdefault("SERVICENOW_SYNC_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/sn-sync")
os.environ.setdefault("DELETE_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/delete")
os.environ.setdefault("SERVICENOW_CALLBACK_URL", "https://ui.example.com/api/servicenow/callback")
os.environ.setdefault("SERVICENOW_INSTANCE_URL", "https://dev307154.service-now.com")
os.environ.setdefault("SERVICENOW_CLIENT_ID", "test-client-id")

handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


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
# handle_authorize
# ============================================================================


class TestAuthorize:
    @patch.object(
        handler_module.servicenow_oauth,
        "build_authorize_url",
        return_value="https://dev307154.service-now.com/oauth_auth.do?state=XYZ",
    )
    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_admin_click_returns_authorize_url_and_creates_pending_row(
        self, mock_put, mock_build, mock_table
    ):
        _admin(mock_table)
        mock_table.query.return_value = {"Items": []}
        with patch.object(handler_module, "_put_oauth_state"):
            resp = lambda_handler(
                _event("GET", "/workspaces/ws1/servicenow/authorize"),
                None,
            )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["authorize_url"].startswith(
            "https://dev307154.service-now.com/oauth_auth.do"
        )
        assert body["connection_id"]

        # A pending connection row was persisted with no client_id / instance_url
        # (those are env-scoped now).
        mock_put.assert_called_once()
        put_item = mock_put.call_args.args[1]
        assert put_item["last_sync_status"] == "needs_oauth"
        assert "instance_url" not in put_item
        assert "client_id" not in put_item

    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_reuses_existing_needs_oauth_connection(self, mock_put, mock_table):
        _admin(mock_table)
        mock_table.query.return_value = {
            "Items": [
                {
                    "connection_id": "existing-uuid",
                    "last_sync_status": "needs_oauth",
                }
            ]
        }
        with patch.object(handler_module, "_put_oauth_state"), patch.object(
            handler_module.servicenow_oauth, "build_authorize_url", return_value="https://x"
        ):
            resp = lambda_handler(
                _event("GET", "/workspaces/ws1/servicenow/authorize"),
                None,
            )
        assert resp["statusCode"] == 200
        assert json.loads(resp["body"])["connection_id"] == "existing-uuid"
        put_item = mock_put.call_args.args[1]
        assert put_item["connection_id"] == "existing-uuid"

    def test_non_admin_gets_403(self, mock_table):
        _non_admin(mock_table)
        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/servicenow/authorize"),
            None,
        )
        assert resp["statusCode"] == 403

    def test_missing_callback_env_500(self, mock_table):
        _admin(mock_table)
        saved = handler_module.SERVICENOW_CALLBACK_URL
        handler_module.SERVICENOW_CALLBACK_URL = ""
        try:
            resp = lambda_handler(
                _event("GET", "/workspaces/ws1/servicenow/authorize"),
                None,
            )
            assert resp["statusCode"] == 500
        finally:
            handler_module.SERVICENOW_CALLBACK_URL = saved

    def test_missing_instance_url_500(self, mock_table):
        _admin(mock_table)
        with patch.object(
            handler_module.servicenow_oauth,
            "get_instance_url",
            side_effect=ValueError("SERVICENOW_INSTANCE_URL env var not configured"),
        ):
            resp = lambda_handler(
                _event("GET", "/workspaces/ws1/servicenow/authorize"),
                None,
            )
        assert resp["statusCode"] == 500
        assert "not fully configured" in json.loads(resp["body"])["error"]


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

    def test_accepts_post_body_code_and_state(self, mock_table):
        # Express proxy POSTs code+state in body to avoid SigV4
        # signature-vs-querystring quirks. Handler must read from body
        # when querystring is empty.
        with patch.object(handler_module, "_consume_oauth_state", return_value=None):
            resp = lambda_handler(
                _event(
                    "POST",
                    "/servicenow/callback",
                    user_email=None,
                    body={"code": "c", "state": "s"},
                ),
                None,
            )
        # State lookup returned None -> 400 "invalid or expired state",
        # but crucially NOT the earlier "code and state are required"
        # 400 -- that would mean the body-parsing regressed again.
        assert resp["statusCode"] == 400
        assert "invalid or expired state" in json.loads(resp["body"])["error"]

    @patch.object(
        handler_module.servicenow_oauth,
        "exchange_code_for_tokens",
        return_value=("access", "refresh", 1800),
    )
    @patch.object(handler_module.servicenow_oauth, "save_refresh_token")
    @patch.object(
        handler_module.servicenow_oauth,
        "get_instance_url",
        return_value="https://dev307154.service-now.com",
    )
    @patch.object(handler_module.dual_write, "ddb_update_item")
    @patch.object(handler_module, "log_admin_action")
    @patch.object(
        handler_module,
        "get_current_user",
        return_value={"sys_id": "u1", "user_name": "svc"},
    )
    def test_happy_path_redirects_and_queues(
        self,
        mock_user,
        mock_audit,
        mock_upd,
        mock_get_url,
        mock_save,
        mock_exchange,
        mock_table,
        mock_sqs,
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
        # Exchange called with no workspace_id (env-scoped credentials now)
        mock_exchange.assert_called_once()
        assert "workspace_id" not in mock_exchange.call_args.kwargs
        assert mock_exchange.call_args.kwargs["connection_id"] == "conn-1"


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
        assert "PK" not in body["connections"][0]
        assert "entity_type" not in body["connections"][0]

    def test_trigger_sync_admin(self, mock_table, mock_sqs):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {"Item": {"config": {"cursor": "2026-05-01 00:00:00"}}}
        mock_table.get_item.side_effect = side_effect

        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/servicenow/connections/conn-1/sync"),
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
        mock_sqs.send_message.assert_called_once()


class TestScrubFallback:
    """Regression: 2026-07-07 legacy DDB rows written before the env-SSM
    refactor may lack the connection_id attribute; the UI's Disconnect
    button then fires against `.../connections/undefined`. _scrub must
    fall back to extracting connection_id from SK."""

    def test_legacy_row_missing_connection_id_falls_back_to_sk(self):
        item = {
            "PK": "WS#ws1",
            "SK": "SERVICENOW_CONNECTION#75b478b7-a3af",
            "last_sync_status": "auth_error",
        }
        view = handler_module._scrub(item)
        assert view["connection_id"] == "75b478b7-a3af"
        assert "PK" not in view
        assert "SK" not in view

    def test_new_row_preserves_explicit_connection_id(self):
        item = {
            "PK": "WS#ws1",
            "SK": "SERVICENOW_CONNECTION#new-id",
            "connection_id": "new-id",
            "last_sync_status": "success",
        }
        view = handler_module._scrub(item)
        assert view["connection_id"] == "new-id"


class TestAuthGating:
    def test_missing_email_401(self):
        ev = _event("GET", "/workspaces/ws1/servicenow/connections", user_email=None)
        resp = lambda_handler(ev, None)
        assert resp["statusCode"] == 401

    def test_unknown_route_404(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(_event("GET", "/servicenow/nope"), None)
        assert resp["statusCode"] == 404

    def test_setup_route_gone_404(self, mock_table):
        """POST /setup used to exist under the per-workspace OAuth model.
        It's now 404 -- the env-SSM model has no setup step."""
        _admin(mock_table)
        resp = lambda_handler(
            _event(
                "POST",
                "/workspaces/ws1/servicenow/setup",
                body={
                    "instance_url": "https://acme.service-now.com",
                    "client_id": "x",
                    "client_secret": "y",
                },
            ),
            None,
        )
        assert resp["statusCode"] == 404
