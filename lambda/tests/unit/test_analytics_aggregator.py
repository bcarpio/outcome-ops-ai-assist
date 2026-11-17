# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the analytics-aggregator Lambda."""

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
    # Seed env var so module-level constant is set to a non-empty string.
    os.environ["ANALYTICS_TABLE"] = "test-analytics"
    spec.loader.exec_module(module)
    return module


handler_module = _load_handler_module()
handler = handler_module.handler
_process_record = handler_module._process_record
_apply_update = handler_module._apply_update
_date_from_timestamp = handler_module._date_from_timestamp
_cost = handler_module._cost
_n = handler_module._n
_s = handler_module._s


def _make_record(
    workspace_id="ws-1",
    user_email="alice@example.com",
    model_id="us.anthropic.sonnet",
    cost="0.10",
    input_tokens=100,
    output_tokens=50,
    duration_ms=500,
    status="success",
    timestamp="2026-04-10T12:00:00+00:00",
    request_id="req-abc",
    event_name="INSERT",
):
    return {
        "eventName": event_name,
        "dynamodb": {
            "NewImage": {
                "workspace_id": {"S": workspace_id},
                "user_email": {"S": user_email},
                "model_id": {"S": model_id},
                "cost_usd": {"S": cost},
                "input_tokens": {"N": str(input_tokens)},
                "output_tokens": {"N": str(output_tokens)},
                "duration_ms": {"N": str(duration_ms)},
                "status": {"S": status},
                "timestamp": {"S": timestamp},
                "request_id": {"S": request_id},
            }
        },
    }


class TestAccessors:
    def test_s_returns_string(self):
        assert _s({"k": {"S": "v"}}, "k") == "v"

    def test_s_returns_default_when_missing(self):
        assert _s({}, "k", default="fallback") == "fallback"

    def test_n_parses_number(self):
        assert _n({"k": {"N": "42"}}, "k") == 42

    def test_n_returns_default_when_missing(self):
        assert _n({}, "k", default=7) == 7

    def test_cost_parses_string_cost(self):
        assert _cost({"cost_usd": {"S": "0.42"}}) == Decimal("0.42")

    def test_cost_handles_number_form(self):
        assert _cost({"cost_usd": {"N": "0.50"}}) == Decimal("0.50")

    def test_cost_missing_returns_zero(self):
        assert _cost({}) == Decimal("0")

    def test_date_from_timestamp(self):
        assert _date_from_timestamp("2026-04-10T12:00:00+00:00") == "2026-04-10"

    def test_date_from_timestamp_empty(self):
        assert _date_from_timestamp("") is None


class TestProcessRecord:
    @patch.object(handler_module, "_get_table")
    def test_updates_three_rollups(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record())

        # DT, DM, DU — three UpdateItem calls
        assert table.update_item.call_count == 3
        sks = [call.kwargs["Key"]["SK"] for call in table.update_item.call_args_list]
        assert sks[0] == "DT#2026-04-10"
        assert sks[1] == "DM#2026-04-10#us.anthropic.sonnet"
        assert sks[2] == "DU#2026-04-10#alice@example.com"

    @patch.object(handler_module, "_get_table")
    def test_skips_non_insert(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(event_name="MODIFY"))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_skips_system_workspace(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(workspace_id="SYSTEM"))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_skips_missing_workspace(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(workspace_id=""))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_skips_missing_request_id(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(request_id=""))
        assert table.update_item.call_count == 0

    @patch.object(handler_module, "_get_table")
    def test_skips_user_rollup_when_user_empty(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(user_email=""))
        # Only DT and DM — no DU because no user
        assert table.update_item.call_count == 2
        sks = [call.kwargs["Key"]["SK"] for call in table.update_item.call_args_list]
        assert any(sk.startswith("DT#") for sk in sks)
        assert any(sk.startswith("DM#") for sk in sks)
        assert not any(sk.startswith("DU#") for sk in sks)

    @patch.object(handler_module, "_get_table")
    def test_refusal_status_increments_refusal_count(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(status="refusal"))
        dt_call = table.update_item.call_args_list[0]
        vals = dt_call.kwargs["ExpressionAttributeValues"]
        assert vals[":ref"] == 1
        assert vals[":err"] == 0

    @patch.object(handler_module, "_get_table")
    def test_error_status_increments_error_count(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        _process_record(_make_record(status="error"))
        dt_call = table.update_item.call_args_list[0]
        vals = dt_call.kwargs["ExpressionAttributeValues"]
        assert vals[":err"] == 1
        assert vals[":ref"] == 0

    @patch.object(handler_module, "_get_table")
    def test_duplicate_request_id_is_skipped(self, mock_get_table):
        table = MagicMock()
        # Condition fails for DT only; DM and DU succeed.
        table.update_item.side_effect = [
            ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException"}},
                "UpdateItem",
            ),
            None,
            None,
        ]
        mock_get_table.return_value = table

        # Should not raise despite the conditional-check failure
        _process_record(_make_record())
        assert table.update_item.call_count == 3

    @patch.object(handler_module, "_get_table")
    def test_unknown_model_id_defaults(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        rec = _make_record()
        # Drop model_id from image
        del rec["dynamodb"]["NewImage"]["model_id"]
        _process_record(rec)

        dm_call = table.update_item.call_args_list[1]
        assert dm_call.kwargs["Key"]["SK"] == "DM#2026-04-10#unknown"


class TestAggregateItem:
    """Shared helper used by both the stream consumer and the backfill script."""

    def test_aggregates_three_rollups(self):
        table = MagicMock()
        ok = handler_module.aggregate_item(
            table=table,
            workspace_id="ws-1",
            user_email="alice@x",
            model_id="sonnet",
            cost=Decimal("0.10"),
            input_tokens=100,
            output_tokens=50,
            duration_ms=500,
            status="success",
            timestamp="2026-04-10T12:00:00+00:00",
            request_id="req-1",
        )
        assert ok is True
        sks = [c.kwargs["Key"]["SK"] for c in table.update_item.call_args_list]
        assert sks == ["DT#2026-04-10", "DM#2026-04-10#sonnet", "DU#2026-04-10#alice@x"]

    def test_returns_false_for_missing_workspace(self):
        table = MagicMock()
        ok = handler_module.aggregate_item(
            table=table, workspace_id="", user_email="a@x", model_id="m",
            cost=Decimal("0"), input_tokens=0, output_tokens=0,
            duration_ms=0, status="success",
            timestamp="2026-04-10T12:00:00+00:00", request_id="req",
        )
        assert ok is False
        assert table.update_item.call_count == 0

    def test_returns_false_for_system_workspace(self):
        table = MagicMock()
        ok = handler_module.aggregate_item(
            table=table, workspace_id="SYSTEM", user_email="a@x", model_id="m",
            cost=Decimal("0"), input_tokens=0, output_tokens=0,
            duration_ms=0, status="success",
            timestamp="2026-04-10T12:00:00+00:00", request_id="req",
        )
        assert ok is False

    def test_returns_false_for_missing_request_id(self):
        table = MagicMock()
        ok = handler_module.aggregate_item(
            table=table, workspace_id="ws-1", user_email="a@x", model_id="m",
            cost=Decimal("0"), input_tokens=0, output_tokens=0,
            duration_ms=0, status="success",
            timestamp="2026-04-10T12:00:00+00:00", request_id="",
        )
        assert ok is False

    def test_lowercases_user_email_in_sk(self):
        table = MagicMock()
        handler_module.aggregate_item(
            table=table, workspace_id="ws-1", user_email="MIX@Example.COM",
            model_id="m", cost=Decimal("0"), input_tokens=0, output_tokens=0,
            duration_ms=0, status="success",
            timestamp="2026-04-10T12:00:00+00:00", request_id="req",
        )
        du_sk = table.update_item.call_args_list[2].kwargs["Key"]["SK"]
        assert du_sk == "DU#2026-04-10#mix@example.com"


class TestHandler:
    """Chunk 10: handler returns DDB-Streams partial-batch-failure envelope
    so a poison record is retried instead of silently dropped on 200."""

    @patch.object(handler_module, "_get_table")
    def test_handler_processes_batch(self, mock_get_table):
        table = MagicMock()
        mock_get_table.return_value = table

        event = {
            "Records": [
                _make_record(request_id="r1"),
                _make_record(request_id="r2"),
            ]
        }
        result = handler(event, None)
        assert result == {"batchItemFailures": []}
        # 2 records x 3 rollups each = 6 update_item calls
        assert table.update_item.call_count == 6

    @patch.object(handler_module, "_get_table")
    def test_handler_continues_on_record_failure(self, mock_get_table):
        """First record's first update raises -> record 1 lands in
        batchItemFailures with its eventID; record 2 processes normally."""
        table = MagicMock()
        table.update_item.side_effect = [
            RuntimeError("boom"),
            None, None, None,
        ]
        mock_get_table.return_value = table

        r1 = _make_record(request_id="r1")
        r1["eventID"] = "evt-1"
        r2 = _make_record(request_id="r2")
        r2["eventID"] = "evt-2"
        event = {"Records": [r1, r2]}
        result = handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "evt-1"}]}

    def test_handler_without_table_env(self):
        with patch.object(handler_module, "ANALYTICS_TABLE", ""):
            result = handler({"Records": []}, None)
            assert result == {"batchItemFailures": []}
