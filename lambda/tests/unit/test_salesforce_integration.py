# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for salesforce-integration Lambda handler."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    if "salesforce_integration_handler" in sys.modules:
        del sys.modules["salesforce_integration_handler"]
    sn_dir = os.path.join(os.path.dirname(__file__), "../../salesforce-integration")
    if sn_dir not in sys.path:
        sys.path.insert(0, sn_dir)
    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    handler_path = os.path.join(sn_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "salesforce_integration_handler", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["salesforce_integration_handler"] = module
    spec.loader.exec_module(module)
    return module


os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("AUDIT_TABLE", "test-audit")
os.environ.setdefault("SALESFORCE_SYNC_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/sf-sync")
os.environ.setdefault("DELETE_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/delete")
os.environ.setdefault("SALESFORCE_CALLBACK_URL", "https://ui.example.com/api/salesforce/callback")
os.environ.setdefault("SALESFORCE_CLIENT_ID", "test-client-id")

handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


@pytest.fixture(autouse=True)
def reset_globals():
    handler_module._dynamodb = None
    handler_module._sqs = None
    yield


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
    return {
        "httpMethod": method,
        "path": path,
        "headers": {"x-user-email": user_email} if user_email else {},
        "queryStringParameters": query or {},
        "body": json.dumps(body) if body is not None else None,
        "requestContext": {"http": {"method": method, "path": path}},
    }


class TestConnect:
    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_admin_can_create_prod_connection(self, mock_put, mock_table):
        _admin(mock_table)
        mock_table.query.return_value = {"Items": []}
        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/salesforce/connect", body={"is_sandbox": False}),
            None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["next"] == "authorize"
        put_item = mock_put.call_args.args[1]
        assert put_item["is_sandbox"] is False
        assert put_item["last_sync_status"] == "needs_oauth"

    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_sandbox_flag_persisted(self, mock_put, mock_table):
        _admin(mock_table)
        mock_table.query.return_value = {"Items": []}
        lambda_handler(
            _event("POST", "/workspaces/ws1/salesforce/connect", body={"is_sandbox": True}),
            None,
        )
        assert mock_put.call_args.args[1]["is_sandbox"] is True

    @patch.object(handler_module.dual_write, "ddb_put_item")
    def test_reuses_existing_pending_of_same_sandbox(self, mock_put, mock_table):
        _admin(mock_table)
        mock_table.query.return_value = {
            "Items": [
                {"connection_id": "existing", "last_sync_status": "needs_oauth", "is_sandbox": True},
            ]
        }
        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/salesforce/connect", body={"is_sandbox": True}),
            None,
        )
        assert json.loads(resp["body"])["connection_id"] == "existing"

    def test_non_admin_403(self, mock_table):
        _non_admin(mock_table)
        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/salesforce/connect", body={"is_sandbox": False}),
            None,
        )
        assert resp["statusCode"] == 403


class TestAuthorize:
    @patch.object(
        handler_module.salesforce_oauth, "build_authorize_url",
        return_value="https://login.salesforce.com/services/oauth2/authorize?state=XYZ",
    )
    def test_returns_authorize_url_for_pending(self, mock_build, mock_table):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            if Key["SK"].startswith("SALESFORCE_CONNECTION#"):
                return {"Item": {"is_sandbox": False}}
            return {}
        mock_table.get_item.side_effect = side_effect

        with patch.object(handler_module, "_put_oauth_state"):
            resp = lambda_handler(
                _event(
                    "GET",
                    "/workspaces/ws1/salesforce/authorize",
                    query={"connection_id": "c1"},
                ),
                None,
            )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert body["authorize_url"].startswith(
            "https://login.salesforce.com/services/oauth2/authorize"
        )

    def test_missing_connection_id_400(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/salesforce/authorize"),
            None,
        )
        assert resp["statusCode"] == 400

    def test_missing_callback_env_500(self, mock_table):
        _admin(mock_table)
        saved = handler_module.SALESFORCE_CALLBACK_URL
        handler_module.SALESFORCE_CALLBACK_URL = ""
        try:
            resp = lambda_handler(
                _event(
                    "GET",
                    "/workspaces/ws1/salesforce/authorize",
                    query={"connection_id": "c1"},
                ),
                None,
            )
            assert resp["statusCode"] == 500
        finally:
            handler_module.SALESFORCE_CALLBACK_URL = saved


class TestCallback:
    def test_missing_code_400(self):
        resp = lambda_handler(
            _event("GET", "/salesforce/callback", user_email=None),
            None,
        )
        assert resp["statusCode"] == 400

    def test_denied_by_user_redirects(self):
        resp = lambda_handler(
            _event(
                "GET",
                "/salesforce/callback",
                user_email=None,
                query={"error": "access_denied"},
            ),
            None,
        )
        assert resp["statusCode"] == 302
        assert "salesforce_error=denied" in resp["headers"]["Location"]

    def test_bad_state_400(self, mock_table):
        with patch.object(handler_module, "_consume_oauth_state", return_value=None):
            resp = lambda_handler(
                _event(
                    "GET",
                    "/salesforce/callback",
                    user_email=None,
                    query={"code": "c", "state": "s"},
                ),
                None,
            )
        assert resp["statusCode"] == 400

    def test_accepts_post_body_code_and_state(self, mock_table):
        """Express proxy POSTs code+state in body (SigV4 quirk)."""
        with patch.object(handler_module, "_consume_oauth_state", return_value=None):
            resp = lambda_handler(
                _event(
                    "POST",
                    "/salesforce/callback",
                    user_email=None,
                    body={"code": "c", "state": "s"},
                ),
                None,
            )
        assert resp["statusCode"] == 400
        assert "invalid or expired state" in json.loads(resp["body"])["error"]

    @patch.object(
        handler_module.salesforce_oauth, "exchange_code_for_tokens",
        return_value={
            "access_token": "a",
            "refresh_token": "r",
            "instance_url": "https://acme.my.salesforce.com",
            "id": "https://login.salesforce.com/id/00D000000/005000000",
        },
    )
    @patch.object(handler_module.salesforce_oauth, "save_refresh_token")
    @patch.object(handler_module.dual_write, "ddb_update_item")
    @patch.object(handler_module, "log_admin_action")
    def test_happy_path_redirects_and_queues(
        self, mock_audit, mock_upd, mock_save, mock_exchange,
        mock_table, mock_sqs,
    ):
        with patch.object(
            handler_module,
            "_consume_oauth_state",
            return_value={
                "workspace_id": "ws1",
                "connection_id": "c1",
                "user_email": "admin@example.com",
                "is_sandbox": False,
            },
        ):
            resp = lambda_handler(
                _event(
                    "GET",
                    "/salesforce/callback",
                    user_email=None,
                    query={"code": "c", "state": "s"},
                ),
                None,
            )
        assert resp["statusCode"] == 302
        assert "salesforce=connected" in resp["headers"]["Location"]
        mock_save.assert_called_once_with("c1", "r")
        mock_upd.assert_called_once()
        vals = mock_upd.call_args.kwargs["ExpressionAttributeValues"]
        assert vals[":s"] == "queued"
        assert vals[":url"] == "https://acme.my.salesforce.com"
        assert vals[":oid"] == "00D000000"
        assert vals[":uid"] == "005000000"
        mock_sqs.send_message.assert_called_once()


class TestConnections:
    def test_list_paginates_and_scrubs(self, mock_table):
        _admin(mock_table)
        mock_table.query.side_effect = [
            {
                "Items": [
                    {
                        "PK": "WS#ws1",
                        "SK": "SALESFORCE_CONNECTION#c1",
                        "GSI2PK": "SALESFORCE#connections",
                        "connection_id": "c1",
                        "entity_type": "salesforce_connection",
                        "instance_url": "https://acme.my.salesforce.com",
                    }
                ],
                "LastEvaluatedKey": {"PK": "WS#ws1"},
            },
            {
                "Items": [
                    {"PK": "WS#ws1", "SK": "SALESFORCE_CONNECTION#c2", "connection_id": "c2"}
                ],
            },
        ]
        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/salesforce/connections"), None,
        )
        assert resp["statusCode"] == 200
        body = json.loads(resp["body"])
        assert [c["connection_id"] for c in body["connections"]] == ["c1", "c2"]
        assert "PK" not in body["connections"][0]

    def test_trigger_sync_admin_passes_cursors(self, mock_table, mock_sqs):
        def side_effect(Key, **_):
            if Key["SK"].startswith("WS_MEMBER#"):
                return {"Item": {"role": "workspace_admin"}}
            return {
                "Item": {
                    "config": {"cursors": {"Account": "2026-06-01T00:00:00Z"}},
                }
            }
        mock_table.get_item.side_effect = side_effect
        resp = lambda_handler(
            _event("POST", "/workspaces/ws1/salesforce/connections/c1/sync"),
            None,
        )
        assert resp["statusCode"] == 202
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["cursors"] == {"Account": "2026-06-01T00:00:00Z"}

    @patch.object(handler_module.dual_write, "ddb_delete_item")
    @patch.object(handler_module.salesforce_oauth, "delete_tokens")
    def test_delete_cascades(
        self, mock_del_tokens, mock_del_conn, mock_table, mock_sqs,
    ):
        _admin(mock_table)
        resp = lambda_handler(
            _event("DELETE", "/workspaces/ws1/salesforce/connections/c1"),
            None,
        )
        assert resp["statusCode"] == 200
        mock_del_conn.assert_called_once()
        mock_del_tokens.assert_called_once_with("c1")
        # delete-worker dispatches on `action` -- without it the message
        # is skipped as "Unknown action: None" and orphans persist forever.
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["action"] == "delete_prefix"
        assert body["source"] == "salesforce"
        assert body["prefix"] == "c1/"


class TestAuthGating:
    def test_missing_email_401(self):
        resp = lambda_handler(
            _event("GET", "/workspaces/ws1/salesforce/connections", user_email=None),
            None,
        )
        assert resp["statusCode"] == 401

    def test_unknown_route_404(self, mock_table):
        _admin(mock_table)
        resp = lambda_handler(_event("GET", "/salesforce/nope"), None)
        assert resp["statusCode"] == 404
