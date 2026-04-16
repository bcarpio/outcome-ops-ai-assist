"""Unit tests for the analytics endpoints in workspace-management Lambda."""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    """Load a fresh workspace-management handler module."""
    if "workspace_management_handler" in sys.modules:
        del sys.modules["workspace_management_handler"]

    handler_path = os.path.join(
        os.path.dirname(__file__), "../../workspace-management/handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "workspace_management_handler", handler_path
    )
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["workspace_management_handler"] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


handler_module = _load_handler_module()

get_analytics_summary = handler_module.get_analytics_summary
get_analytics_users = handler_module.get_analytics_users
get_analytics_workspace = handler_module.get_analytics_workspace
_parse_analytics_date_range = handler_module._parse_analytics_date_range
_aggregate_audit_items = handler_module._aggregate_audit_items
_audit_cost = handler_module._audit_cost
_audit_int = handler_module._audit_int
lambda_handler = handler_module.lambda_handler


def _make_audit_item(
    workspace_id="ws-1",
    user_email="alice@example.com",
    model_id="us.anthropic.claude-sonnet-4-6",
    cost="0.10",
    input_tokens=100,
    output_tokens=50,
    duration_ms=500,
    status="success",
    timestamp="2026-04-10T12:00:00+00:00",
):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"{timestamp}#req1",
        "workspace_id": workspace_id,
        "user_email": user_email,
        "model_id": model_id,
        "cost_usd": cost,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "duration_ms": duration_ms,
        "status": status,
        "timestamp": timestamp,
    }


def _make_tables(workspace_items=None, audit_items=None, org_admin=False,
                 workspace_admin_ids=None, user_workspace_ids=None,
                 workspace_metadata=None):
    """Build mock ws_table and audit_table.

    The returned dynamodb resource will return `ws_table` when called with the
    workspaces table name and `audit_table` when called with the audit table name.
    """
    ws_table = MagicMock()
    audit_table = MagicMock()
    workspace_admin_ids = set(workspace_admin_ids or [])
    user_workspace_ids = list(user_workspace_ids or [])
    workspace_metadata = workspace_metadata or {}

    def ws_get_item(Key):
        pk = Key["PK"]
        sk = Key["SK"]
        # Org admin check: PK=USER#..., SK=ORG_MEMBER
        if sk == "ORG_MEMBER":
            if org_admin:
                return {"Item": {"org_role": "org_admin"}}
            return {}
        # Workspace member check: PK=USER#..., SK=WS_MEMBER#{ws_id}
        if sk.startswith("WS_MEMBER#"):
            ws_id = sk.split("#", 1)[1]
            if ws_id in workspace_admin_ids:
                return {"Item": {"role": "workspace_admin", "workspace_id": ws_id}}
            if ws_id in user_workspace_ids:
                return {"Item": {"role": "member", "workspace_id": ws_id}}
            return {}
        # Workspace metadata lookup
        if sk == "METADATA" and pk.startswith("WS#"):
            ws_id = pk.split("#", 1)[1]
            if ws_id in workspace_metadata:
                return {"Item": workspace_metadata[ws_id]}
            return {}
        return {}

    ws_table.get_item.side_effect = ws_get_item

    def ws_query(**kwargs):
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        pk = expr_values.get(":pk", "")
        # User memberships
        if pk.startswith("USER#"):
            return {
                "Items": [
                    {"workspace_id": wid, "role": "member"} for wid in user_workspace_ids
                ]
            }
        return {"Items": []}

    ws_table.query.side_effect = ws_query

    def ws_scan(**kwargs):
        return {
            "Items": [
                {"workspace_id": wid, "name": workspace_metadata.get(wid, {}).get("name", wid), "SK": "METADATA"}
                for wid in (workspace_items or [])
            ]
        }

    ws_table.scan.side_effect = ws_scan

    def audit_query(**kwargs):
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        pk = expr_values.get(":pk", "")
        start = expr_values.get(":start", "")
        end = expr_values.get(":end", "")
        ws_id = pk.replace("WS#", "")
        items = [i for i in (audit_items or []) if i.get("workspace_id") == ws_id]
        items = [i for i in items if start <= i.get("SK", "") <= end]
        return {"Items": items}

    audit_table.query.side_effect = audit_query

    ws_table_name = "test-workspaces"
    audit_table_name = "test-audit"

    def table_factory(name):
        if name == ws_table_name:
            return ws_table
        if name == audit_table_name:
            return audit_table
        return MagicMock()

    dynamodb = MagicMock()
    dynamodb.Table.side_effect = table_factory

    return dynamodb, ws_table, audit_table, ws_table_name, audit_table_name


class TestHelperFunctions:
    def test_parse_date_range_defaults_to_30_days(self):
        start, end, err = _parse_analytics_date_range({})
        assert err is None
        start_dt = datetime.fromisoformat(start)
        end_dt = datetime.fromisoformat(end)
        assert 29 <= (end_dt - start_dt).days <= 30

    def test_parse_date_range_rejects_over_90_days(self):
        now = datetime.now(timezone.utc)
        start = (now - timedelta(days=120)).isoformat()
        _, _, err = _parse_analytics_date_range({
            "start_date": start,
            "end_date": now.isoformat(),
        })
        assert err is not None
        assert "90" in err

    def test_parse_date_range_rejects_invalid_format(self):
        _, _, err = _parse_analytics_date_range({"start_date": "not-a-date"})
        assert err is not None

    def test_parse_date_range_accepts_custom(self):
        start = "2026-04-01T00:00:00+00:00"
        end = "2026-04-15T00:00:00+00:00"
        s, e, err = _parse_analytics_date_range({
            "start_date": start, "end_date": end,
        })
        assert err is None
        assert s.startswith("2026-04-01")
        assert e.startswith("2026-04-15")

    def test_audit_cost_parses_string(self):
        assert _audit_cost({"cost_usd": "0.42"}) == 0.42

    def test_audit_cost_handles_missing(self):
        assert _audit_cost({}) == 0.0

    def test_audit_cost_handles_invalid(self):
        assert _audit_cost({"cost_usd": "not-a-number"}) == 0.0

    def test_audit_int_handles_missing(self):
        assert _audit_int({}, "input_tokens") == 0

    def test_aggregate_groups_by_model_and_workspace(self):
        items = [
            _make_audit_item(workspace_id="ws-1", model_id="sonnet", cost="0.10"),
            _make_audit_item(workspace_id="ws-1", model_id="haiku", cost="0.02"),
            _make_audit_item(workspace_id="ws-2", model_id="sonnet", cost="0.30"),
        ]
        summary = _aggregate_audit_items(items, {"ws-1": "One", "ws-2": "Two"})
        assert summary["total_queries"] == 3
        assert round(summary["total_cost_usd"], 2) == 0.42
        assert summary["by_model"]["sonnet"]["queries"] == 2
        assert summary["by_model"]["haiku"]["queries"] == 1
        ws_map = {w["workspace_id"]: w for w in summary["by_workspace"]}
        assert ws_map["ws-1"]["queries"] == 2
        assert ws_map["ws-2"]["name"] == "Two"

    def test_aggregate_daily_trend_sorted(self):
        items = [
            _make_audit_item(timestamp="2026-04-03T10:00:00+00:00"),
            _make_audit_item(timestamp="2026-04-01T10:00:00+00:00"),
            _make_audit_item(timestamp="2026-04-02T10:00:00+00:00"),
        ]
        summary = _aggregate_audit_items(items, {})
        dates = [d["date"] for d in summary["daily_trend"]]
        assert dates == ["2026-04-01", "2026-04-02", "2026-04-03"]


class TestAnalyticsSummary:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_returns_aggregated_data(
        self, mock_dynamodb, mock_audit, mock_ws_table_name
    ):
        audit_items = [
            _make_audit_item(workspace_id="ws-1", cost="0.10",
                             timestamp="2026-04-10T10:00:00+00:00"),
            _make_audit_item(workspace_id="ws-1", model_id="haiku", cost="0.02",
                             timestamp="2026-04-11T10:00:00+00:00"),
        ]
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"],
            audit_items=audit_items,
            org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "Engineering"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws_table_name.return_value = ws_name

        start = "2026-04-01T00:00:00+00:00"
        end = "2026-04-20T00:00:00+00:00"
        result = get_analytics_summary(
            "admin@example.com",
            {"start_date": start, "end_date": end},
        )

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 2
        assert round(body["total_cost_usd"], 2) == 0.12
        assert len(body["by_workspace"]) == 1
        assert body["by_workspace"][0]["name"] == "Engineering"
        assert len(body["by_model"]) == 2

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_max_90_day_range(self, mock_dynamodb, mock_audit, mock_ws):
        mock_audit.return_value = "audit"
        mock_ws.return_value = "ws"
        mock_dynamodb.return_value = MagicMock()
        start = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        end = datetime.now(timezone.utc).isoformat()
        result = get_analytics_summary("u@x", {"start_date": start, "end_date": end})
        assert result["statusCode"] == 400

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_audit_table_not_configured(
        self, mock_dynamodb, mock_audit, mock_ws
    ):
        mock_audit.return_value = ""
        mock_ws.return_value = "ws"
        result = get_analytics_summary("u@x", {})
        assert result["statusCode"] == 500

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_workspace_filter_requires_access(
        self, mock_dynamodb, mock_audit, mock_ws
    ):
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"],
            org_admin=False,
            user_workspace_ids=[],
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("stranger@x", {"workspace_id": "ws-1"})
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_member_sees_only_own_usage(
        self, mock_dynamodb, mock_audit, mock_ws
    ):
        audit_items = [
            _make_audit_item(workspace_id="ws-1", user_email="alice@example.com",
                             timestamp="2026-04-10T10:00:00+00:00", cost="0.10"),
            _make_audit_item(workspace_id="ws-1", user_email="bob@example.com",
                             timestamp="2026-04-10T11:00:00+00:00", cost="0.50"),
        ]
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"],
            audit_items=audit_items,
            org_admin=False,
            user_workspace_ids=["ws-1"],
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("alice@example.com", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 1
        assert round(body["total_cost_usd"], 2) == 0.10

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_empty_result(self, mock_dynamodb, mock_audit, mock_ws):
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=[], org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-02T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 0
        assert body["total_cost_usd"] == 0.0


class TestAnalyticsUsers:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_returns_top_users(self, mock_dynamodb, mock_audit, mock_ws):
        audit_items = [
            _make_audit_item(workspace_id="ws-1", user_email="alice@x", cost="0.10",
                             timestamp="2026-04-10T10:00:00+00:00"),
            _make_audit_item(workspace_id="ws-1", user_email="alice@x", cost="0.05",
                             timestamp="2026-04-11T10:00:00+00:00"),
            _make_audit_item(workspace_id="ws-1", user_email="bob@x", cost="0.20",
                             timestamp="2026-04-12T10:00:00+00:00"),
        ]
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=audit_items, org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        emails = [u["email"] for u in body["users"]]
        assert "alice@x" in emails and "bob@x" in emails
        alice = next(u for u in body["users"] if u["email"] == "alice@x")
        assert alice["queries"] == 2

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_respects_limit(self, mock_dynamodb, mock_audit, mock_ws):
        audit_items = [
            _make_audit_item(user_email=f"u{i}@x", cost="0.01",
                             timestamp=f"2026-04-{10+i:02d}T10:00:00+00:00")
            for i in range(5)
        ]
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=audit_items, org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
            "limit": "2",
        })
        body = json.loads(result["body"])
        assert len(body["users"]) == 2

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_non_admin_denied(self, mock_dynamodb, mock_audit, mock_ws):
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=[], org_admin=False,
            user_workspace_ids=["ws-1"],
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("member@x", {})
        assert result["statusCode"] == 403


class TestAnalyticsWorkspace:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_detail_returns_breakdown(
        self, mock_dynamodb, mock_audit, mock_ws
    ):
        audit_items = [
            _make_audit_item(workspace_id="ws-1", user_email="alice@x",
                             duration_ms=1000, cost="0.10",
                             timestamp="2026-04-10T10:00:00+00:00"),
            _make_audit_item(workspace_id="ws-1", user_email="bob@x",
                             duration_ms=2000, cost="0.20", status="refusal",
                             timestamp="2026-04-11T10:00:00+00:00"),
        ]
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=audit_items,
            user_workspace_ids=["ws-1"],
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "Engineering"}},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("alice@x", "ws-1", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["workspace_name"] == "Engineering"
        assert body["total_queries"] == 2
        assert body["avg_duration_ms"] == 1500
        assert body["refusal_count"] == 1
        assert len(body["by_user"]) == 2

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_access_denied(self, mock_dynamodb, mock_audit, mock_ws):
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=["ws-1"], audit_items=[], org_admin=False,
            user_workspace_ids=[],
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("stranger@x", "ws-1", {})
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_audit_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_not_found(self, mock_dynamodb, mock_audit, mock_ws):
        ddb, _, _, ws_name, audit_name = _make_tables(
            workspace_items=[], audit_items=[], org_admin=True,
            workspace_metadata={},
        )
        mock_dynamodb.return_value = ddb
        mock_audit.return_value = audit_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("admin@x", "ws-missing", {})
        assert result["statusCode"] == 404


class TestAnalyticsRouting:
    @patch.object(handler_module, "get_analytics_summary")
    def test_routes_summary(self, mock_summary):
        mock_summary.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/summary",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"start_date": "2026-04-01T00:00:00+00:00"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_summary.assert_called_once()

    @patch.object(handler_module, "get_analytics_users")
    def test_routes_users(self, mock_users):
        mock_users.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/users",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_users.assert_called_once()

    @patch.object(handler_module, "get_analytics_workspace")
    def test_routes_workspace(self, mock_ws):
        mock_ws.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/workspace/ws-1",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        call_args = mock_ws.call_args
        assert call_args.args[1] == "ws-1"
