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

    # ADR-019: stub the license layer for default-pass behavior in
    # existing tests. License-specific tests override these per-test.
    monkeypatch.setattr(
        handler_module,
        "validate_license",
        lambda *a, **kw: {"sub": "test-customer", "license": {"tier": "team"}},
    )

    def _allow(*a, **kw):
        result = MagicMock()
        result.allowed = True
        result.reason = None
        result.current = 0
        result.limit = 100
        return result

    monkeypatch.setattr(handler_module, "check_repo_limit", _allow)
    monkeypatch.setattr(handler_module, "track_repo_active", lambda *a, **kw: True)
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

    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_repo_limit_block_skips_with_blocked_status(
        self, _refresh, _load, mock_table, sample_connection, monkeypatch
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}

        def _block(*a, **kw):
            r = MagicMock()
            r.allowed = False
            r.reason = "tier limit"
            r.current = 100
            r.limit = 100
            return r

        monkeypatch.setattr(handler_module, "check_repo_limit", _block)

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "skipped"
        assert result["reason"] == "license_limit"

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
# lambda_handler - SQS dispatch + license-layer regression detection
# ============================================================================


class TestLambdaHandlerLicenseLayer:

    def test_license_layer_absent_emits_audit_and_raises(self, monkeypatch):
        """validate_license=None means the license layer wasn't attached at
        deploy. Per ADR-019, emit a LICENSE_FAIL_OPEN audit row and raise so
        SQS retries / DLQs the message rather than silently dropping it."""
        monkeypatch.setattr(handler_module, "validate_license", None)

        with patch.object(handler_module, "log_license_fail_open") as mock_log:
            with pytest.raises(ImportError):
                handler_module.lambda_handler({"Records": []}, None)
        mock_log.assert_called_once()
        # Lambda name passed as positional arg
        assert mock_log.call_args[0][1] == "onedrive-sync"

    def test_license_validation_failure_raises(self, monkeypatch):
        """validate_license() raising = expired/revoked/etc. Re-raise so SQS retries."""
        def _raise(*a, **kw):
            raise Exception("license expired")
        monkeypatch.setattr(handler_module, "validate_license", _raise)

        with pytest.raises(Exception, match="license expired"):
            handler_module.lambda_handler({"Records": []}, None)

    def test_no_records_returns_400(self):
        result = handler_module.lambda_handler({}, None)
        assert result["statusCode"] == 400


class TestLambdaHandlerDispatch:

    @patch.object(handler_module, "process_sync_request")
    def test_processes_sqs_records(self, mock_process):
        mock_process.return_value = {"status": "success", "files_queued": 5}
        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c1"})},
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c2"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result["statusCode"] == 200
        assert mock_process.call_count == 2

    @patch.object(handler_module, "process_sync_request")
    def test_invalid_json_records_continue(self, mock_process):
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"body": "{not valid json"},
                {"body": json.dumps({"workspace_id": "ws", "connection_id": "c"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        body = json.loads(result["body"])
        # First message bad, second ok
        assert any(r.get("status") == "error" for r in body["results"])
        assert any(r.get("status") == "success" for r in body["results"])
