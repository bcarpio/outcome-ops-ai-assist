"""Unit tests for salesforce-scheduler Lambda."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    if "salesforce_scheduler_handler" in sys.modules:
        del sys.modules["salesforce_scheduler_handler"]
    sn_dir = os.path.join(os.path.dirname(__file__), "../../salesforce-scheduler")
    if sn_dir not in sys.path:
        sys.path.insert(0, sn_dir)
    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    handler_path = os.path.join(sn_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "salesforce_scheduler_handler", handler_path,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["salesforce_scheduler_handler"] = module
    spec.loader.exec_module(module)
    return module


os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("SALESFORCE_SYNC_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/sf-sync")

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


def _row(workspace_id, connection_id, status="success", cursors=None):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"SALESFORCE_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "last_sync_status": status,
        "config": {"cursors": cursors} if cursors is not None else {},
    }


class TestScheduler:
    def test_active_region_enqueues_healthy_connections(self, mock_table, mock_sqs):
        mock_table.scan.return_value = {
            "Items": [
                _row("ws1", "c1", cursors={"Account": "2026-06-01T12:00:00Z"}),
                _row("ws2", "c2"),
            ]
        }
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result == {"enqueued": 2, "skipped": 0}
        assert mock_sqs.send_message.call_count == 2
        first_body = json.loads(mock_sqs.send_message.call_args_list[0].kwargs["MessageBody"])
        assert first_body["workspace_id"] == "ws1"
        assert first_body["connection_id"] == "c1"
        assert first_body["cursors"] == {"Account": "2026-06-01T12:00:00Z"}
        assert first_body["triggered_by"] == "scheduler"

    def test_passive_region_short_circuits(self, mock_table, mock_sqs):
        with patch.object(handler_module, "is_active_region", return_value=False):
            result = lambda_handler({}, None)
        assert result["enqueued"] == 0
        mock_table.scan.assert_not_called()
        mock_sqs.send_message.assert_not_called()

    def test_skips_reauth_required(self, mock_table, mock_sqs):
        mock_table.scan.return_value = {
            "Items": [
                _row("ws1", "c1", status="reauth_required"),
                _row("ws1", "c2", status="success"),
            ]
        }
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result == {"enqueued": 1, "skipped": 1}
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["connection_id"] == "c2"

    def test_skips_in_progress_and_needs_oauth(self, mock_table, mock_sqs):
        mock_table.scan.return_value = {
            "Items": [
                _row("ws1", "c1", status="in_progress"),
                _row("ws1", "c2", status="needs_oauth"),
                _row("ws1", "c3", status="success"),
            ]
        }
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result == {"enqueued": 1, "skipped": 2}

    def test_paginates_scan(self, mock_table, mock_sqs):
        mock_table.scan.side_effect = [
            {
                "Items": [_row("ws1", "c1"), _row("ws2", "c2")],
                "LastEvaluatedKey": {"PK": "WS#ws2"},
            },
            {"Items": [_row("ws3", "c3")]},
        ]
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result == {"enqueued": 3, "skipped": 0}
        assert mock_table.scan.call_count == 2
        assert mock_sqs.send_message.call_count == 3

    def test_malformed_row_skipped(self, mock_table, mock_sqs):
        mock_table.scan.return_value = {
            "Items": [
                {"PK": "WS#ws1", "SK": "SALESFORCE_CONNECTION#c1"},  # no connection_id
                _row("ws1", "c2"),
            ]
        }
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result["enqueued"] == 1
        assert result["skipped"] == 1

    def test_zero_connections(self, mock_table, mock_sqs):
        mock_table.scan.return_value = {"Items": []}
        with patch.object(handler_module, "is_active_region", return_value=True):
            result = lambda_handler({}, None)
        assert result == {"enqueued": 0, "skipped": 0}
        mock_sqs.send_message.assert_not_called()
