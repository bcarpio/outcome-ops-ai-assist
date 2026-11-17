# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for onedrive-sync Lambda (handler + license-layer integration)."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_sqs_client = MagicMock()
mock_ssm_client = MagicMock()


def _load_module(mod_name, filename, directory, also_register_as=None):
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)

    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    if also_register_as:
        sys.modules[also_register_as] = module
    spec.loader.exec_module(module)
    return module


od_api_module = _load_module(
    "od_sync_onedrive_api", "onedrive_api.py", "../../onedrive-sync",
    also_register_as="onedrive_api",
)
od_token_module = _load_module(
    "od_sync_token_manager", "token_manager.py", "../../onedrive-sync",
    also_register_as="token_manager",
)
handler_module = _load_module(
    "od_sync_handler", "handler.py", "../../onedrive-sync"
)
handler_module.dynamodb = mock_dynamodb_resource
handler_module.sqs_client = mock_sqs_client
handler_module.ssm_client = mock_ssm_client


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks(monkeypatch):
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_ssm_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    handler_module._oauth_config = None
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
    handler_module.ONEDRIVE_SYNC_QUEUE = "https://sqs.example.com/od-sync"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    return {
        "PK": "WS#ws_test",
        "SK": "ONEDRIVE_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "drive_id": "drv_xyz",
        "drive_name": "OneDrive",
        "selected_folders": [
            {
                "folder_id": "fldr_a",
                "folder_name": "Documents",
                "folder_path": "/Documents",
            }
        ],
        "delta_tokens": {},
        "last_sync_at": None,
    }


# ============================================================================
# get_connection / update_connection_status
# ============================================================================


class TestConnectionReadWrite:

    def test_get_connection_returns_item(self, mock_table, sample_connection):
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.get_connection("ws_test", "conn_test")
        assert result == sample_connection
        # Confirm the right key shape
        call_key = mock_table.get_item.call_args[1]["Key"]
        assert call_key["SK"] == "ONEDRIVE_CONNECTION#conn_test"

    def test_get_connection_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        assert handler_module.get_connection("ws", "c") is None

    def test_get_connection_dynamodb_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "GetItem"
        )
        assert handler_module.get_connection("ws", "c") is None

    def test_update_status_includes_delta_tokens(self, mock_table):
        handler_module.update_connection_status(
            "ws", "c", "completed",
            file_count=42,
            delta_tokens={"fldr": "deltaUrl"},
        )
        call_args = mock_table.update_item.call_args[1]
        expr = call_args["UpdateExpression"]
        assert "file_count" in expr
        assert "delta_tokens" in expr
        assert call_args["ExpressionAttributeValues"][":delta"] == {"fldr": "deltaUrl"}


# ============================================================================
# queue_file_ingestion
# ============================================================================


class TestQueueFileIngestion:

    def test_queue_file_payload_shape(self):
        item = {
            "id": "item_1",
            "name": "report.pdf",
            "file": {"mimeType": "application/pdf"},
            "size": 12345,
            "parentReference": {"path": "/drive/root:/Documents"},
        }
        handler_module.queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            item=item,
            drive_id="drv_xyz",
            drive_name="OneDrive",
            folder_path="/Documents",
            download_url="https://download.example.com/file",
            sync_id="sync_x",
            file_index=0,
            total_files=0,
        )

        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        # Confirms the source key + downstream-required fields
        assert sent["source"] == "onedrive"
        assert sent["workspace_id"] == "ws_test"
        assert sent["connection_id"] == "conn_test"
        f = sent["file"]
        assert f["item_id"] == "item_1"
        assert f["drive_id"] == "drv_xyz"
        assert f["drive_name"] == "OneDrive"
        assert f["folder_path"] == "/Documents"
        assert f["filename"] == "report.pdf"
        assert f["mime_type"] == "application/pdf"
        assert f["download_url"] == "https://download.example.com/file"

        # FIFO ordering by workspace+connection
        assert mock_sqs_client.send_message.call_args[1]["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_file_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_file_ingestion(
            workspace_id="ws", connection_id="c",
            item={"id": "i", "name": "f", "file": {}},
            drive_id="d", drive_name="D",
            folder_path="/x", download_url="https://x",
            sync_id="s", file_index=0, total_files=0,
        )


# ============================================================================
# process_sync_request - happy paths and gating
# ============================================================================


class TestProcessSyncRequest:

    def test_no_connection_returns_error(self, mock_table):
        mock_table.get_item.return_value = {}
        result = handler_module.process_sync_request({
            "workspace_id": "ws", "connection_id": "c"
        })
        assert result["status"] == "error"

    def test_no_folders_skips(self, mock_table, sample_connection):
        sample_connection["selected_folders"] = []
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "skipped"

    def test_missing_drive_id_errors(self, mock_table, sample_connection):
        sample_connection["drive_id"] = ""
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "error"
        assert "drive_id" in result["error"]

    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value=None)
    def test_token_refresh_failure_marks_auth_error(
        self, _refresh, _load, mock_table, sample_connection
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "error"
        assert "Authentication failed" in result["error"]

    @patch.object(handler_module, "queue_file_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_drive_delta")
    @patch.object(handler_module, "get_folder_delta")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_full_success_queues_files_and_emits_audit(
        self, _refresh, _load, mock_folder_delta, mock_drive_delta,
        mock_audit, mock_queue_file, mock_table, sample_connection
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        # Folder-scoped delta returns 2 files (selected_folders has fldr_a, not "root")
        mock_folder_delta.return_value = (
            [
                {
                    "id": "f1", "name": "report.pdf",
                    "file": {"mimeType": "application/pdf"},
                    "@microsoft.graph.downloadUrl": "https://x/f1",
                },
                {
                    "id": "f2", "name": "spec.docx",
                    "file": {"mimeType": "application/msword"},
                    "@microsoft.graph.downloadUrl": "https://x/f2",
                },
                # A folder - should be filtered out
                {"id": "fldr", "name": "sub", "folder": {}},
                # A deletion - should be filtered out (no file key)
                {"id": "del1", "deleted": {"state": "deleted"}},
            ],
            "https://graph.microsoft.com/v1.0/delta_link",
        )
        mock_drive_delta.return_value = ([], None)  # would be used if folder_id was "root"

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"
        assert result["files_queued"] == 2

        # Both files queued
        assert mock_queue_file.call_count == 2

        # Folder-scoped delta used (not drive delta)
        mock_folder_delta.assert_called_once()
        mock_drive_delta.assert_not_called()

        # Audit row emitted with onedrive source
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["source"] == "onedrive"
        assert audit_kwargs["files_synced"] == 2
        assert audit_kwargs["extra_details"]["drive_id"] == "drv_xyz"

    @patch.object(handler_module, "queue_file_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_drive_delta")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_root_folder_id_uses_drive_delta(
        self, _refresh, _load, mock_drive_delta,
        mock_audit, mock_queue_file, mock_table, sample_connection
    ):
        """When folder_id == 'root', use get_drive_delta (whole-drive) not get_folder_delta."""
        sample_connection["selected_folders"] = [
            {"folder_id": "root", "folder_name": "(root)", "folder_path": "/"}
        ]
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_drive_delta.return_value = (
            [{"id": "f1", "name": "x.pdf", "file": {},
              "@microsoft.graph.downloadUrl": "https://x"}],
            None,
        )

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"
        mock_drive_delta.assert_called_once()


# ============================================================================
# lambda_handler - SQS dispatch
# ============================================================================


class TestLambdaHandlerDispatch:
    """SQS partial-batch-failure contract."""

    def test_no_records_returns_400(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, "process_sync_request")
    def test_processes_sqs_records(self, mock_process):
        mock_process.return_value = {"status": "success", "files_queued": 5}
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps({"workspace_id": "ws", "connection_id": "c1"})},
                {"messageId": "m2", "body": json.dumps({"workspace_id": "ws", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": []}
        assert mock_process.call_count == 2

    @patch.object(handler_module, "process_sync_request")
    def test_invalid_json_records_continue(self, mock_process):
        """Poison-pill in slot 1 lands in batchItemFailures; slot 2 succeeds
        (ack'd). Ensures a bad message never blocks a good one."""
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"messageId": "bad", "body": "{not valid json"},
                {"messageId": "good", "body": json.dumps({"workspace_id": "ws", "connection_id": "c"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}


# ============================================================================
# Orphan cleanup (deletions detected via delta)
# ============================================================================


class TestKnownItemIdsTracking:
    """known_item_ids is the source-of-truth set the sync ships to
    delete-worker as expected_ids. Add on new file, remove on delete
    item in delta response."""

    @patch.object(handler_module, "queue_file_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "get_folder_delta")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_new_files_added_to_known_item_ids(
        self, _refresh, _load, mock_delta, mock_cleanup,
        mock_audit, _queue, mock_table, sample_connection
    ):
        sample_connection["known_item_ids"] = ["existing_1"]
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_delta.return_value = (
            [
                {"id": "new_1", "name": "a.pdf", "file": {},
                 "@microsoft.graph.downloadUrl": "https://x"},
                {"id": "new_2", "name": "b.pdf", "file": {},
                 "@microsoft.graph.downloadUrl": "https://x"},
            ],
            "delta_link",
        )

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        # cleanup queued with the union (existing + new)
        called_with = mock_cleanup.call_args[0]
        ws, conn, current_ids = called_with
        assert ws == "ws_test"
        assert conn == "conn_test"
        assert set(current_ids) == {"existing_1", "new_1", "new_2"}

    @patch.object(handler_module, "queue_file_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "get_folder_delta")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_deleted_items_removed_from_known_item_ids(
        self, _refresh, _load, mock_delta, mock_cleanup,
        mock_audit, _queue, mock_table, sample_connection
    ):
        """Delta reports `deleted: {state: deleted}` for files removed
        in OneDrive. Those item_ids must drop out of known_item_ids so
        the orphan cleanup picks them up."""
        sample_connection["known_item_ids"] = ["keep_me", "delete_me", "also_delete"]
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_delta.return_value = (
            [
                {"id": "delete_me", "deleted": {"state": "deleted"}},
                {"id": "also_delete", "@removed": {"reason": "deleted"}},
                {"id": "new_one", "name": "x.pdf", "file": {},
                 "@microsoft.graph.downloadUrl": "https://x"},
            ],
            "delta_link",
        )

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        _ws, _conn, current_ids = mock_cleanup.call_args[0]
        assert "keep_me" in current_ids
        assert "new_one" in current_ids
        assert "delete_me" not in current_ids
        assert "also_delete" not in current_ids

    @patch.object(handler_module, "queue_file_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "get_folder_delta")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_known_item_ids_persisted_to_ddb(
        self, _refresh, _load, mock_delta, _cleanup,
        _audit, _queue, mock_table, sample_connection
    ):
        sample_connection["known_item_ids"] = []
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_delta.return_value = (
            [{"id": "f1", "name": "a", "file": {},
              "@microsoft.graph.downloadUrl": "https://x"}],
            "delta_link",
        )

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        # update_item should have been called with known_item_ids set
        update_calls = mock_table.update_item.call_args_list
        # find the "completed" status update
        for call in update_calls:
            expr_values = call[1].get("ExpressionAttributeValues", {})
            if expr_values.get(":status") == "completed":
                assert ":known" in expr_values
                assert "f1" in expr_values[":known"]
                return
        raise AssertionError("No completed-status update with known_item_ids")


class TestQueueOrphanCleanup:

    def test_sends_correct_payload(self):
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["id1", "id2"])
        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_orphans",
            "workspace_id": "ws1",
            "source": "onedrive",
            "prefix": "conn1/",
            "current_ids": ["id1", "id2"],
        }
        assert mock_sqs_client.send_message.call_args[1]["QueueUrl"] == "https://sqs.example.com/delete"

    def test_refuses_empty_current_ids(self):
        """Defensive: empty set would orphan-delete every file. Skip."""
        handler_module.queue_orphan_cleanup("ws1", "conn1", [])
        mock_sqs_client.send_message.assert_not_called()

    def test_skips_when_delete_queue_unconfigured(self, monkeypatch):
        monkeypatch.setattr(handler_module, "DELETE_QUEUE", None)
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["id1"])
        mock_sqs_client.send_message.assert_not_called()

    def test_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["id1"])
