"""Unit tests for servicenow-sync Lambda (chunk 03)."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


def _load_handler_module():
    if "servicenow_sync_handler" in sys.modules:
        del sys.modules["servicenow_sync_handler"]
    sn_dir = os.path.join(os.path.dirname(__file__), "../../servicenow-sync")
    if sn_dir not in sys.path:
        sys.path.insert(0, sn_dir)
    lambda_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    handler_path = os.path.join(sn_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("servicenow_sync_handler", handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["servicenow_sync_handler"] = module
    spec.loader.exec_module(module)
    return module


os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("FILE_INGESTION_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/file-ingestion")
os.environ.setdefault("DELETE_QUEUE", "https://sqs.us-west-2.amazonaws.com/123/delete")
os.environ.setdefault("KB_BUCKET", "test-kb-bucket")

handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


@pytest.fixture(autouse=True)
def reset_globals():
    handler_module._dynamodb = None
    handler_module._sqs = None
    handler_module._s3 = None
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


@pytest.fixture
def mock_s3():
    client = MagicMock()
    client.list_objects_v2.return_value = {"Contents": [], "IsTruncated": False}
    handler_module._s3 = client
    return client


def _connection_row(cursor=None):
    return {
        "instance_url": "https://acme.service-now.com",
        "config": {"cursor": cursor},
    }


class TestProcessMessage:
    @patch.object(handler_module.servicenow_oauth, "refresh_access_token", return_value="access-token")
    @patch.object(handler_module, "list_articles", return_value=([
        {"sys_id": "a1", "number": "KB0001", "short_description": "hi",
         "sys_updated_on": "2026-06-01 12:00:00"},
        {"sys_id": "a2", "number": "KB0002", "short_description": "there",
         "sys_updated_on": "2026-06-02 12:00:00"},
    ], False))
    @patch.object(handler_module, "list_attachments_for_article", return_value=[
        {"sys_id": "att1", "file_name": "notes.pdf"},
        {"sys_id": "att2", "file_name": "unlist.exe"},  # not in allowlist
    ])
    def test_happy_path_enqueues_and_advances_cursor(
        self, mock_atts, mock_articles, mock_refresh,
        mock_table, mock_sqs, mock_s3,
    ):
        mock_table.get_item.return_value = {"Item": _connection_row(cursor=None)}

        result = handler_module.process_sync_message({
            "workspace_id": "ws1",
            "connection_id": "conn-1",
            "cursor": "",
        })

        assert result["status"] == "success"
        assert result["articles"] == 2
        # 2 articles x 1 allowed attachment each = 2 attachments
        assert result["attachments"] == 2
        # Verify file-ingestion enqueue shape (first message = article a1)
        first_call = mock_sqs.send_message.call_args_list[0].kwargs
        body = json.loads(first_call["MessageBody"])
        assert body["source"] == "servicenow_article"
        assert body["file"]["article_sys_id"] == "a1"

        # Cursor advanced to the max sys_updated_on
        upd_calls = [
            c for c in mock_table.update_item.call_args_list
            if c.kwargs["Key"]["SK"].startswith("SERVICENOW_CONNECTION#")
        ]
        # Last update should be the terminal success with cursor advance
        final = upd_calls[-1].kwargs["ExpressionAttributeValues"]
        assert final[":cfg"]["cursor"] == "2026-06-02 12:00:00"
        assert final[":s"] == "success"

    @patch.object(handler_module.servicenow_oauth, "refresh_access_token", side_effect=ValueError("Token expired; user must re-authenticate"))
    def test_invalid_grant_flips_reauth_and_returns_success(
        self, mock_refresh, mock_table, mock_sqs, mock_s3,
    ):
        # invalid_grant path shouldn't retry -- return "reauth_required" so
        # SQS ack's and doesn't clog the queue.
        result = handler_module.process_sync_message({
            "workspace_id": "ws1",
            "connection_id": "conn-1",
        })
        assert result["status"] == "reauth_required"
        # Connection row got a reauth_required status flip.
        upd_calls = [
            c for c in mock_table.update_item.call_args_list
            if c.kwargs["Key"]["SK"].startswith("SERVICENOW_CONNECTION#")
        ]
        assert any(
            c.kwargs["ExpressionAttributeValues"][":s"] == "reauth_required"
            for c in upd_calls
        )
        mock_sqs.send_message.assert_not_called()

    @patch.object(handler_module.servicenow_oauth, "refresh_access_token", return_value="tok")
    @patch.object(handler_module, "list_articles", side_effect=handler_module.ServiceNowRateLimitedError("kb_knowledge"))
    def test_rate_limited_marks_partial_success(
        self, mock_articles, mock_refresh,
        mock_table, mock_sqs, mock_s3,
    ):
        mock_table.get_item.return_value = {"Item": _connection_row()}
        result = handler_module.process_sync_message({
            "workspace_id": "ws1",
            "connection_id": "conn-1",
        })
        # No articles synced + failure -> failed
        assert result["status"] == "failed"

    @patch.object(handler_module.servicenow_oauth, "refresh_access_token", return_value="tok")
    @patch.object(handler_module, "list_articles", return_value=([
        {"sys_id": "a1", "sys_updated_on": "2026-06-01"},
    ], False))
    @patch.object(handler_module, "list_attachments_for_article", return_value=[])
    def test_orphan_cleanup_enqueues_deletes(
        self, mock_atts, mock_articles, mock_refresh,
        mock_table, mock_sqs, mock_s3,
    ):
        mock_table.get_item.return_value = {"Item": _connection_row()}
        # S3 has 3 articles already; enumeration returned 1 (a1).
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "workspaces/ws1/servicenow/conn-1/a1.html"},
                {"Key": "workspaces/ws1/servicenow/conn-1/orphan-1.html"},
                {"Key": "workspaces/ws1/servicenow/conn-1/orphan-2.html"},
            ],
            "IsTruncated": False,
        }

        result = handler_module.process_sync_message({
            "workspace_id": "ws1",
            "connection_id": "conn-1",
        })
        assert result["orphaned"] == 2

        delete_messages = [
            json.loads(c.kwargs["MessageBody"])
            for c in mock_sqs.send_message.call_args_list
            if c.kwargs["QueueUrl"].endswith("/delete")
        ]
        deleted_ids = {m["article_sys_id"] for m in delete_messages}
        assert deleted_ids == {"orphan-1", "orphan-2"}

    def test_connection_disappeared_returns_skipped(self, mock_table, mock_sqs, mock_s3):
        with patch.object(handler_module.servicenow_oauth, "refresh_access_token", return_value="tok"):
            mock_table.get_item.return_value = {}
            result = handler_module.process_sync_message({
                "workspace_id": "ws1",
                "connection_id": "gone",
            })
        assert result["status"] == "skipped"
        assert result["reason"] == "connection_gone"

    def test_passive_region_skips(self, mock_table, mock_sqs):
        # guard_active is imported into the sync handler's namespace at load
        # time; patch it there, not at the shared module path.
        with patch.object(handler_module, "guard_active", side_effect=handler_module.RegionPassiveError):
            result = handler_module.process_sync_message({
                "workspace_id": "ws1",
                "connection_id": "conn-1",
            })
        assert result["status"] == "skipped"
        assert result["reason"] == "passive_region"
        mock_sqs.send_message.assert_not_called()


class TestSQSEnvelope:
    def test_success_returns_empty_batch_item_failures(self, mock_table, mock_sqs, mock_s3):
        with patch.object(handler_module, "process_sync_message", return_value={"status": "success"}):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}]
            }, None)
        assert resp == {"batchItemFailures": []}

    def test_failed_status_lands_in_batch_item_failures(self, mock_table, mock_sqs, mock_s3):
        with patch.object(handler_module, "process_sync_message", return_value={"status": "failed"}):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}]
            }, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_only_failing_record_in_batch(self, mock_table, mock_sqs, mock_s3):
        def side_effect(msg):
            return {"status": "success"} if msg["connection_id"] == "ok" else {"status": "failed"}
        with patch.object(handler_module, "process_sync_message", side_effect=side_effect):
            resp = lambda_handler({
                "Records": [
                    {"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "ok"})},
                    {"messageId": "m2", "body": json.dumps({"workspace_id": "ws1", "connection_id": "bad"})},
                    {"messageId": "m3", "body": json.dumps({"workspace_id": "ws1", "connection_id": "ok"})},
                ]
            }, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "m2"}]}

    def test_exception_lands_in_batch_item_failures(self, mock_table, mock_sqs, mock_s3):
        with patch.object(handler_module, "process_sync_message", side_effect=RuntimeError("boom")):
            resp = lambda_handler({
                "Records": [{"messageId": "m1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})}]
            }, None)
        assert resp == {"batchItemFailures": [{"itemIdentifier": "m1"}]}

    def test_empty_records(self, mock_table, mock_sqs, mock_s3):
        resp = lambda_handler({"Records": []}, None)
        assert resp == {"batchItemFailures": []}
