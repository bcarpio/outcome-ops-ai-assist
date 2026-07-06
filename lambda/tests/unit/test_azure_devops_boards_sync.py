"""Unit tests for the azure-devops-boards-sync Lambda.

The worker is an orchestrator: it reads a project sync message from
SQS, refreshes an OAuth token, runs WIQL, batch-fetches work items,
and enqueues each to file-ingestion. Tests focus on:

- Initial-sync watermark fallback (N days ago)
- Incremental sync using the stored watermark
- Batch boundary at 200 IDs
- Watermark advances to max ChangedDate across the sync
- Token-refresh failure marks project auth_error with
  ``reconnect_required``
- Empty result (no new work items) leaves watermark unchanged
- ``initial_sync_max_ids`` cap enforced
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading -- pre-mock boto3 module-level clients
# ---------------------------------------------------------------------------

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


# Env vars for handler module-load.
_HANDLER_LOAD_ENV = {
    "AZURE_DEVOPS_TENANT_ID": "example-tenant-uuid",
    "AZURE_DEVOPS_CLIENT_ID": "client-uuid",
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM": "/dev/oo/azure-devops/client-secret",
    "WORKSPACES_TABLE": "test-workspaces",
    "FILE_INGESTION_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/file-ingestion",
    "AUDIT_TABLE": "test-audit",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

api_module = _load_module(
    "azdo_boards_sync_api",
    "azure_devops_work_items_api.py",
    "../../azure-devops-boards-sync",
    also_register_as="azure_devops_work_items_api",
)
token_module = _load_module(
    "azdo_boards_sync_token",
    "token_manager.py",
    "../../azure-devops-boards-sync",
    also_register_as="token_manager",
)
handler = _load_module(
    "azdo_boards_sync_handler",
    "handler.py",
    "../../azure-devops-boards-sync",
)
handler.dynamodb = mock_dynamodb_resource
handler.sqs_client = mock_sqs_client
handler.ssm_client = mock_ssm_client

for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mocks(monkeypatch):
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_ssm_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    handler._client_secret = None

    monkeypatch.setattr(
        handler, "validate_license",
        lambda *a, **kw: {"sub": "test-customer"},
    )

    def _allow(*a, **kw):
        r = MagicMock()
        r.allowed = True
        r.reason = None
        r.current = 0
        r.limit = 100
        return r

    monkeypatch.setattr(handler, "check_repo_limit", _allow)
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    table.get_item.return_value = {"Item": {}}
    mock_dynamodb_resource.Table.return_value = table
    return table


def _project_row(watermark=None):
    row = {
        "PK": "WS#ws1",
        "SK": "AZDO_BOARDS_PROJECT#example-org/widgets",
        "workspace_id": "ws1",
        "org": "example-org",
        "project": "widgets",
        "slug": "example-org/widgets",
        "connection_id": "c1",
        "enabled": True,
        "sync_status": "pending",
    }
    if watermark is not None:
        row["last_changed_watermark"] = watermark
    return row


def _sync_message():
    return {
        "workspace_id": "ws1",
        "connection_id": "c1",
        "slug": "example-org/widgets",
        "org": "example-org",
        "project": "widgets",
        "triggered_by": "manual",
        "sync_id": "test-sync-1",
    }


def _make_wi(id_, changed_date="2026-07-04T12:00:00Z"):
    return {
        "id": id_,
        "url": f"https://dev.azure.com/example-org/widgets/_apis/wit/workitems/{id_}",
        "fields": {
            "System.Id": id_,
            "System.Title": f"Work Item {id_}",
            "System.State": "Active",
            "System.WorkItemType": "Bug",
            "System.ChangedDate": changed_date,
            "System.Description": "<p>Some text</p>",
        },
    }


# ---------------------------------------------------------------------------
# WIQL / watermark behavior
# ---------------------------------------------------------------------------


class TestInitialSync:
    """Watermark null -> use N-days-ago fallback."""

    def test_initial_watermark_is_lookback_days_ago(self):
        wm = handler._initial_watermark()
        assert wm.endswith("Z")
        assert "T" in wm  # ISO shape

    def test_null_watermark_triggers_initial_wiql(self, mock_table):
        mock_table.get_item.return_value = {"Item": _project_row(watermark=None)}
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[]) as mock_wiql, \
             patch.object(handler, "log_source_sync"):
            result = handler.process_sync_request(_sync_message())

        assert result["status"] == "success"
        # WIQL was called with the initial-lookback watermark, not null.
        wiql_query = mock_wiql.call_args.args[3]
        assert "System.ChangedDate" in wiql_query
        assert "[System.State] NOT IN" in wiql_query


class TestIncrementalSync:

    def test_stored_watermark_used_when_present(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[]) as mock_wiql, \
             patch.object(handler, "log_source_sync"):
            handler.process_sync_request(_sync_message())

        wiql_query = mock_wiql.call_args.args[3]
        # Watermark stripped to YYYY-MM-DD for ADO's WIQL date-precision
        # requirement; operator is >= (inclusive) so items later in the
        # watermark day still come along.
        assert "[System.ChangedDate] >= '2026-07-01'" in wiql_query

    def test_empty_result_returns_success_and_advances_no_watermark(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[]), \
             patch.object(handler, "log_source_sync"), \
             patch.object(handler.dual_write, "ddb_update_item") as mock_update:
            result = handler.process_sync_request(_sync_message())

        assert result["queued"] == 0
        # Two update_items: one for in_progress at start, one for success
        # at end. The success update carries watermark=None (unchanged).
        success_calls = [
            c for c in mock_update.call_args_list
            if ":status" in c.kwargs.get("ExpressionAttributeValues", {})
            and c.kwargs["ExpressionAttributeValues"][":status"] == "success"
        ]
        assert len(success_calls) == 1
        # No :wm key in the success expression when there were no changes.
        assert ":wm" not in success_calls[0].kwargs["ExpressionAttributeValues"]


# ---------------------------------------------------------------------------
# Batching + watermark advance
# ---------------------------------------------------------------------------


class TestBatching:

    def test_ids_split_at_200_boundary(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        ids = list(range(1, 251))  # 250 ids
        wis_batch_1 = [_make_wi(i, "2026-07-02T00:00:00Z") for i in ids[:200]]
        wis_batch_2 = [_make_wi(i, "2026-07-03T00:00:00Z") for i in ids[200:]]

        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=ids), \
             patch.object(
                 handler, "get_work_items",
                 side_effect=[wis_batch_1, wis_batch_2],
             ) as mock_get, \
             patch.object(handler, "get_work_item_comments", return_value=[]), \
             patch.object(handler, "log_source_sync"):
            result = handler.process_sync_request(_sync_message())

        assert result["queued"] == 250
        assert mock_get.call_count == 2
        # First batch capped at 200 IDs.
        first_batch = mock_get.call_args_list[0].args[2]
        assert len(first_batch) == 200

    def test_watermark_advances_to_max_changed_date(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        # 3 IDs, ChangedDates spread across a week.
        wis = [
            _make_wi(1, "2026-07-02T00:00:00Z"),
            _make_wi(2, "2026-07-05T12:34:56Z"),  # max
            _make_wi(3, "2026-07-03T00:00:00Z"),
        ]
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[1, 2, 3]), \
             patch.object(handler, "get_work_items", return_value=wis), \
             patch.object(handler, "get_work_item_comments", return_value=[]), \
             patch.object(handler, "log_source_sync"), \
             patch.object(handler.dual_write, "ddb_update_item") as mock_update:
            handler.process_sync_request(_sync_message())

        success_calls = [
            c for c in mock_update.call_args_list
            if ":status" in c.kwargs.get("ExpressionAttributeValues", {})
            and c.kwargs["ExpressionAttributeValues"][":status"] == "success"
        ]
        assert success_calls[0].kwargs["ExpressionAttributeValues"][":wm"] == (
            "2026-07-05T12:34:56Z"
        )


class TestInitialSyncCap:

    def test_id_list_truncated_to_initial_sync_max_ids(self, mock_table, monkeypatch):
        # Force the cap down for a fast test.
        monkeypatch.setattr(handler, "INITIAL_SYNC_MAX_IDS", 3)

        mock_table.get_item.return_value = {"Item": _project_row(watermark=None)}
        ids = list(range(1, 11))  # 10 ids > 3 cap
        wis = [_make_wi(i) for i in ids[:3]]

        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=ids), \
             patch.object(handler, "get_work_items", return_value=wis) as mock_get, \
             patch.object(handler, "get_work_item_comments", return_value=[]), \
             patch.object(handler, "log_source_sync"):
            result = handler.process_sync_request(_sync_message())

        # Only 3 IDs made it into the fetch batch.
        assert result["queued"] == 3
        assert mock_get.call_args.args[2] == [1, 2, 3]


# ---------------------------------------------------------------------------
# Enqueue payload
# ---------------------------------------------------------------------------


class TestFileIngestionEnqueue:

    def test_message_shape_has_source_and_deterministic_dedup_id(self, mock_table):
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        wis = [_make_wi(42)]

        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[42]), \
             patch.object(handler, "get_work_items", return_value=wis), \
             patch.object(
                 handler, "get_work_item_comments",
                 return_value=[{"text": "hi", "createdBy": {"displayName": "A"}}],
             ), \
             patch.object(handler, "log_source_sync"):
            handler.process_sync_request(_sync_message())

        call = mock_sqs_client.send_message.call_args
        body = json.loads(call.kwargs["MessageBody"])
        assert body["source"] == "azure_devops_boards"
        assert body["file"]["id"] == 42
        assert body["file"]["title"] == "Work Item 42"
        assert body["file"]["org"] == "example-org"
        assert body["file"]["project"] == "widgets"
        assert body["file"]["comments"][0]["text"] == "hi"
        # Dedup id derives from sync_id + work-item id -- deterministic.
        assert call.kwargs["MessageDeduplicationId"] == "test-sync-1-wi-42"
        assert call.kwargs["MessageGroupId"] == "ws1-c1"

    def test_comments_fetch_failure_still_enqueues_work_item(self, mock_table):
        """Comments API is best-effort -- a failure should not kill
        the sync of the work item itself."""
        mock_table.get_item.return_value = {
            "Item": _project_row(watermark="2026-07-01T00:00:00Z"),
        }
        wis = [_make_wi(7)]

        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value="tok"), \
             patch.object(handler, "run_wiql", return_value=[7]), \
             patch.object(handler, "get_work_items", return_value=wis), \
             patch.object(
                 handler, "get_work_item_comments",
                 side_effect=api_module.AzureDevOpsBoardsAPIError(
                     "boom", status_code=503,
                 ),
             ), \
             patch.object(handler, "log_source_sync"):
            result = handler.process_sync_request(_sync_message())

        assert result["queued"] == 1
        body = json.loads(mock_sqs_client.send_message.call_args.kwargs["MessageBody"])
        assert body["file"]["comments"] == []


# ---------------------------------------------------------------------------
# Auth / error handling
# ---------------------------------------------------------------------------


class TestTokenRefreshFailure:

    def test_none_return_marks_auth_error(self, mock_table):
        mock_table.get_item.return_value = {"Item": _project_row()}
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(handler, "refresh_access_token", return_value=None), \
             patch.object(handler.dual_write, "ddb_update_item") as mock_update:
            result = handler.process_sync_request(_sync_message())

        assert result["status"] == "error"
        assert result["error"] == "auth_error"

        auth_calls = [
            c for c in mock_update.call_args_list
            if c.kwargs.get("ExpressionAttributeValues", {}).get(":status")
                == "auth_error"
        ]
        assert auth_calls
        assert (
            "reconnect_required"
            in auth_calls[0].kwargs["ExpressionAttributeValues"][":error"]
        )

    def test_raised_exception_marks_auth_error(self, mock_table):
        mock_table.get_item.return_value = {"Item": _project_row()}
        with patch.object(handler, "_get_client_secret", return_value="s"), \
             patch.object(
                 handler, "refresh_access_token",
                 side_effect=RuntimeError("boom"),
             ):
            result = handler.process_sync_request(_sync_message())
        assert result["error"] == "auth_error"


class TestLambdaHandler:

    def test_missing_records_returns_400(self):
        result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 400

    def test_invalid_json_body_skipped(self, mock_table):
        event = {"Records": [{"body": "not-json"}]}
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200

    def test_missing_required_fields_returns_error_but_doesnt_raise(
        self, mock_table,
    ):
        event = {"Records": [{"body": json.dumps({"workspace_id": "ws1"})}]}
        # Missing project/org/slug/connection_id -> error but 200 back
        # from lambda_handler.
        result = handler.lambda_handler(event, None)
        assert result["statusCode"] == 200
