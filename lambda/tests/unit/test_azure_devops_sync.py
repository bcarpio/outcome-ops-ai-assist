"""Unit tests for azure-devops-sync worker.

The worker's surface is small but the state-machine + skip-unchanged
behavior is load-bearing for both the hourly scheduler tick and the
manual UI-triggered sync. Tests target what the chunk plan calls out:

1. State-machine transitions: pending -> in_progress -> success /
   skip-unchanged / failed. Each terminal state writes last_sync_at;
   in_progress doesn't.
2. Skip-unchanged short-circuit: if last_commit_sha matches the live
   tip AND triggered_by != "manual", no downstream enqueue happens
   and the row lands in skip-unchanged. The hourly scheduler retick
   stays cheap.
3. Manual override: if triggered_by == "manual", the short-circuit
   does NOT fire even when SHA matches -- the user explicitly asked
   for a re-sync.
4. Failure classification: ADO auth/not-found/api errors land in
   sync_status=failed with last_sync_error populated, then re-raise
   so SQS retries (and eventually DLQs).
5. Provider-aware downstream enqueue: application repo -> one
   repo-summaries message with provider="azure_devops". Standards
   repo -> N file-ingestion messages, one per blob in the tree.
   The chunk-05a downstream consumers will read these fields; pin
   the shape now so the contract is stable when 05a lands.
6. Deleted-row tolerance: if the AZDO_REPO# row was deleted between
   the scheduler enqueue and the sync pickup, the worker logs +
   returns rather than raising.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Module loading + env handling
# ---------------------------------------------------------------------------


def _load_handler():
    if "azdo_sync_handler" in sys.modules:
        del sys.modules["azdo_sync_handler"]
    sync_dir = os.path.join(os.path.dirname(__file__), "../../azure-devops-sync")
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)
    handler_path = os.path.join(sync_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("azdo_sync_handler", handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["azdo_sync_handler"] = module
    spec.loader.exec_module(module)
    return module


_HANDLER_LOAD_ENV = {
    "WORKSPACES_TABLE": "test-workspaces",
    "FILE_INGESTION_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/file-ingestion",
    "REPO_SUMMARIES_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/repo-summaries",
    "AZURE_DEVOPS_TENANT_ID": "contoso-tenant-uuid",
    "AZURE_DEVOPS_CLIENT_ID": "client-uuid",
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM": "/dev/oo/azure-devops/client-secret",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

handler = _load_handler()

for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_table_and_sqs():
    """Mock the DDB table + SQS client. Returns (table, sqs)."""
    table = MagicMock()
    table.get_item.return_value = {}
    resource = MagicMock()
    resource.Table.return_value = table
    handler._dynamodb = resource

    sqs = MagicMock()
    handler._sqs = sqs

    # Skip the real ssm read of client_secret.
    handler._client_secret = "cached-secret"

    yield table, sqs

    handler._dynamodb = None
    handler._sqs = None
    handler._client_secret = None


@pytest.fixture(autouse=True)
def _mock_dual_write():
    """Avoid the real dual_write going to a moto-backed table -- we
    only care that the update was issued, not its persistence."""
    with patch.object(handler, "dual_write") as mock:
        mock.ddb_update_item = MagicMock()
        yield mock


def _msg(**overrides):
    """Build a sync SQS message body."""
    base = {
        "workspace_id": "w1",
        "connection_id": "c1",
        "slug": "example-org/example-project/example-repo",
        "org": "example-org",
        "project": "example-project",
        "repo": "example-repo",
        "triggered_by": "scheduler",
        "sync_id": "uuid-xyz",
    }
    base.update(overrides)
    return base


def _repo_row(**overrides):
    """Build an AZDO_REPO# row as it would come back from get_item."""
    base = {
        "PK": "WS#w1",
        "SK": "AZDO_REPO#example-org/example-project/example-repo",
        "slug": "example-org/example-project/example-repo",
        "connection_id": "c1",
        "enabled": True,
        "include_code_maps": True,
        "repo_type": "application",
        "last_commit_sha": None,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Deleted-row tolerance
# ---------------------------------------------------------------------------


class TestMissingRow:

    def test_row_missing_returns_without_raise(self, mock_table_and_sqs):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {}  # No Item key
        result = handler.process_sync_message(_msg())
        assert result["status"] == "skip-missing-row"
        # No SQS send, no token refresh attempt would have run.
        assert not sqs.send_message.called

    def test_disabled_row_returns_without_running(self, mock_table_and_sqs):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {"Item": _repo_row(enabled=False)}
        result = handler.process_sync_message(_msg())
        assert result["status"] == "skip-disabled"
        assert not sqs.send_message.called


# ---------------------------------------------------------------------------
# Skip-unchanged short-circuit
# ---------------------------------------------------------------------------


class TestSkipUnchanged:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    def test_matching_sha_and_non_manual_skips_downstream(
        self, mock_tok, mock_sha, mock_branch, mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="scheduler"))
        assert result["status"] == "skip-unchanged"
        assert result["commit_sha"] == "sha-A"
        # No downstream enqueue. No tree fetch either (the worker
        # short-circuits before that call).
        assert not sqs.send_message.called

    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/src/a.py", "gitObjectType": "blob"},
    ])
    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    def test_manual_trigger_overrides_skip_even_when_sha_matches(
        self, mock_tok, mock_sha, mock_branch, mock_tree,
        mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="manual"))
        # The user said "sync now" -- ignore the SHA equality and
        # actually fetch the tree + enqueue downstream.
        assert result["status"] == "success"
        assert sqs.send_message.called

    @patch.object(handler, "get_tree_recursive", return_value=[])
    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    def test_changed_sha_falls_through_to_success(
        self, mock_tok, mock_sha, mock_branch, mock_tree,
        mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg())
        assert result["status"] == "success"
        assert result["commit_sha"] == "sha-B"


# ---------------------------------------------------------------------------
# Downstream enqueue: application vs standards
# ---------------------------------------------------------------------------


class TestDownstreamEnqueue:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/src/a.py", "gitObjectType": "blob", "objectId": "x"},
        {"path": "/src/b.py", "gitObjectType": "blob", "objectId": "y"},
        {"path": "/dir",      "gitObjectType": "tree", "objectId": "z"},
    ])
    def test_application_repo_enqueues_one_repo_summaries_message(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {
            "Item": _repo_row(repo_type="application"),
        }
        result = handler.process_sync_message(_msg())
        assert result["status"] == "success"
        assert result["downstream_enqueued"] == 1
        assert sqs.send_message.call_count == 1
        body = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["provider"] == "azure_devops"
        # Tree-blob count, not including the `tree` entry.
        assert body["file_count"] == 2

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/std/a.md", "gitObjectType": "blob"},
        {"path": "/std/b.md", "gitObjectType": "blob"},
        {"path": "/std/c.md", "gitObjectType": "blob"},
        {"path": "/dir",      "gitObjectType": "tree"},
    ])
    def test_standards_repo_enqueues_one_file_ingestion_message_per_blob(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {
            "Item": _repo_row(repo_type="standards"),
        }
        result = handler.process_sync_message(_msg())
        assert result["downstream_enqueued"] == 3
        # 3 file-ingestion messages (one per blob).
        assert sqs.send_message.call_count == 3
        bodies = [
            json.loads(c.kwargs["MessageBody"])
            for c in sqs.send_message.call_args_list
        ]
        # Each carries provider + path so chunk-05a downstream knows
        # which API client to use.
        for body in bodies:
            assert body["provider"] == "azure_devops"
            assert body["connection_id"] == "c1"
            assert body["org"] == "example-org"
            assert "path" in body


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestFailures:

    @patch.object(handler, "refresh_access_token", return_value=None)
    def test_failed_token_refresh_marks_failed_and_raises(
        self, mock_tok, mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {"Item": _repo_row()}
        with pytest.raises(RuntimeError):
            handler.process_sync_message(_msg())
        # The DDB update sequence: in_progress, then failed.
        update_calls = _mock_dual_write.ddb_update_item.call_args_list
        statuses = [
            c.kwargs["ExpressionAttributeValues"].get(":status")
            for c in update_calls
        ]
        assert "in_progress" in statuses
        assert "failed" in statuses

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive")
    def test_auth_error_classifies_as_failed_and_raises(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table_and_sqs, _mock_dual_write,
    ):
        from azure_devops_api import AzureDevOpsAuthError
        mock_tree.side_effect = AzureDevOpsAuthError("401")
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        with pytest.raises(AzureDevOpsAuthError):
            handler.process_sync_message(_msg())
        # The error message recorded in DDB is "reconnect_required"
        # -- the UI uses this exact token to surface the reconnect
        # banner on the workspace settings page.
        statuses_and_errors = [
            (c.kwargs["ExpressionAttributeValues"].get(":status"),
             c.kwargs["ExpressionAttributeValues"].get(":error"))
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        assert ("failed", "reconnect_required") in statuses_and_errors

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive")
    def test_not_found_classifies_as_failed_and_raises(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table_and_sqs, _mock_dual_write,
    ):
        from azure_devops_api import AzureDevOpsNotFoundError
        mock_tree.side_effect = AzureDevOpsNotFoundError("404")
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        with pytest.raises(AzureDevOpsNotFoundError):
            handler.process_sync_message(_msg())


class TestDeletedRowToleranceOnUpdate:
    """If the repo row gets deleted between the in_progress write and
    the terminal-state write, the worker shouldn't crash. The DDB
    ConditionalCheckFailedException is logged + swallowed."""

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[])
    def test_conditional_check_failure_does_not_propagate(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table_and_sqs, _mock_dual_write,
    ):
        table, sqs = mock_table_and_sqs
        table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        _mock_dual_write.ddb_update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        # Should NOT raise; logs a warning and returns gracefully.
        result = handler.process_sync_message(_msg())
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Lambda entry point: partial batch failures
# ---------------------------------------------------------------------------


class TestLambdaHandler:

    @patch.object(handler, "process_sync_message")
    def test_returns_empty_failures_on_all_ok(self, mock_process, mock_table_and_sqs):
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps(_msg())},
                {"messageId": "m2", "body": json.dumps(_msg(slug="o/p/r2"))},
            ]
        }
        result = handler.lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler, "process_sync_message")
    def test_returns_failed_ids_on_per_message_failure(
        self, mock_process, mock_table_and_sqs,
    ):
        # Second message raises; SQS should retry only that one.
        mock_process.side_effect = [
            {"status": "success"},
            Exception("boom"),
        ]
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps(_msg())},
                {"messageId": "m2", "body": json.dumps(_msg(slug="o/p/r2"))},
            ]
        }
        result = handler.lambda_handler(event, None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]
