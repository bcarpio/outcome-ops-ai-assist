"""Unit tests for the analytics-cost-alert Lambda."""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

from botocore.exceptions import ClientError


def _load_handler_module():
    """Load the cost-alert handler without polluting the global os.environ.

    Module-level constants (ANALYTICS_TABLE, ORG_MONTHLY_USD, ...) are read
    from env vars at import time; we patch them directly on the module after
    loading instead so unrelated test modules see a clean environment.
    """
    if "analytics_cost_alert_handler" in sys.modules:
        del sys.modules["analytics_cost_alert_handler"]

    handler_path = os.path.join(
        os.path.dirname(__file__), "../../analytics-cost-alert/handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "analytics_cost_alert_handler", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["analytics_cost_alert_handler"] = module
    spec.loader.exec_module(module)

    # Apply test defaults directly to the loaded module.
    module.ANALYTICS_TABLE = "test-analytics"
    module.WORKSPACES_TABLE = "test-workspaces"
    module.SNS_TOPIC_ARN = "arn:aws:sns:us-east-1:000000000000:cost-alerts"
    module.ORG_MONTHLY_USD = 100.0
    module.WORKSPACE_MONTHLY_USD = 50.0
    module.ENV = "test"
    return module


handler_module = _load_handler_module()


def _make_workspaces_table(workspaces):
    """Return a mock workspaces table scan-returning the given METADATA items.

    ``workspaces`` is a sequence of (wid, name) or (wid, name, budget) tuples.
    Tuples without a budget default to no override.
    """
    items = []
    for entry in workspaces:
        if len(entry) == 2:
            wid, name = entry
            items.append({"workspace_id": wid, "name": name})
        else:
            wid, name, budget = entry
            items.append({
                "workspace_id": wid,
                "name": name,
                "monthly_budget_usd": Decimal(str(budget)),
            })
    table = MagicMock()
    table.scan.return_value = {"Items": items}
    return table


def _make_analytics_table(cost_by_ws, claim_failures=None):
    """Return a mock analytics table that:
    - Query returns DT rollup items summing to ``cost_by_ws[workspace_id]``
    - PutItem raises ConditionalCheckFailedException for SKs in ``claim_failures``
    """
    claim_failures = set(claim_failures or [])
    table = MagicMock()

    def _query(**kwargs):
        pk = kwargs["ExpressionAttributeValues"][":pk"]
        wid = pk.replace("WS#", "")
        cost = cost_by_ws.get(wid, 0.0)
        if cost == 0:
            return {"Items": []}
        return {"Items": [{"cost_usd": Decimal(str(cost))}]}

    table.query.side_effect = _query

    def _put(**kwargs):
        sk = kwargs["Item"]["SK"]
        if sk in claim_failures:
            raise ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "PutItem",
            )

    table.put_item.side_effect = _put
    return table


def _setup(mock_ddb, workspaces, cost_by_ws, claim_failures=None):
    analytics_table = _make_analytics_table(cost_by_ws, claim_failures)
    ws_table = _make_workspaces_table(workspaces)

    def _table(name):
        if name == "test-analytics":
            return analytics_table
        if name == "test-workspaces":
            return ws_table
        return MagicMock()

    ddb_resource = MagicMock()
    ddb_resource.Table.side_effect = _table
    mock_ddb.return_value = ddb_resource
    return analytics_table, ws_table


class TestMonthBounds:
    def test_month_bounds(self):
        now = datetime(2026, 4, 15, 13, 0, 0, tzinfo=timezone.utc)
        month_key, start, end = handler_module._month_bounds(now)
        assert month_key == "2026-04"
        assert start == "2026-04-01"
        assert end == "2026-04-15"


class TestHandler:
    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_no_alerts_when_under_thresholds(self, mock_sns, mock_ddb):
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering")],
            cost_by_ws={"ws-1": 10.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns

        result = handler_module.handler({}, None)
        assert result["statusCode"] == 200
        sns.publish.assert_not_called()

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_workspace_alert_published(self, mock_sns, mock_ddb):
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering")],
            cost_by_ws={"ws-1": 60.0},  # > 50 threshold
        )
        sns = MagicMock()
        mock_sns.return_value = sns

        handler_module.handler({}, None)

        assert sns.publish.call_count == 1
        call = sns.publish.call_args
        assert "Workspace budget" in call.kwargs["Subject"]
        assert "Engineering" in call.kwargs["Subject"]
        assert "$60.00" in call.kwargs["Message"]

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_org_alert_published(self, mock_sns, mock_ddb):
        _setup(
            mock_ddb,
            workspaces=[
                ("ws-1", "A"),
                ("ws-2", "B"),
            ],
            # sum = 120 which is > org threshold 100, each is under ws threshold 50
            cost_by_ws={"ws-1": 40.0, "ws-2": 80.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns

        handler_module.handler({}, None)

        # Workspace B is over $50 threshold -> 1 workspace alert + 1 org alert
        assert sns.publish.call_count == 2
        subjects = [c.kwargs["Subject"] for c in sns.publish.call_args_list]
        assert any("Organization budget" in s for s in subjects)
        assert any("Workspace budget" in s for s in subjects)

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_already_alerted_is_skipped(self, mock_sns, mock_ddb):
        month_key = datetime.now(timezone.utc).strftime("%Y-%m")
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering")],
            cost_by_ws={"ws-1": 75.0},
            claim_failures={f"MTD#{month_key}#WS#ws-1"},
        )
        sns = MagicMock()
        mock_sns.return_value = sns

        handler_module.handler({}, None)

        # Claim race lost -> no SNS publish
        sns.publish.assert_not_called()

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_zero_thresholds_publishes_nothing(self, mock_sns, mock_ddb):
        # All thresholds zero and no per-workspace overrides -> no alerts.
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "A")],
            cost_by_ws={"ws-1": 9999.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns
        with (
            patch.object(handler_module, "ORG_MONTHLY_USD", 0.0),
            patch.object(handler_module, "WORKSPACE_MONTHLY_USD", 0.0),
        ):
            result = handler_module.handler({}, None)
        assert result["statusCode"] == 200
        sns.publish.assert_not_called()

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_per_workspace_override_fires_when_global_is_zero(
        self, mock_sns, mock_ddb
    ):
        # Workspace override of $25 takes effect even when global default is 0.
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering", 25.0)],
            cost_by_ws={"ws-1": 30.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns
        with (
            patch.object(handler_module, "ORG_MONTHLY_USD", 0.0),
            patch.object(handler_module, "WORKSPACE_MONTHLY_USD", 0.0),
        ):
            handler_module.handler({}, None)
        assert sns.publish.call_count == 1
        msg = sns.publish.call_args.kwargs["Message"]
        assert "$25.00" in msg
        assert "workspace override" in msg

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_per_workspace_override_supersedes_global(
        self, mock_sns, mock_ddb
    ):
        # Workspace sets a high override ($500) so the lower global ($50)
        # should NOT fire even when the global would otherwise alert.
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering", 500.0)],
            cost_by_ws={"ws-1": 60.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns
        handler_module.handler({}, None)
        sns.publish.assert_not_called()

    @patch.object(handler_module, "_ddb")
    @patch.object(handler_module, "_sns_client")
    def test_workspace_without_override_uses_global(
        self, mock_sns, mock_ddb
    ):
        # No override on this workspace -> the $50 global threshold applies.
        _setup(
            mock_ddb,
            workspaces=[("ws-1", "Engineering")],
            cost_by_ws={"ws-1": 75.0},
        )
        sns = MagicMock()
        mock_sns.return_value = sns
        handler_module.handler({}, None)
        assert sns.publish.call_count == 1
        msg = sns.publish.call_args.kwargs["Message"]
        assert "org default" in msg

    @patch.object(handler_module, "_ddb")
    def test_handler_without_tables_env(self, mock_ddb):
        with patch.object(handler_module, "ANALYTICS_TABLE", ""):
            result = handler_module.handler({}, None)
            assert result["statusCode"] == 200
            assert "not configured" in result["body"].lower()


class TestClaimAlert:
    def test_claim_alert_wins_race(self):
        table = MagicMock()
        ok = handler_module._claim_alert(table, "MTD#2026-04#ORG", 120.0, 100.0)
        assert ok is True
        table.put_item.assert_called_once()

    def test_claim_alert_loses_race(self):
        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem"
        )
        ok = handler_module._claim_alert(table, "MTD#2026-04#ORG", 120.0, 100.0)
        assert ok is False

    def test_claim_alert_reraises_unexpected(self):
        import pytest

        table = MagicMock()
        table.put_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceeded"}}, "PutItem"
        )
        with pytest.raises(ClientError):
            handler_module._claim_alert(table, "MTD#2026-04#ORG", 120.0, 100.0)
