"""Unit tests for salesforce-sync Lambda."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    if "salesforce_sync_handler" in sys.modules:
        del sys.modules["salesforce_sync_handler"]
    sn_dir = os.path.join(os.path.dirname(__file__), "../../salesforce-sync")
    if sn_dir not in sys.path:
        sys.path.insert(0, sn_dir)
    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    handler_path = os.path.join(sn_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "salesforce_sync_handler", handler_path,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["salesforce_sync_handler"] = module
    spec.loader.exec_module(module)
    return module


os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("FILE_INGESTION_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/file-ingestion")

handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


@pytest.fixture(autouse=True)
def reset_globals():
    handler_module._dynamodb = None
    handler_module._sqs = None
    handler_module.validate_license = None
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


class TestProcessMessage:
    @patch.object(
        handler_module.salesforce_oauth, "refresh_access_token",
        return_value=("access-tok", "https://acme.my.salesforce.com"),
    )
    @patch("salesforce_sync_handler.iterate_records")
    @patch("salesforce_sync_handler.list_recent_activities", return_value=[])
    @patch("salesforce_sync_handler.latest_modified_date", return_value="2026-06-01T12:00:00Z")
    def test_full_sync_walks_all_object_types(
        self, mock_baseline, mock_activities, mock_iter,
        mock_refresh, mock_table, mock_sqs,
    ):
        mock_iter.side_effect = [
            iter([{"Id": "001A", "Name": "Acme", "SystemModstamp": "2026-06-01T10:00:00Z"}]),
            iter([{"Id": "003C", "Name": "Bob", "SystemModstamp": "2026-06-01T09:00:00Z"}]),
            iter([{"Id": "006O", "Name": "Deal", "SystemModstamp": "2026-06-01T08:00:00Z"}]),
        ]
        mock_table.get_item.return_value = {
            "Item": {"instance_url": "https://acme.my.salesforce.com"}
        }

        result = handler_module.process_sync_message({
            "workspace_id": "ws1",
            "connection_id": "conn-1",
            "cursors": {},
        })

        assert result["status"] == "success"
        assert result["enqueued"] == 3
        assert mock_sqs.send_message.call_count == 3
        # All 3 messages must have FIFO group + dedup id
        for call in mock_sqs.send_message.call_args_list:
            assert call.kwargs["MessageGroupId"] == "ws1-conn-1"
            assert call.kwargs["MessageDeduplicationId"]
        # Activity cursors baselined
        upd = [c for c in mock_table.update_item.call_args_list
               if c.kwargs["ExpressionAttributeValues"].get(":cfg")]
        cursors = upd[-1].kwargs["ExpressionAttributeValues"][":cfg"]["cursors"]
        assert "Task" in cursors
        assert "Event" in cursors

    @patch.object(
        handler_module.salesforce_oauth, "refresh_access_token",
        return_value=("tok", "https://acme.my.salesforce.com"),
    )
    @patch("salesforce_sync_handler.iterate_records", return_value=iter([]))
    @patch("salesforce_sync_handler.changed_activity_parent_ids",
           return_value=(set(), "2026-06-02T10:00:00Z"))
    def test_incremental_sync_walks_activity_changes(
        self, mock_act, mock_iter, mock_refresh, mock_table, mock_sqs,
    ):
        mock_table.get_item.return_value = {
            "Item": {"instance_url": "https://acme.my.salesforce.com"}
        }
        result = handler_module.process_sync_message({
            "workspace_id": "ws1", "connection_id": "conn-1",
            "cursors": {
                "Account": "2026-06-01T00:00:00Z",
                "Contact": "2026-06-01T00:00:00Z",
                "Opportunity": "2026-06-01T00:00:00Z",
                "Task": "2026-06-01T00:00:00Z",
                "Event": "2026-06-01T00:00:00Z",
            },
        })
        assert result["status"] == "success"
        # changed_activity_parent_ids called for both Task and Event
        assert mock_act.call_count == 2

    @patch.object(
        handler_module.salesforce_oauth, "refresh_access_token",
        side_effect=ValueError("re-authenticate"),
    )
    def test_reauth_required_marks_status(self, mock_refresh, mock_table):
        result = handler_module.process_sync_message({
            "workspace_id": "ws1", "connection_id": "conn-1",
        })
        assert result["status"] == "reauth_required"
        # Status flip called
        statuses = [
            c.kwargs["ExpressionAttributeValues"][":s"]
            for c in mock_table.update_item.call_args_list
        ]
        assert "reauth_required" in statuses

    @patch.object(
        handler_module.salesforce_oauth, "refresh_access_token",
        return_value=("tok", "https://acme.my.salesforce.com"),
    )
    def test_passive_region_skips(self, mock_refresh, mock_table, mock_sqs):
        with patch.object(
            handler_module, "guard_active",
            side_effect=handler_module.RegionPassiveError,
        ):
            result = handler_module.process_sync_message({
                "workspace_id": "ws1", "connection_id": "conn-1",
            })
        assert result["status"] == "skipped"
        assert result["reason"] == "passive_region"
        mock_sqs.send_message.assert_not_called()

    @patch.object(
        handler_module.salesforce_oauth, "refresh_access_token",
        return_value=("tok", "https://acme.my.salesforce.com"),
    )
    def test_missing_connection_row(self, mock_refresh, mock_table):
        mock_table.get_item.return_value = {}
        result = handler_module.process_sync_message({
            "workspace_id": "ws1", "connection_id": "conn-1",
        })
        assert result["status"] == "skipped"
        assert result["reason"] == "connection_gone"


class TestUpdateSyncStatusRegression:
    """REMOVE clause on success clears stale last_sync_error."""

    def test_success_uses_remove_for_last_sync_error(self, mock_table):
        handler_module._update_sync_status("ws1", "c1", status="success")
        expr = mock_table.update_item.call_args.kwargs["UpdateExpression"]
        assert "REMOVE #err" in expr

    def test_failed_with_error_sets_last_sync_error(self, mock_table):
        handler_module._update_sync_status("ws1", "c1", status="failed", error="oops")
        expr = mock_table.update_item.call_args.kwargs["UpdateExpression"]
        assert "last_sync_error = :e" in expr
        assert "REMOVE" not in expr


class TestEnqueueIngestionFIFO:
    def test_includes_group_and_dedup(self, mock_sqs):
        handler_module._enqueue_ingestion(
            {
                "source": "salesforce_account",
                "workspace_id": "ws1",
                "connection_id": "conn-1",
                "file": {"record_id": "001XXX"},
            },
            sync_id="sync-abc",
        )
        kwargs = mock_sqs.send_message.call_args.kwargs
        assert kwargs["MessageGroupId"] == "ws1-conn-1"
        assert kwargs["MessageDeduplicationId"] == "sync-abc-salesforce_account-001XXX"


class TestSQSEnvelope:
    def test_success_empty_batch_failures(self, mock_table, mock_sqs):
        with patch.object(handler_module, "process_sync_message", return_value={"status": "success"}):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "w", "connection_id": "c"})}]
            }, None)
        assert resp == {"batchItemFailures": []}

    def test_failed_status_lands_in_batch_item_failures(self, mock_table, mock_sqs):
        with patch.object(handler_module, "process_sync_message", return_value={"status": "failed"}):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "w", "connection_id": "c"})}]
            }, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_exception_lands_in_batch_item_failures(self, mock_table, mock_sqs):
        with patch.object(handler_module, "process_sync_message", side_effect=RuntimeError("boom")):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "w", "connection_id": "c"})}]
            }, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "m1"}]}
