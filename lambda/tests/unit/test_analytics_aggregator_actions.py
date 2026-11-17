# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the admin-action rollup branch of analytics-aggregator.

Covers the new DA / DC / DCU shapes and the category-based dispatch added in
plan/04-lambda-aggregator-actions.md. Existing Bedrock-side tests live in
test_analytics_aggregator.py and are unchanged.
"""

import importlib.util
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


def _load_handler_module():
    if "analytics_aggregator_handler" in sys.modules:
        del sys.modules["analytics_aggregator_handler"]

    handler_path = os.path.join(
        os.path.dirname(__file__), "../../analytics-aggregator/handler.py"
    )
    spec = importlib.util.spec_from_file_location(
        "analytics_aggregator_handler", handler_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["analytics_aggregator_handler"] = module
    os.environ["ANALYTICS_TABLE"] = "test-analytics"
    spec.loader.exec_module(module)
    return module


handler_module = _load_handler_module()
_process_record = handler_module._process_record
aggregate_admin_item = handler_module.aggregate_admin_item
_apply_action_update = handler_module._apply_action_update
_legacy_bedrock_category = handler_module._legacy_bedrock_category


def _make_admin_record(
    workspace_id="ws-1",
    user_email="alice@example.com",
    action="API_KEY_CREATED",
    timestamp="2026-04-10T12:00:00+00:00",
    request_id="req-admin-1",
    event_name="INSERT",
    category="admin",
):
    image = {
        "workspace_id": {"S": workspace_id},
        "user_email": {"S": user_email},
        "action": {"S": action},
        "timestamp": {"S": timestamp},
        "request_id": {"S": request_id},
    }
    if category:
        image["category"] = {"S": category}
    return {"eventName": event_name, "dynamodb": {"NewImage": image}}


def _make_bedrock_record(
    workspace_id="ws-1",
    user_email="alice@example.com",
    cost="0.10",
    timestamp="2026-04-10T12:00:00+00:00",
    request_id="req-b-1",
    category="bedrock",
):
    image = {
        "workspace_id": {"S": workspace_id},
        "user_email": {"S": user_email},
        "model_id": {"S": "us.anthropic.sonnet"},
        "cost_usd": {"S": cost},
        "input_tokens": {"N": "100"},
        "output_tokens": {"N": "50"},
        "duration_ms": {"N": "500"},
        "status": {"S": "success"},
        "timestamp": {"S": timestamp},
        "request_id": {"S": request_id},
    }
    if category:
        image["category"] = {"S": category}
    return {"eventName": "INSERT", "dynamodb": {"NewImage": image}}


# ============================================================================
# aggregate_admin_item
# ============================================================================


class TestAggregateAdminItem:
    def test_writes_three_rollups(self):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table,
            workspace_id="ws-1",
            user_email="alice@x",
            action="API_KEY_CREATED",
            timestamp="2026-04-10T12:00:00+00:00",
            request_id="r-1",
        )
        assert ok is True
        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert sks == [
            "DA#2026-04-10",
            "DC#2026-04-10#API_KEY_CREATED",
            "DCU#2026-04-10#alice@x",
        ]

    def test_skips_dcu_when_user_empty(self):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table,
            workspace_id="ws-1",
            user_email="",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00",
            request_id="r-2",
        )
        assert ok is True
        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert sks == ["DA#2026-04-10", "DC#2026-04-10#WORKSPACE_CREATED"]

    def test_lowercases_user_email_in_dcu(self):
        table = MagicMock()
        aggregate_admin_item(
            table=table,
            workspace_id="ws-1",
            user_email="MIX@Example.COM",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00",
            request_id="r-3",
        )
        dcu_sk = table.update_item.call_args_list[2].kwargs["Key"]["SK"]
        assert dcu_sk == "DCU#2026-04-10#mix@example.com"

    def test_returns_false_for_missing_workspace(self):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table, workspace_id="", user_email="a@x",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00", request_id="r",
        )
        assert ok is False
        assert table.update_item.call_count == 0

    def test_returns_false_for_system_workspace(self):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table, workspace_id="SYSTEM", user_email="a@x",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00", request_id="r",
        )
        assert ok is False

    def test_returns_false_for_missing_request_id(self):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table, workspace_id="ws-1", user_email="a@x",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00", request_id="",
        )
        assert ok is False

    def test_unknown_action_still_increments_da(self, caplog):
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table, workspace_id="ws-1", user_email="a@x",
            action="WIDGET_FROBBED",
            timestamp="2026-04-10T12:00:00+00:00", request_id="r-x",
        )
        assert ok is True
        # All three updates still emitted
        assert table.update_item.call_count == 3
        # Sentinel that an unknown action got logged at warning level
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert any("WIDGET_FROBBED" in r.getMessage() for r in warnings)

    def test_document_ingested_skipped_no_rollup_writes(self):
        """DOCUMENT_INGESTED rows are written to the audit table for
        forensic provenance but must NOT contribute to the dashboard
        rollups (one per ingested doc would dominate every other admin
        action). Aggregator returns False and emits zero update_item
        calls."""
        table = MagicMock()
        ok = aggregate_admin_item(
            table=table, workspace_id="ws-1", user_email="ingest@x",
            action="DOCUMENT_INGESTED",
            timestamp="2026-05-07T12:00:00+00:00", request_id="r-doc-1",
        )
        assert ok is False
        assert table.update_item.call_count == 0

    def test_action_count_uses_decimal(self):
        """ADR-009: numeric ADD operands round-trip through Decimal so future
        arithmetic doesn't drift on float."""
        table = MagicMock()
        aggregate_admin_item(
            table=table, workspace_id="ws-1", user_email="a@x",
            action="WORKSPACE_CREATED",
            timestamp="2026-04-10T12:00:00+00:00", request_id="r-d",
        )
        first_call = table.update_item.call_args_list[0]
        assert first_call.kwargs["ExpressionAttributeValues"][":one"] == Decimal("1")


class TestApplyActionUpdate:
    def test_duplicate_request_id_swallowed(self):
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        # Must not raise.
        _apply_action_update(
            table=table, pk="WS#ws-1", sk="DA#2026-04-10",
            request_id="dup", last_action_at="2026-04-10T12:00:00+00:00",
        )

    def test_other_client_errors_propagate(self):
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ProvisionedThroughputExceeded"}},
            "UpdateItem",
        )
        with pytest.raises(ClientError):
            _apply_action_update(
                table=table, pk="WS#ws-1", sk="DA#2026-04-10",
                request_id="r", last_action_at="2026-04-10T12:00:00+00:00",
            )


# ============================================================================
# _process_record dispatch
# ============================================================================


class TestProcessRecordDispatch:
    @patch.object(handler_module, "_get_table")
    def test_admin_category_routes_to_admin_path(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_admin_record())

        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert any(sk.startswith("DA#") for sk in sks)
        assert not any(sk.startswith("DT#") for sk in sks)

    @patch.object(handler_module, "_get_table")
    def test_bedrock_category_routes_to_bedrock_path(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_bedrock_record())

        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert any(sk.startswith("DT#") for sk in sks)
        assert not any(sk.startswith("DA#") for sk in sks)

    @patch.object(handler_module, "_get_table")
    def test_legacy_bedrock_row_without_category_still_aggregates(self, mock_get_table):
        """Pre-existing rows have no 'category' but do have cost_usd. The
        legacy fallback keeps them on the bedrock path."""
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_bedrock_record(category=None))

        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert any(sk.startswith("DT#") for sk in sks)

    @patch.object(handler_module, "_get_table")
    def test_legacy_admin_row_without_category_or_cost_is_skipped(self, mock_get_table):
        """The two existing system-prompt audit rows have no category and no
        cost_usd. They net to zero aggregation; the legacy fallback returns
        None and dispatch falls through. Documented behavior, not a bug."""
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_admin_record(category=None))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_auth_category_is_no_op(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_admin_record(category="auth"))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_idempotent_replay_dt_admin(self, mock_get_table):
        """Replaying the same admin record twice produces the same aggregate
        state. We simulate this by raising ConditionalCheckFailedException on
        every update_item call - the helper swallows it."""
        table = MagicMock()
        table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        mock_get_table.return_value = table

        # Should not raise
        _process_record(_make_admin_record())
        # All three update_item calls were attempted; all swallowed.
        assert table.update_item.call_count == 3


class TestLegacyBedrockCategory:
    def test_returns_bedrock_when_cost_usd_present(self):
        assert _legacy_bedrock_category({"cost_usd": {"S": "0.05"}}) == "bedrock"

    def test_returns_none_when_no_cost_usd(self):
        assert _legacy_bedrock_category({"action": {"S": "X"}}) is None

    def test_returns_none_for_empty_cost_block(self):
        assert _legacy_bedrock_category({"cost_usd": {}}) is None
