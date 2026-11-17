# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the analytics endpoints in workspace-management Lambda.

The handlers read pre-aggregated daily rollups from the analytics table:

  PK=WS#{workspace_id}, SK=DT#{date}              -> day totals
  PK=WS#{workspace_id}, SK=DM#{date}#{model_id}   -> day x model
  PK=WS#{workspace_id}, SK=DU#{date}#{email}      -> day x user
"""

import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch


def _load_handler_module():
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
_parse_sk_date_and_suffix = handler_module._parse_sk_date_and_suffix
_agg_num = handler_module._agg_num
_agg_int = handler_module._agg_int
lambda_handler = handler_module.lambda_handler


# ---------------------------------------------------------------------------
# Aggregate item factories
# ---------------------------------------------------------------------------


def _dt(workspace_id, date, queries=1, cost="0.10", input_tokens=100,
        output_tokens=50, duration_ms=500, refusals=0, errors=0):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"DT#{date}",
        "queries": queries,
        "cost_usd": Decimal(str(cost)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "total_duration_ms": duration_ms,
        "refusal_count": refusals,
        "error_count": errors,
    }


def _dm(workspace_id, date, model_id, queries=1, cost="0.10",
        input_tokens=100, output_tokens=50):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"DM#{date}#{model_id}",
        "queries": queries,
        "cost_usd": Decimal(str(cost)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }


def _du(workspace_id, date, email, queries=1, cost="0.10",
        input_tokens=100, output_tokens=50, last_active=""):
    item = {
        "PK": f"WS#{workspace_id}",
        "SK": f"DU#{date}#{email}",
        "queries": queries,
        "cost_usd": Decimal(str(cost)),
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
    }
    if last_active:
        item["last_active_ts"] = last_active
    return item


# ---------------------------------------------------------------------------
# Mock-table builder
# ---------------------------------------------------------------------------


def _make_tables(
    workspace_items=None,
    analytics_items=None,
    org_admin=False,
    workspace_admin_ids=None,
    user_workspace_ids=None,
    workspace_metadata=None,
):
    ws_table = MagicMock()
    analytics_table = MagicMock()
    workspace_admin_ids = set(workspace_admin_ids or [])
    user_workspace_ids = list(user_workspace_ids or [])
    workspace_metadata = workspace_metadata or {}
    analytics_items = list(analytics_items or [])

    def ws_get_item(Key):
        pk = Key["PK"]
        sk = Key["SK"]
        if sk == "ORG_MEMBER":
            return {"Item": {"org_role": "org_admin"}} if org_admin else {}
        if sk.startswith("WS_MEMBER#"):
            ws_id = sk.split("#", 1)[1]
            if ws_id in workspace_admin_ids:
                return {"Item": {"role": "workspace_admin", "workspace_id": ws_id}}
            if ws_id in user_workspace_ids:
                return {"Item": {"role": "member", "workspace_id": ws_id}}
            return {}
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
        if pk.startswith("USER#"):
            return {
                "Items": [
                    {"workspace_id": wid, "role": "member"} for wid in user_workspace_ids
                ]
            }
        return {"Items": []}

    ws_table.query.side_effect = ws_query

    def ws_scan(**kwargs):
        items = []
        for wid in (workspace_items or []):
            base = {
                "workspace_id": wid,
                "name": workspace_metadata.get(wid, {}).get("name", wid),
                "SK": "METADATA",
            }
            # Merge any extra metadata fields (e.g. monthly_budget_usd) onto
            # the scanned item so budget-aware reads see them.
            base.update(workspace_metadata.get(wid, {}))
            items.append(base)
        return {"Items": items}

    ws_table.scan.side_effect = ws_scan

    def analytics_query(**kwargs):
        expr_values = kwargs.get("ExpressionAttributeValues", {})
        pk = expr_values.get(":pk", "")
        start = expr_values.get(":s", "")
        end = expr_values.get(":e", "")
        ws_id = pk.replace("WS#", "")
        out = []
        for it in analytics_items:
            if it.get("PK") != f"WS#{ws_id}":
                continue
            sk = it.get("SK", "")
            if start <= sk <= end:
                out.append(it)
        return {"Items": out}

    analytics_table.query.side_effect = analytics_query

    ws_name = "test-workspaces"
    analytics_name = "test-analytics"

    def table_factory(name):
        if name == ws_name:
            return ws_table
        if name == analytics_name:
            return analytics_table
        return MagicMock()

    dynamodb = MagicMock()
    dynamodb.Table.side_effect = table_factory
    return dynamodb, ws_table, analytics_table, ws_name, analytics_name


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_date_range_defaults(self):
        start, end, err = _parse_analytics_date_range({})
        assert err is None
        assert 29 <= (datetime.fromisoformat(end) - datetime.fromisoformat(start)).days <= 30

    def test_parse_date_range_rejects_over_90_days(self):
        now = datetime.now(timezone.utc)
        _, _, err = _parse_analytics_date_range({
            "start_date": (now - timedelta(days=120)).isoformat(),
            "end_date": now.isoformat(),
        })
        assert err is not None
        assert "90" in err

    def test_parse_date_range_invalid(self):
        _, _, err = _parse_analytics_date_range({"start_date": "nope"})
        assert err is not None

    def test_parse_sk_totals(self):
        date, suffix = _parse_sk_date_and_suffix("DT#2026-04-10")
        assert date == "2026-04-10"
        assert suffix == ""

    def test_parse_sk_model(self):
        date, suffix = _parse_sk_date_and_suffix(
            "DM#2026-04-10#us.anthropic.sonnet"
        )
        assert date == "2026-04-10"
        assert suffix == "us.anthropic.sonnet"

    def test_parse_sk_user_with_hash_in_email_preserved(self):
        # split(sep, 2) keeps everything after the 2nd '#' intact even if the
        # email itself contained a '#' (unlikely but defensive).
        date, suffix = _parse_sk_date_and_suffix("DU#2026-04-10#x#y@z")
        assert date == "2026-04-10"
        assert suffix == "x#y@z"

    def test_agg_num_handles_decimal(self):
        assert _agg_num({"cost_usd": Decimal("0.42")}, "cost_usd") == 0.42

    def test_agg_int_handles_missing(self):
        assert _agg_int({}, "queries") == 0


# ---------------------------------------------------------------------------
# Summary tests
# ---------------------------------------------------------------------------


class TestAnalyticsSummary:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_aggregates_dt_and_dm(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        items = [
            _dt("ws-1", "2026-04-10", queries=3, cost="0.30"),
            _dt("ws-1", "2026-04-11", queries=2, cost="0.20"),
            _dm("ws-1", "2026-04-10", "sonnet", queries=2, cost="0.20"),
            _dm("ws-1", "2026-04-10", "haiku", queries=1, cost="0.10"),
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"],
            analytics_items=items,
            org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "Engineering"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 5
        assert round(body["total_cost_usd"], 2) == 0.50
        assert body["by_workspace"][0]["name"] == "Engineering"
        # by_model is only populated from DM rows
        assert set(body["by_model"].keys()) == {"sonnet", "haiku"}
        # daily_trend sourced from DT rows, sorted by date
        assert [d["date"] for d in body["daily_trend"]] == ["2026-04-10", "2026-04-11"]

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_member_sees_only_own_usage(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        items = [
            _du("ws-1", "2026-04-10", "alice@x", queries=2, cost="0.20"),
            _du("ws-1", "2026-04-10", "bob@x", queries=5, cost="0.50"),
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"],
            analytics_items=items,
            org_admin=False,
            user_workspace_ids=["ws-1"],
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("alice@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 2
        assert round(body["total_cost_usd"], 2) == 0.20

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_over_90_days(self, mock_dynamodb, mock_analytics, mock_ws):
        mock_analytics.return_value = "a"
        mock_ws.return_value = "w"
        mock_dynamodb.return_value = MagicMock()
        start = (datetime.now(timezone.utc) - timedelta(days=120)).isoformat()
        end = datetime.now(timezone.utc).isoformat()
        result = get_analytics_summary("u@x", {"start_date": start, "end_date": end})
        assert result["statusCode"] == 400

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_analytics_table_not_configured(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        mock_analytics.return_value = ""
        mock_ws.return_value = "w"
        result = get_analytics_summary("u@x", {})
        assert result["statusCode"] == 500

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_workspace_filter_requires_access(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"],
            org_admin=False,
            user_workspace_ids=[],
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("stranger@x", {"workspace_id": "ws-1"})
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_summary_empty_result(self, mock_dynamodb, mock_analytics, mock_ws):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=[], org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_summary("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-02T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["total_queries"] == 0
        assert body["total_cost_usd"] == 0.0


# ---------------------------------------------------------------------------
# Users tests
# ---------------------------------------------------------------------------


class TestAnalyticsUsers:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_returns_top_users(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        items = [
            _du("ws-1", "2026-04-10", "alice@x", queries=2, cost="0.15",
                last_active="2026-04-11T00:00:00+00:00"),
            _du("ws-1", "2026-04-11", "alice@x", queries=3, cost="0.10",
                last_active="2026-04-11T10:00:00+00:00"),
            _du("ws-1", "2026-04-12", "bob@x", queries=1, cost="0.20"),
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=items, org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        alice = next(u for u in body["users"] if u["email"] == "alice@x")
        bob = next(u for u in body["users"] if u["email"] == "bob@x")
        assert alice["queries"] == 5
        assert round(alice["cost_usd"], 2) == 0.25
        assert alice["last_active"] == "2026-04-11T10:00:00+00:00"
        assert bob["queries"] == 1

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_respects_limit(self, mock_dynamodb, mock_analytics, mock_ws):
        items = [
            _du("ws-1", f"2026-04-{10+i:02d}", f"u{i}@x", queries=10 - i)
            for i in range(5)
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=items, org_admin=True,
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "WS"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("admin@x", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
            "limit": "2",
        })
        body = json.loads(result["body"])
        assert len(body["users"]) == 2
        # sorted by queries desc
        assert body["users"][0]["queries"] >= body["users"][1]["queries"]

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_users_non_admin_denied(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=[], org_admin=False,
            user_workspace_ids=["ws-1"],
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_users("member@x", {})
        assert result["statusCode"] == 403


# ---------------------------------------------------------------------------
# Workspace detail tests
# ---------------------------------------------------------------------------


class TestAnalyticsWorkspace:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_detail_from_rollups(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        items = [
            _dt("ws-1", "2026-04-10", queries=5, cost="0.50",
                duration_ms=5000, refusals=1, errors=1),
            _dt("ws-1", "2026-04-11", queries=3, cost="0.30",
                duration_ms=1500),
            _dm("ws-1", "2026-04-10", "sonnet", queries=3, cost="0.30"),
            _dm("ws-1", "2026-04-11", "sonnet", queries=3, cost="0.30"),
            _du("ws-1", "2026-04-10", "alice@x", queries=4, cost="0.40"),
            _du("ws-1", "2026-04-11", "bob@x", queries=2, cost="0.20"),
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=items,
            user_workspace_ids=["ws-1"],
            workspace_metadata={"ws-1": {"workspace_id": "ws-1", "name": "Engineering"}},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("alice@x", "ws-1", {
            "start_date": "2026-04-01T00:00:00+00:00",
            "end_date": "2026-04-20T00:00:00+00:00",
        })
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["workspace_name"] == "Engineering"
        assert body["total_queries"] == 8  # from DT rows
        assert round(body["total_cost_usd"], 2) == 0.80
        assert body["avg_duration_ms"] == int(6500 / 8)
        assert body["refusal_count"] == 1
        assert body["error_count"] == 1
        assert len(body["by_user"]) == 2
        assert len(body["by_model"]) == 1

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_access_denied(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1"], analytics_items=[], org_admin=False,
            user_workspace_ids=[],
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("stranger@x", "ws-1", {})
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_workspace_not_found(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=[], analytics_items=[], org_admin=True,
            workspace_metadata={},
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = get_analytics_workspace("admin@x", "ws-missing", {})
        assert result["statusCode"] == 404


# ---------------------------------------------------------------------------
# Routing tests
# ---------------------------------------------------------------------------


class TestWorkspaceBudget:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_get_budget_returns_value(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        # First get_item: org_admin check (not admin); second: membership check;
        # third: workspace metadata with budget
        ws_table.get_item.side_effect = [
            {},  # org_admin check
            {"Item": {"role": "member"}},  # _can_access_workspace membership
            {"Item": {
                "workspace_id": "ws-1",
                "monthly_budget_usd": Decimal("250.00"),
                "budget_updated_at": "2026-04-15T00:00:00+00:00",
                "budget_updated_by": "admin@x",
            }},
        ]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.get_workspace_budget("user@x", "ws-1")
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["monthly_budget_usd"] == 250.0
        assert body["is_default"] is False

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_get_budget_default_when_unset(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        ws_table.get_item.side_effect = [
            {},
            {"Item": {"role": "member"}},
            {"Item": {"workspace_id": "ws-1"}},  # no budget field
        ]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.get_workspace_budget("user@x", "ws-1")
        body = json.loads(result["body"])
        assert body["monthly_budget_usd"] is None
        assert body["is_default"] is True

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_get_budget_forbids_non_member(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        ws_table.get_item.side_effect = [
            {},  # org_admin check
            {},  # no membership
        ]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.get_workspace_budget("stranger@x", "ws-1")
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_budget_requires_org_admin(self, mock_ddb, mock_ws):
        # Even a workspace_admin cannot update the budget — budget is a
        # governance control owned by the org admins.
        ws_table = MagicMock()
        ws_table.get_item.side_effect = [{}]  # org_admin check: false
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.update_workspace_budget(
            "ws_admin@x", "ws-1", {"monthly_budget_usd": 100}
        )
        assert result["statusCode"] == 403
        body = json.loads(result["body"])
        assert "org admin" in body["error"].lower()

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_budget_sets_value(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        # admin check passes (org_admin)
        ws_table.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},  # org_admin check
            {"Item": {"workspace_id": "ws-1"}},  # workspace exists
        ]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.update_workspace_budget(
            "admin@x", "ws-1", {"monthly_budget_usd": 250}
        )
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["monthly_budget_usd"] == 250.0
        assert body["is_default"] is False
        # update_item called with SET path, not REMOVE
        update_call = ws_table.update_item.call_args
        assert "SET monthly_budget_usd" in update_call.kwargs["UpdateExpression"]

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_budget_zero_clears_override(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        ws_table.get_item.side_effect = [
            {"Item": {"org_role": "org_admin"}},
            {"Item": {"workspace_id": "ws-1"}},
        ]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.update_workspace_budget(
            "admin@x", "ws-1", {"monthly_budget_usd": 0}
        )
        body = json.loads(result["body"])
        assert result["statusCode"] == 200
        assert body["monthly_budget_usd"] is None
        assert body["is_default"] is True
        update_call = ws_table.update_item.call_args
        assert "REMOVE monthly_budget_usd" in update_call.kwargs["UpdateExpression"]

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_update_budget_rejects_negative(self, mock_ddb, mock_ws):
        ws_table = MagicMock()
        ws_table.get_item.side_effect = [{"Item": {"org_role": "org_admin"}}]
        ddb = MagicMock()
        ddb.Table.return_value = ws_table
        mock_ddb.return_value = ddb
        mock_ws.return_value = "ws"

        result = handler_module.update_workspace_budget(
            "admin@x", "ws-1", {"monthly_budget_usd": -5}
        )
        assert result["statusCode"] == 400


class TestAnalyticsBudgets:
    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_budgets_requires_org_admin(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=[], analytics_items=[], org_admin=False,
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = handler_module.get_analytics_budgets("member@x")
        assert result["statusCode"] == 403

    @patch.object(handler_module, "_get_workspaces_table")
    @patch.object(handler_module, "_get_analytics_table")
    @patch.object(handler_module, "_get_dynamodb")
    def test_budgets_lists_all_workspaces_with_spend(
        self, mock_dynamodb, mock_analytics, mock_ws
    ):
        today = datetime.now(timezone.utc)
        date_key = today.strftime("%Y-%m-%d")
        items = [
            _dt("ws-1", date_key, queries=5, cost="42.50"),
            _dt("ws-2", date_key, queries=2, cost="10.00"),
        ]
        ddb, _, _, ws_name, analytics_name = _make_tables(
            workspace_items=["ws-1", "ws-2"],
            analytics_items=items,
            org_admin=True,
            workspace_metadata={
                "ws-1": {
                    "workspace_id": "ws-1",
                    "name": "Engineering",
                    "monthly_budget_usd": Decimal("100"),
                },
                "ws-2": {
                    "workspace_id": "ws-2",
                    "name": "Data Science",
                },
            },
        )
        mock_dynamodb.return_value = ddb
        mock_analytics.return_value = analytics_name
        mock_ws.return_value = ws_name

        result = handler_module.get_analytics_budgets("admin@x")
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        # sorted by MTD spend desc
        assert body["workspaces"][0]["workspace_id"] == "ws-1"
        assert body["workspaces"][0]["monthly_budget_usd"] == 100.0
        assert body["workspaces"][0]["is_default"] is False
        assert body["workspaces"][0]["mtd_spend_usd"] == 42.5
        # ws-2 has no override -> is_default True
        assert body["workspaces"][1]["is_default"] is True
        assert body["workspaces"][1]["monthly_budget_usd"] is None
        # Month key is present
        assert body["month"] == today.strftime("%Y-%m")


class TestAnalyticsRouting:
    @patch.object(handler_module, "get_analytics_summary")
    def test_routes_summary(self, mock_fn):
        mock_fn.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/summary",
            "headers": {"x-user-email": "admin@example.com"},
            "queryStringParameters": {"start_date": "2026-04-01T00:00:00+00:00"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_fn.assert_called_once()

    @patch.object(handler_module, "get_analytics_users")
    def test_routes_users(self, mock_fn):
        mock_fn.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/users",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        mock_fn.assert_called_once()

    @patch.object(handler_module, "get_analytics_workspace")
    def test_routes_workspace(self, mock_fn):
        mock_fn.return_value = {"statusCode": 200, "body": "{}"}
        event = {
            "httpMethod": "GET",
            "path": "/analytics/workspace/ws-1",
            "headers": {"x-user-email": "admin@example.com"},
        }
        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        call_args = mock_fn.call_args
        assert call_args.args[1] == "ws-1"
