# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for onenote-sync Lambda handler."""

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


on_api_module = _load_module(
    "on_sync_onenote_api", "onenote_api.py", "../../onenote-sync",
    also_register_as="onenote_api",
)
on_token_module = _load_module(
    "on_sync_token_manager", "token_manager.py", "../../onenote-sync",
    also_register_as="token_manager",
)
handler_module = _load_module(
    "on_sync_handler", "handler.py", "../../onenote-sync"
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
    handler_module.ONENOTE_SYNC_QUEUE = "https://sqs.example.com/on-sync"
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
        "SK": "ONENOTE_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "user_id": "user-graph-1",
        "selected_notebooks": [
            {"notebook_id": "nb_a", "notebook_name": "Personal"}
        ],
        "page_timestamps": {},
        "known_page_ids": [],
        "last_sync_at": None,
    }


# ============================================================================
# sanitize_filename - must match file-ingestion exactly
# ============================================================================


class TestSanitizeFilename:
    """The sanitized form is what ends up as the S3 key filename stem.
    delete-worker's orphan check is a direct string match against
    known_page_ids, so the two implementations MUST agree."""

    def test_strips_unsafe_characters(self):
        assert handler_module.sanitize_filename("a<b>c") == "a_b_c"
        assert handler_module.sanitize_filename('a"b/c\\d|e?f*g:h') == "a_b_c_d_e_f_g_h"

    def test_collapses_whitespace(self):
        assert handler_module.sanitize_filename("hello   world") == "hello_world"

    def test_strips_leading_trailing_dots_underscores(self):
        assert handler_module.sanitize_filename("._abc._") == "abc"

    def test_truncates_to_200(self):
        result = handler_module.sanitize_filename("a" * 500)
        assert len(result) == 200


# ============================================================================
# get_connection / update_connection_status
# ============================================================================


class TestConnectionReadWrite:

    def test_get_connection_returns_item(self, mock_table, sample_connection):
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.get_connection("ws_test", "conn_test")
        assert result == sample_connection
        # SK uses the OneNote prefix (separate from ONEDRIVE_CONNECTION#)
        call_key = mock_table.get_item.call_args[1]["Key"]
        assert call_key["SK"] == "ONENOTE_CONNECTION#conn_test"

    def test_get_connection_not_found(self, mock_table):
        mock_table.get_item.return_value = {}
        assert handler_module.get_connection("ws", "c") is None

    def test_get_connection_dynamodb_error(self, mock_table):
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "GetItem"
        )
        assert handler_module.get_connection("ws", "c") is None

    def test_update_status_includes_page_state(self, mock_table):
        handler_module.update_connection_status(
            "ws", "c", "completed",
            page_count=7,
            page_timestamps={"p1": "2026-05-10T12:00:00Z"},
            known_page_ids=["p1_sanitized"],
        )
        call_args = mock_table.update_item.call_args[1]
        expr = call_args["UpdateExpression"]
        assert "page_count" in expr
        assert "page_timestamps" in expr
        assert "known_page_ids" in expr
        values = call_args["ExpressionAttributeValues"]
        assert values[":ts"] == {"p1": "2026-05-10T12:00:00Z"}
        assert values[":known"] == ["p1_sanitized"]


# ============================================================================
# queue_page_ingestion
# ============================================================================


class TestQueuePageIngestion:

    def test_queue_payload_shape(self):
        page = {
            "id": "page-1",
            "title": "Meeting Notes",
            "lastModifiedDateTime": "2026-05-10T12:00:00Z",
        }
        handler_module.queue_page_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            notebook_id="nb_a",
            notebook_name="Personal",
            section_id="sec_1",
            section_name="Q2",
            page=page,
            html_content="<html><body>content</body></html>",
            sync_id="sync_x",
        )

        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert sent["source"] == "onenote"
        assert sent["workspace_id"] == "ws_test"
        assert sent["connection_id"] == "conn_test"
        f = sent["file"]
        assert f["page_id"] == "page-1"
        assert f["title"] == "Meeting Notes"
        assert f["notebook_id"] == "nb_a"
        assert f["notebook_name"] == "Personal"
        assert f["section_id"] == "sec_1"
        assert f["section_name"] == "Q2"
        assert f["last_modified"] == "2026-05-10T12:00:00Z"
        # Content shipped inline (no separate download step downstream)
        assert "<html>" in f["content_html"]

        # FIFO ordering by workspace+connection
        assert mock_sqs_client.send_message.call_args[1]["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_page_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_page_ingestion(
            workspace_id="ws", connection_id="c",
            notebook_id="nb", notebook_name="N",
            section_id="s", section_name="S",
            page={"id": "p", "title": "t", "lastModifiedDateTime": ""},
            html_content="<html/>",
            sync_id="sx",
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

    def test_no_notebooks_skips(self, mock_table, sample_connection):
        sample_connection["selected_notebooks"] = []
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "skipped"

    def test_missing_user_id_errors(self, mock_table, sample_connection):
        sample_connection["user_id"] = ""
        mock_table.get_item.return_value = {"Item": sample_connection}
        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "error"
        assert "user_id" in result["error"]

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

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_page_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_page_content", return_value="<html><body>content here</body></html>")
    @patch.object(handler_module, "list_pages")
    @patch.object(handler_module, "list_sections")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_full_success_queues_pages_and_emits_audit(
        self, _refresh, _load, mock_sections, mock_pages,
        _content, mock_audit, mock_queue_page, _cleanup,
        mock_table, sample_connection
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_sections.return_value = [{"id": "sec_1", "displayName": "Q2"}]
        mock_pages.return_value = [
            {"id": "p1", "title": "Note A", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
            {"id": "p2", "title": "Note B", "lastModifiedDateTime": "2026-05-09T08:00:00Z"},
        ]

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"
        assert result["pages_queued"] == 2
        assert result["pages_unchanged"] == 0
        assert result["total_pages"] == 2

        # Both pages queued
        assert mock_queue_page.call_count == 2

        # Audit row carries onenote source + per-user notebook count
        audit_kwargs = mock_audit.call_args[1]
        assert audit_kwargs["source"] == "onenote"
        assert audit_kwargs["files_synced"] == 2
        assert audit_kwargs["extra_details"]["user_id"] == "user-graph-1"
        assert audit_kwargs["extra_details"]["notebook_count"] == 1
        assert audit_kwargs["extra_details"]["pages_unchanged"] == 0

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_page_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_page_content")
    @patch.object(handler_module, "list_pages")
    @patch.object(handler_module, "list_sections")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_unchanged_page_skipped(
        self, _refresh, _load, mock_sections, mock_pages,
        mock_content, _audit, mock_queue_page, _cleanup,
        mock_table, sample_connection
    ):
        """Page with matching lastModifiedDateTime in page_timestamps is NOT
        re-fetched and NOT re-queued (change-detection optimization)."""
        sample_connection["page_timestamps"] = {
            "p1": "2026-05-10T12:00:00Z",
        }
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_sections.return_value = [{"id": "sec_1", "displayName": "Q2"}]
        mock_pages.return_value = [
            {"id": "p1", "title": "Unchanged", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
            {"id": "p2", "title": "Changed", "lastModifiedDateTime": "2026-05-11T08:00:00Z"},
        ]
        mock_content.return_value = "<html>changed</html>"

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"
        assert result["pages_queued"] == 1
        assert result["pages_unchanged"] == 1
        # Content only fetched for the changed page
        assert mock_content.call_count == 1
        # Only the changed page queued
        assert mock_queue_page.call_count == 1
        assert mock_queue_page.call_args.kwargs["page"]["id"] == "p2"

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_page_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_page_content", return_value="")
    @patch.object(handler_module, "list_pages")
    @patch.object(handler_module, "list_sections")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_empty_page_content_not_queued(
        self, _refresh, _load, mock_sections, mock_pages,
        _content, _audit, mock_queue_page, _cleanup,
        mock_table, sample_connection
    ):
        """Pages with empty/tiny HTML body are skipped (would produce no
        useful embeddings)."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_sections.return_value = [{"id": "sec_1", "displayName": "Q2"}]
        mock_pages.return_value = [
            {"id": "p1", "title": "Empty", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
        ]

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test"
        })
        assert result["status"] == "success"
        assert result["pages_queued"] == 0
        # Page id still tracked (so its key on S3 won't be orphan-deleted
        # just because content was empty this round)
        assert result["total_pages"] == 1
        mock_queue_page.assert_not_called()


# ============================================================================
# known_page_ids tracking + orphan cleanup
# ============================================================================


class TestKnownPageIdsTracking:
    """known_page_ids is what gets shipped to delete-worker as the
    current set; anything in S3 not in this set is orphan-deleted."""

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_page_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_page_content", return_value="<html>x</html>")
    @patch.object(handler_module, "list_pages")
    @patch.object(handler_module, "list_sections")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_page_ids_sanitized_before_storage(
        self, _refresh, _load, mock_sections, mock_pages,
        _content, _audit, _queue, mock_cleanup,
        mock_table, sample_connection
    ):
        """Page IDs go through sanitize_filename so the form matches
        what file-ingestion writes to S3 (and what delete-worker reads)."""
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_sections.return_value = [{"id": "sec_1", "displayName": "Q2"}]
        # Microsoft Graph page IDs often contain ! and other special chars
        mock_pages.return_value = [
            {"id": "0-abc!def", "title": "P", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
        ]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        # Cleanup called with the SANITIZED form
        _ws, _conn, current_ids = mock_cleanup.call_args[0]
        assert current_ids == [handler_module.sanitize_filename("0-abc!def")]

    @patch.object(handler_module, "queue_orphan_cleanup")
    @patch.object(handler_module, "queue_page_ingestion")
    @patch.object(handler_module, "log_source_sync")
    @patch.object(handler_module, "get_page_content", return_value="<html>x</html>")
    @patch.object(handler_module, "list_pages")
    @patch.object(handler_module, "list_sections")
    @patch.object(handler_module, "_load_oauth_config")
    @patch.object(handler_module, "refresh_access_token", return_value="access")
    def test_known_page_ids_and_timestamps_persisted_to_ddb(
        self, _refresh, _load, mock_sections, mock_pages,
        _content, _audit, _queue, _cleanup,
        mock_table, sample_connection
    ):
        mock_table.get_item.return_value = {"Item": sample_connection}
        mock_sections.return_value = [{"id": "sec_1", "displayName": "Q2"}]
        mock_pages.return_value = [
            {"id": "p1", "title": "x", "lastModifiedDateTime": "2026-05-10T12:00:00Z"},
        ]

        handler_module.process_sync_request({
            "workspace_id": "ws_test", "connection_id": "conn_test",
        })

        update_calls = mock_table.update_item.call_args_list
        for call in update_calls:
            expr_values = call[1].get("ExpressionAttributeValues", {})
            if expr_values.get(":status") == "completed":
                assert ":known" in expr_values
                assert ":ts" in expr_values
                assert expr_values[":ts"] == {"p1": "2026-05-10T12:00:00Z"}
                return
        raise AssertionError("No completed-status update with known_page_ids+page_timestamps")


class TestQueueOrphanCleanup:

    def test_sends_correct_payload(self):
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["pid1", "pid2"])
        sent = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert sent == {
            "action": "delete_orphans",
            "workspace_id": "ws1",
            "source": "onenote",
            "prefix": "conn1/",
            "current_ids": ["pid1", "pid2"],
        }
        assert mock_sqs_client.send_message.call_args[1]["QueueUrl"] == "https://sqs.example.com/delete"

    def test_refuses_empty_current_ids(self):
        """Defensive: empty set would orphan-delete everything for this
        connection. Skip the SQS send."""
        handler_module.queue_orphan_cleanup("ws1", "conn1", [])
        mock_sqs_client.send_message.assert_not_called()

    def test_skips_when_delete_queue_unconfigured(self, monkeypatch):
        monkeypatch.setattr(handler_module, "DELETE_QUEUE", None)
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["pid1"])
        mock_sqs_client.send_message.assert_not_called()

    def test_swallows_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "X", "Message": "boom"}}, "SendMessage"
        )
        # Must not raise
        handler_module.queue_orphan_cleanup("ws1", "conn1", ["pid1"])


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
        mock_process.return_value = {"status": "success", "pages_queued": 5}
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
        """Poison-pill routes to DLQ; sibling success message ack'd."""
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"messageId": "bad", "body": "{not valid json"},
                {"messageId": "good", "body": json.dumps({"workspace_id": "ws", "connection_id": "c"})},
            ]
        }
        result = handler_module.lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "bad"}]}
