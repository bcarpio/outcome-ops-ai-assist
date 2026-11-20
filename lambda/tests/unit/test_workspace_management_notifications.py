# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the workspace-management notification routes.

GET /notifications and
DELETE /workspaces/{workspace_id}/notifications/{notification_id}.
Backed by moto DynamoDB tables (the parent conftest wraps every test in
mock_aws).
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError

# Loaded under its own module name so patch("workspace_management_handler.X")
# in test_workspace_management.py keeps resolving to that file's instance.
_MODULE_NAME = "workspace_management_handler_notifications"


def _load_handler_module():
    if _MODULE_NAME in sys.modules:
        del sys.modules[_MODULE_NAME]
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../workspace-management/handler.py"
    )
    spec = importlib.util.spec_from_file_location(_MODULE_NAME, handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


handler = _load_handler_module()

WS_TABLE = "test-workspaces"
NOTIF_TABLE = "test-notifications"

ADMIN_A = "admin-a@example.com"
MEMBER_B = "member-b@example.com"
OTHER_B = "other-b@example.com"
ORG_ADMIN = "org-admin@example.com"
OUTSIDER = "outsider@example.com"


def _create_table(ddb, name):
    ddb.create_table(
        TableName=name,
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    return ddb.Table(name)


def _notif(ws, nid, added_by, updated_at):
    return {
        "PK": f"WS#{ws}",
        "SK": f"NOTIF#{nid}",
        "notification_id": nid,
        "workspace_id": ws,
        "type": "repo_stalled",
        "severity": "warning",
        "title": f"title {nid}",
        "message": "stalled",
        "repo_name": "org/repo",
        "added_by": added_by,
        "reason": "code_map",
        "target_sha": "abc123",
        "failed_files": ["a.py"],
        "failed_files_total": 1,
        "created_at": updated_at,
        "updated_at": updated_at,
        "ttl": 1999999999,
    }


@pytest.fixture
def env(monkeypatch):
    """Workspaces + notifications tables with three workspaces:

    - ws-a: ADMIN_A is workspace_admin; MEMBER_B is a plain member.
    - ws-b: MEMBER_B and OTHER_B are plain members.
    - ws-c: nobody above (except the org admin) can see it.
    """
    monkeypatch.setenv("NOTIFICATIONS_TABLE", NOTIF_TABLE)
    monkeypatch.delenv("REPLICATION_EVENT_BUS_NAME", raising=False)
    ddb = boto3.resource("dynamodb", region_name="us-west-2")
    ws = _create_table(ddb, WS_TABLE)
    notif = _create_table(ddb, NOTIF_TABLE)

    for ws_id in ("ws-a", "ws-b", "ws-c"):
        ws.put_item(Item={
            "PK": f"WS#{ws_id}", "SK": "METADATA", "workspace_id": ws_id,
            "name": ws_id,
        })

    def member(email, ws_id, role):
        ws.put_item(Item={
            "PK": f"USER#{email}", "SK": f"WS_MEMBER#{ws_id}",
            "workspace_id": ws_id, "user_email": email, "role": role,
        })

    member(ADMIN_A, "ws-a", "workspace_admin")
    member(MEMBER_B, "ws-a", "member")
    member(MEMBER_B, "ws-b", "member")
    member(OTHER_B, "ws-b", "member")
    ws.put_item(Item={
        "PK": f"USER#{ORG_ADMIN}", "SK": "ORG_MEMBER", "org_role": "org_admin",
    })

    rows = [
        _notif("ws-a", "a1", OTHER_B, "2026-11-01T00:00:00+00:00"),
        _notif("ws-a", "a2", MEMBER_B, "2026-11-03T00:00:00+00:00"),
        _notif("ws-b", "b1", MEMBER_B, "2026-11-02T00:00:00+00:00"),
        _notif("ws-b", "b2", OTHER_B, "2026-11-04T00:00:00+00:00"),
        _notif("ws-c", "c1", OUTSIDER, "2026-11-05T00:00:00+00:00"),
    ]
    for row in rows:
        notif.put_item(Item=row)

    audit = MagicMock()
    with patch.object(handler, "_dynamodb", None), \
            patch.object(handler, "_get_workspaces_table", return_value=WS_TABLE), \
            patch.object(handler, "log_admin_action", audit):
        yield {"ws": ws, "notif": notif, "audit": audit}


def _call(method, path, email):
    event = {
        "headers": {"x-user-email": email} if email else {},
        "requestContext": {"http": {"method": method, "path": path}},
    }
    return handler.lambda_handler(event, None)


def _list_ids(email):
    resp = _call("GET", "/notifications", email)
    assert resp["statusCode"] == 200
    return [n["notification_id"] for n in json.loads(resp["body"])["notifications"]]


def _dismiss_audit_calls(audit):
    return [
        c for c in audit.call_args_list
        if c.args[1] == handler.ACTION_NOTIFICATION_DISMISSED
    ]


class TestListNotifications:
    def test_workspace_admin_sees_all_rows_of_admin_workspace(self, env):
        assert _list_ids(ADMIN_A) == ["a2", "a1"]

    def test_plain_member_sees_only_own_rows(self, env):
        # ws-a: only a2 (added_by MEMBER_B); ws-b: only b1. Newest first.
        assert _list_ids(MEMBER_B) == ["a2", "b1"]

    def test_non_member_sees_nothing_of_other_workspaces(self, env):
        assert _list_ids(OUTSIDER) == []
        assert "c1" not in _list_ids(OTHER_B)

    def test_org_admin_sees_all(self, env):
        assert _list_ids(ORG_ADMIN) == ["c1", "b2", "a2", "b1", "a1"]

    def test_hidden_fields_stripped(self, env):
        resp = _call("GET", "/notifications", ADMIN_A)
        for n in json.loads(resp["body"])["notifications"]:
            assert "PK" not in n and "SK" not in n and "ttl" not in n
            assert n["failed_files"] == ["a.py"]
            assert n["workspace_id"] == "ws-a"

    def test_missing_user_returns_401(self, env):
        assert _call("GET", "/notifications", None)["statusCode"] == 401

    def test_unconfigured_table_returns_empty(self, env, monkeypatch):
        monkeypatch.delenv("NOTIFICATIONS_TABLE")
        assert _list_ids(ADMIN_A) == []

    def test_ddb_error_returns_500(self, env):
        err = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceededException",
                       "Message": "slow down"}},
            "Query",
        )
        with patch.object(handler, "_query_workspace_notifications",
                          side_effect=err):
            resp = _call("GET", "/notifications", ADMIN_A)
        assert resp["statusCode"] == 500

    def test_pagination_follows_last_evaluated_key(self):
        table = MagicMock()
        table.query.side_effect = [
            {"Items": [{"notification_id": "p1"}],
             "LastEvaluatedKey": {"PK": "WS#ws-a", "SK": "NOTIF#p1"}},
            {"Items": [{"notification_id": "p2"}]},
        ]
        items = handler._query_workspace_notifications(table, "ws-a")
        assert [i["notification_id"] for i in items] == ["p1", "p2"]
        assert table.query.call_count == 2
        second = table.query.call_args_list[1].kwargs
        assert second["ExclusiveStartKey"] == {"PK": "WS#ws-a", "SK": "NOTIF#p1"}
        assert second["ExpressionAttributeValues"] == {
            ":pk": "WS#ws-a", ":sk": "NOTIF#",
        }

    def test_pagination_end_to_end_two_pages(self, env):
        # Force Limit=1 on the moto table so each workspace takes 2+ pages.
        original = handler._query_workspace_notifications

        def limited(notif_table, ws_id):
            wrapped = MagicMock(wraps=notif_table)
            wrapped.query.side_effect = lambda **kw: notif_table.query(
                Limit=1, **kw
            )
            return original(wrapped, ws_id)

        with patch.object(handler, "_query_workspace_notifications", limited):
            assert _list_ids(ADMIN_A) == ["a2", "a1"]


class TestDismissNotification:
    def _exists(self, env, ws, nid):
        return "Item" in env["notif"].get_item(
            Key={"PK": f"WS#{ws}", "SK": f"NOTIF#{nid}"}
        )

    def test_admin_dismisses_any_row(self, env):
        resp = _call("DELETE", "/workspaces/ws-a/notifications/a1", ADMIN_A)
        assert resp["statusCode"] == 204
        assert resp["body"] == ""
        assert not self._exists(env, "ws-a", "a1")
        calls = _dismiss_audit_calls(env["audit"])
        assert len(calls) == 1
        assert calls[0].kwargs["workspace_id"] == "ws-a"
        assert calls[0].kwargs["user_email"] == ADMIN_A
        assert calls[0].kwargs["details"]["notification_id"] == "a1"

    def test_added_by_dismisses_own_row(self, env):
        resp = _call("DELETE", "/workspaces/ws-b/notifications/b1", MEMBER_B)
        assert resp["statusCode"] == 204
        assert not self._exists(env, "ws-b", "b1")
        assert len(_dismiss_audit_calls(env["audit"])) == 1

    def test_other_member_forbidden(self, env):
        resp = _call("DELETE", "/workspaces/ws-b/notifications/b2", MEMBER_B)
        assert resp["statusCode"] == 403
        assert self._exists(env, "ws-b", "b2")
        assert _dismiss_audit_calls(env["audit"]) == []

    def test_non_member_forbidden_even_for_unknown_id(self, env):
        resp = _call("DELETE", "/workspaces/ws-c/notifications/nope", OUTSIDER)
        assert resp["statusCode"] == 403
        resp = _call("DELETE", "/workspaces/ws-c/notifications/c1", OUTSIDER)
        assert resp["statusCode"] == 403
        assert self._exists(env, "ws-c", "c1")

    def test_org_admin_dismisses_in_any_workspace(self, env):
        resp = _call("DELETE", "/workspaces/ws-c/notifications/c1", ORG_ADMIN)
        assert resp["statusCode"] == 204
        assert not self._exists(env, "ws-c", "c1")

    def test_unknown_id_returns_404(self, env):
        resp = _call("DELETE", "/workspaces/ws-a/notifications/missing", ADMIN_A)
        assert resp["statusCode"] == 404
        assert _dismiss_audit_calls(env["audit"]) == []

    def test_dismissed_row_disappears_for_everyone(self, env):
        assert "a2" in _list_ids(MEMBER_B)
        _call("DELETE", "/workspaces/ws-a/notifications/a2", ADMIN_A)
        assert "a2" not in _list_ids(MEMBER_B)
        assert "a2" not in _list_ids(ADMIN_A)

    def test_delete_goes_through_dual_write(self, env):
        with patch.object(handler.dual_write, "ddb_delete_item") as dw:
            resp = _call("DELETE", "/workspaces/ws-a/notifications/a1", ADMIN_A)
        assert resp["statusCode"] == 204
        dw.assert_called_once_with(
            NOTIF_TABLE, {"PK": "WS#ws-a", "SK": "NOTIF#a1"},
        )

    def test_passive_region_blocks(self, env):
        with patch.object(handler, "guard_active",
                          side_effect=handler.RegionPassiveError("passive")):
            resp = _call("DELETE", "/workspaces/ws-a/notifications/a1", ADMIN_A)
        assert resp["statusCode"] == 403
        assert self._exists(env, "ws-a", "a1")

    def test_unconfigured_table_returns_503(self, env, monkeypatch):
        monkeypatch.delenv("NOTIFICATIONS_TABLE")
        resp = _call("DELETE", "/workspaces/ws-a/notifications/a1", ADMIN_A)
        assert resp["statusCode"] == 503

    def test_ddb_error_returns_500(self, env):
        err = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "boom"}},
            "DeleteItem",
        )
        with patch.object(handler.dual_write, "ddb_delete_item",
                          side_effect=err):
            resp = _call("DELETE", "/workspaces/ws-a/notifications/a1", ADMIN_A)
        assert resp["statusCode"] == 500
        assert _dismiss_audit_calls(env["audit"]) == []
