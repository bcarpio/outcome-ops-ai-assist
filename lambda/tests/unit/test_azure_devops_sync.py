# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for azure-devops-sync worker.

After 05a, the worker no longer enqueues to file-ingestion or
repo-summaries -- it writes each indexable blob directly to the KB
S3 bucket. process-documents is S3-triggered + source-agnostic,
so chat KB search picks up ADO files without any downstream Lambda
change.

Tests target:
1. State machine: pending -> in_progress -> success / skip-unchanged
   / failed. Terminal states bump last_sync_at; in_progress doesn't.
2. Skip-unchanged short-circuit (matching SHA + non-manual trigger
   = no S3 walk).
3. Manual trigger overrides the skip-unchanged.
4. File filter mirrors github-sync (extension allow-list +
   SKIP_DIRS + 1MB size cap).
5. S3 upload uses the workspaces/{ws}/azure_devops/{slug}/{path}
   key shape + source=azure_devops metadata; goes through
   dual_write so HA replication fires.
6. Per-file fetch errors (AzureDevOpsNotFoundError = file vanished
   mid-walk, other API errors = count + continue) are non-fatal at
   the message level.
7. Failure classification (auth, not-found at the tree-walk level,
   network) lands sync_status=failed with the right last_sync_error.
8. Partial-batch-failure routing for the SQS handler.
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
    "KB_BUCKET": "test-kb-bucket",
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
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_table():
    """Mock the DDB table. Returns the table mock."""
    table = MagicMock()
    table.get_item.return_value = {}
    resource = MagicMock()
    resource.Table.return_value = table
    handler._dynamodb = resource
    handler._client_secret = "cached-secret"
    yield table
    handler._dynamodb = None
    handler._client_secret = None


@pytest.fixture(autouse=True)
def _mock_dual_write():
    with patch.object(handler, "dual_write") as mock:
        mock.ddb_update_item = MagicMock()
        mock.s3_put_object = MagicMock()
        yield mock


@pytest.fixture(autouse=True)
def _presence():
    """Repo presence (ADR-052): connected unless a test says otherwise."""
    with patch.object(handler, "is_repo_connected", return_value=True) as mock:
        yield mock


def _msg(**overrides):
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
# Missing-row / disabled tolerance
# ---------------------------------------------------------------------------


class TestMissingRow:

    def test_row_missing_returns_without_raise(self, mock_table, _mock_dual_write):
        mock_table.get_item.return_value = {}
        result = handler.process_sync_message(_msg())
        assert result["status"] == "skip-missing-row"
        assert not _mock_dual_write.s3_put_object.called

    def test_disabled_row_returns_without_running(self, mock_table, _mock_dual_write):
        mock_table.get_item.return_value = {"Item": _repo_row(enabled=False)}
        result = handler.process_sync_message(_msg())
        assert result["status"] == "skip-disabled"
        assert not _mock_dual_write.s3_put_object.called


# ---------------------------------------------------------------------------
# File filter (mirrors github-sync's should_process_*)
# ---------------------------------------------------------------------------


class TestIsIndexable:

    def test_blob_with_indexable_extension(self):
        assert handler._is_indexable({
            "path": "/src/main.py", "gitObjectType": "blob", "size": 100,
        })

    def test_tree_objects_skipped(self):
        assert not handler._is_indexable({
            "path": "/src", "gitObjectType": "tree",
        })

    def test_unindexable_extension_skipped(self):
        # Binary blob -- no extension in INDEXABLE_EXTENSIONS.
        assert not handler._is_indexable({
            "path": "/bin/app.exe", "gitObjectType": "blob",
        })

    def test_path_in_skip_dir_filtered(self):
        # node_modules at any depth.
        assert not handler._is_indexable({
            "path": "/ui/node_modules/foo/bar.js", "gitObjectType": "blob",
        })
        assert not handler._is_indexable({
            "path": "/node_modules/x.json", "gitObjectType": "blob",
        })

    def test_oversize_skipped(self):
        assert not handler._is_indexable({
            "path": "/big.md", "gitObjectType": "blob",
            "size": handler.MAX_FILE_SIZE + 1,
        })

    def test_doc_extensions_indexable(self):
        for ext in (".md", ".json", ".yaml"):
            assert handler._is_indexable({
                "path": f"/docs/x{ext}", "gitObjectType": "blob", "size": 10,
            })

    def test_code_extensions_indexable(self):
        for ext in (".py", ".ts", ".java", ".tf"):
            assert handler._is_indexable({
                "path": f"/src/x{ext}", "gitObjectType": "blob", "size": 10,
            })


# ---------------------------------------------------------------------------
# Skip-unchanged short-circuit
# ---------------------------------------------------------------------------


class TestSkipUnchanged:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    def test_matching_sha_and_non_manual_skips_walk(
        self, mock_tok, mock_sha, mock_branch, mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="scheduler"))
        assert result["status"] == "skip-unchanged"
        # No S3 upload happened.
        assert not _mock_dual_write.s3_put_object.called

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[])
    def test_manual_trigger_overrides_skip_unchanged(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="manual"))
        assert result["status"] == "success"
        # Tree was walked even though SHA matched.
        mock_tree.assert_called_once()


# ---------------------------------------------------------------------------
# S3 upload happy path
# ---------------------------------------------------------------------------


class TestS3Upload:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/src/main.py", "gitObjectType": "blob", "size": 100},
        {"path": "/docs/README.md", "gitObjectType": "blob", "size": 200},
        {"path": "/dist/bundle.js", "gitObjectType": "blob", "size": 100},  # under dist -- skipped
        {"path": "/bin/app.exe", "gitObjectType": "blob", "size": 100},     # bad ext -- skipped
        {"path": "/sub", "gitObjectType": "tree"},                          # tree -- skipped
    ])
    @patch.object(handler, "get_file_content", return_value=b"file body")
    def test_uploads_only_indexable_files(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row()}
        result = handler.process_sync_message(_msg())
        # Two indexable files: /src/main.py + /docs/README.md.
        assert result["status"] == "success"
        assert result["uploaded"] == 2
        # Two get_file_content calls + two dual_write.s3_put_object calls.
        assert mock_get_content.call_count == 2
        assert _mock_dual_write.s3_put_object.call_count == 2

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/src/main.py", "gitObjectType": "blob", "size": 100},
    ])
    @patch.object(handler, "get_file_content", return_value=b"file body")
    def test_s3_key_shape_and_metadata(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(repo_type="application"),
        }
        handler.process_sync_message(_msg())
        # The S3 key shape is workspaces/{ws}/azure_devops/{slug}/{path}
        # with the LEADING SLASH stripped.
        call_kwargs = _mock_dual_write.s3_put_object.call_args.kwargs
        assert call_kwargs["key"] == (
            "workspaces/w1/azure_devops/example-org/example-project/example-repo/src/main.py"
        )
        # process-documents reads `source` from metadata to drive vector
        # key prefix + repo-label fallback in chat. Pin this contract.
        assert call_kwargs["Metadata"]["source"] == "azure_devops"
        assert call_kwargs["Metadata"]["azure_devops_slug"] == (
            "example-org/example-project/example-repo"
        )
        assert call_kwargs["Metadata"]["azure_devops_commit_sha"] == "sha-B"
        assert call_kwargs["Metadata"]["azure_devops_repo_type"] == "application"
        assert call_kwargs["bucket"] == "test-kb-bucket"

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/a.py", "gitObjectType": "blob", "size": 10},
        {"path": "/b.py", "gitObjectType": "blob", "size": 10},
    ])
    def test_vanished_file_doesnt_fail_whole_sync(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        from shared.azure_devops_api import AzureDevOpsNotFoundError
        # First file vanished between tree-walk + fetch; second succeeds.
        with patch.object(handler, "get_file_content", side_effect=[
            AzureDevOpsNotFoundError("404 on /a.py"),
            b"second file body",
        ]):
            mock_table.get_item.return_value = {"Item": _repo_row()}
            result = handler.process_sync_message(_msg())
        assert result["status"] == "success"
        assert result["uploaded"] == 1
        # One S3 upload (the second file).
        assert _mock_dual_write.s3_put_object.call_count == 1


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestFailures:

    @patch.object(handler, "refresh_access_token", return_value=None)
    def test_failed_token_refresh_marks_failed_and_raises(
        self, mock_tok, mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row()}
        with pytest.raises(RuntimeError):
            handler.process_sync_message(_msg())
        statuses = [
            c.kwargs["ExpressionAttributeValues"].get(":status")
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        assert "in_progress" in statuses
        assert "failed" in statuses

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive")
    def test_auth_error_on_tree_walk_classifies_reconnect_required(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        from shared.azure_devops_api import AzureDevOpsAuthError
        mock_tree.side_effect = AzureDevOpsAuthError("401")
        mock_table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        with pytest.raises(AzureDevOpsAuthError):
            handler.process_sync_message(_msg())
        statuses_and_errors = [
            (c.kwargs["ExpressionAttributeValues"].get(":status"),
             c.kwargs["ExpressionAttributeValues"].get(":error"))
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        # The "reconnect_required" string is the contract the UI uses
        # to surface the reconnect banner -- pin it.
        assert ("failed", "reconnect_required") in statuses_and_errors


class TestDeletedRowToleranceOnUpdate:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "refresh_access_token", return_value="access-token")
    @patch.object(handler, "get_tree_recursive", return_value=[])
    def test_conditional_check_failure_does_not_propagate(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        _mock_dual_write.ddb_update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}},
            "UpdateItem",
        )
        result = handler.process_sync_message(_msg())
        assert result["status"] == "success"


# ---------------------------------------------------------------------------
# Lambda entry point: partial batch failures
# ---------------------------------------------------------------------------


class TestLambdaHandler:

    @patch.object(handler, "process_sync_message")
    def test_returns_empty_failures_on_all_ok(self, mock_process, mock_table):
        mock_process.return_value = {"status": "success"}
        event = {
            "Records": [
                {"messageId": "m1", "body": json.dumps(_msg())},
                {"messageId": "m2", "body": json.dumps(_msg(slug="o/p/r2"))},
            ]
        }
        assert handler.lambda_handler(event, None) == {"batchItemFailures": []}

    @patch.object(handler, "process_sync_message")
    def test_returns_failed_ids_on_per_message_failure(
        self, mock_process, mock_table,
    ):
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


# ---------------------------------------------------------------------------
# Repo removed mid-flight (ADR-052): start, before upload, before dispatch
# ---------------------------------------------------------------------------


class TestRepoRemovedGuards:

    def _run(self, mock_table, _presence, presence_side_effect):
        _presence.side_effect = presence_side_effect
        mock_table.get_item.return_value = {"Item": _repo_row(repo_type="application")}
        with patch.object(handler, "get_default_branch", return_value="main") as branch, \
                patch.object(handler, "get_latest_commit_sha", return_value="sha-B"), \
                patch.object(handler, "refresh_access_token", return_value="access-token") as tok, \
                patch.object(handler, "get_tree_recursive", return_value=[
        {"path": "/src/main.py", "gitObjectType": "blob", "size": 10},
        {"path": "/docs/README.md", "gitObjectType": "blob", "size": 10},
    ]) as tree, \
                patch.object(handler, "get_file_content", return_value=b"body"), \
                patch.object(handler, "_enqueue_repo_summaries_job") as enqueue:
            result = handler.process_sync_message(_msg(triggered_by="manual"))
        return result, branch, tok, tree, enqueue

    def test_removed_at_start_makes_no_provider_calls(
        self, mock_table, _presence, _mock_dual_write, caplog,
    ):
        caplog.set_level("INFO")
        result, branch, tok, tree, enqueue = self._run(mock_table, _presence, [False])
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "start"
        tok.assert_not_called()
        branch.assert_not_called()
        tree.assert_not_called()
        mock_table.get_item.assert_not_called()
        assert not _mock_dual_write.ddb_update_item.called
        assert not _mock_dual_write.s3_put_object.called
        enqueue.assert_not_called()
        _presence.assert_called_once_with("w1", "azure_devops", "example-org/example-project/example-repo")
        assert (
            "[azure-devops-sync] repo removed, dropping sync for w1/" in caplog.text
        )

    def test_removed_before_upload_uploads_and_dispatches_nothing(
        self, mock_table, _presence, _mock_dual_write,
    ):
        result, _, _, tree, enqueue = self._run(mock_table, _presence, [True, False])
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "before_upload"
        tree.assert_called_once()
        assert not _mock_dual_write.s3_put_object.called
        enqueue.assert_not_called()

    def test_removed_before_dispatch_keeps_uploads_but_does_not_dispatch(
        self, mock_table, _presence, _mock_dual_write,
    ):
        result, _, _, _, enqueue = self._run(
            mock_table, _presence, [True, True, False],
        )
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "before_dispatch"
        assert _mock_dual_write.s3_put_object.call_count == 2
        enqueue.assert_not_called()

    def test_connected_runs_all_three_checks_and_dispatches(
        self, mock_table, _presence, _mock_dual_write,
    ):
        result, _, _, _, enqueue = self._run(
            mock_table, _presence, [True, True, True],
        )
        assert result["status"] == "success"
        assert _presence.call_count == 3
        enqueue.assert_called_once()

    def test_lookup_error_at_start_raises(self, mock_table, _presence):
        from shared.repo_presence import RepoPresenceError
        with pytest.raises(RepoPresenceError):
            self._run(mock_table, _presence, RepoPresenceError("ddb down"))

    def test_lookup_error_mid_sync_marks_failed_and_raises(
        self, mock_table, _presence, _mock_dual_write,
    ):
        from shared.repo_presence import RepoPresenceError
        with pytest.raises(RepoPresenceError):
            self._run(
                mock_table, _presence, [True, RepoPresenceError("ddb down")],
            )
        assert not _mock_dual_write.s3_put_object.called
        statuses = [
            c.kwargs["ExpressionAttributeValues"].get(":status")
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        assert "failed" in statuses

    def test_lambda_handler_removed_is_not_redelivered(self, mock_table, _presence):
        _presence.return_value = False
        event = {"Records": [{"messageId": "m1", "body": json.dumps(_msg())}]}
        assert handler.lambda_handler(event, None) == {"batchItemFailures": []}

    def test_lambda_handler_lookup_error_is_redelivered(self, mock_table, _presence):
        from shared.repo_presence import RepoPresenceError
        _presence.side_effect = RepoPresenceError("ddb down")
        event = {"Records": [{"messageId": "m1", "body": json.dumps(_msg())}]}
        assert handler.lambda_handler(event, None) == {
            "batchItemFailures": [{"itemIdentifier": "m1"}],
        }
