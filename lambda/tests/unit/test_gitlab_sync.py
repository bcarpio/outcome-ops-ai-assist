"""Unit tests for gitlab-sync worker (ADR-050, chunk-03).

Mirrors test_azure_devops_sync.py. The worker writes each indexable
blob directly to the KB S3 bucket (process-documents is S3-triggered +
source-agnostic) and fans application repos into REPO_SUMMARIES_QUEUE
for code-map + symbol-graph generation.

Tests target:
1. State machine: pending -> in_progress -> success / skip-unchanged
   / failed. Terminal states bump last_sync_at; in_progress doesn't.
2. Skip-unchanged short-circuit (matching SHA + non-manual trigger).
3. Manual trigger overrides the skip-unchanged.
4. File filter mirrors github-sync (extension allow-list + SKIP_DIRS).
   NOTE: GitLab's ``get_tree`` returns bare blob PATH STRINGS, so the
   filter operates on strings (no gitObjectType/size).
5. S3 upload uses the workspaces/{ws}/gitlab/{slug}/{path} key shape +
   source=gitlab metadata; goes through dual_write so HA replication fires.
6. Per-file fetch errors (404 = file vanished mid-walk, other errors =
   count + continue) are non-fatal at the message level.
7. Failure classification (auth 401/403, not-found 404, refresh None)
   lands sync_status=failed with the right last_sync_error.
8. Partial-batch-failure routing for the SQS handler.
9. Application repos fan out to REPO_SUMMARIES_QUEUE with provider=gitlab.
"""

import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# Module loading + env handling
# ---------------------------------------------------------------------------


def _load_handler():
    if "gitlab_sync_handler" in sys.modules:
        del sys.modules["gitlab_sync_handler"]
    sync_dir = os.path.join(os.path.dirname(__file__), "../../gitlab-sync")
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)
    handler_path = os.path.join(sync_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("gitlab_sync_handler", handler_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["gitlab_sync_handler"] = module
    spec.loader.exec_module(module)
    return module


_HANDLER_LOAD_ENV = {
    "WORKSPACES_TABLE": "test-workspaces",
    "KB_BUCKET": "test-kb-bucket",
    "OAUTH_KMS_KEY_ARN": "arn:aws:kms:us-west-2:123:key/abc",
    "GITLAB_BASE_URL": "https://gitlab.com",
    "GITLAB_CLIENT_ID": "client-id",
    "GITLAB_CLIENT_SECRET_SSM_PARAM": "/dev/oo/gitlab/client-secret",
    "REPO_SUMMARIES_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/repo-summaries.fifo",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

handler = _load_handler()

for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


_SLUG = "example-group/sub/example-project"


def _http_error(code):
    return urllib.error.HTTPError("https://gitlab.com", code, "err", None, None)


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
    yield table
    handler._dynamodb = None


@pytest.fixture(autouse=True)
def _mock_dual_write():
    with patch.object(handler, "dual_write") as mock:
        mock.ddb_update_item = MagicMock()
        mock.s3_put_object = MagicMock()
        yield mock


@pytest.fixture(autouse=True)
def _mock_sqs():
    """Stub the sqs client so the repo-summaries fan-out is observable."""
    sqs = MagicMock()
    handler._sqs = sqs
    yield sqs
    handler._sqs = None


def _msg(**overrides):
    base = {
        "workspace_id": "w1",
        "connection_id": "c1",
        "slug": _SLUG,
        "group": "example-group/sub",
        "project": "example-project",
        "triggered_by": "scheduler",
        "sync_id": "uuid-xyz",
    }
    base.update(overrides)
    return base


def _repo_row(**overrides):
    base = {
        "PK": "WS#w1",
        "SK": f"GITLAB_REPO#{_SLUG}",
        "slug": _SLUG,
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
# File filter (mirrors github-sync's should_process_*; operates on strings)
# ---------------------------------------------------------------------------


class TestIsIndexable:

    def test_blob_with_indexable_extension(self):
        assert handler._is_indexable("src/main.py")

    def test_leading_slash_tolerated(self):
        assert handler._is_indexable("/src/main.py")

    def test_unindexable_extension_skipped(self):
        assert not handler._is_indexable("bin/app.exe")

    def test_no_extension_skipped(self):
        assert not handler._is_indexable("Makefile")

    def test_path_in_skip_dir_filtered(self):
        assert not handler._is_indexable("ui/node_modules/foo/bar.js")
        assert not handler._is_indexable("node_modules/x.json")

    def test_empty_path_skipped(self):
        assert not handler._is_indexable("")

    def test_doc_extensions_indexable(self):
        for ext in (".md", ".json", ".yaml"):
            assert handler._is_indexable(f"docs/x{ext}")

    def test_code_extensions_indexable(self):
        for ext in (".py", ".ts", ".java", ".tf"):
            assert handler._is_indexable(f"src/x{ext}")


# ---------------------------------------------------------------------------
# Skip-unchanged short-circuit
# ---------------------------------------------------------------------------


class TestSkipUnchanged:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    def test_matching_sha_and_non_manual_skips_walk(
        self, mock_tok, mock_sha, mock_branch, mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="scheduler"))
        assert result["status"] == "skip-unchanged"
        assert not _mock_dual_write.s3_put_object.called

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-A")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=[])
    def test_manual_trigger_overrides_skip_unchanged(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(last_commit_sha="sha-A"),
        }
        result = handler.process_sync_message(_msg(triggered_by="manual"))
        assert result["status"] == "success"
        mock_tree.assert_called_once()


# ---------------------------------------------------------------------------
# S3 upload happy path
# ---------------------------------------------------------------------------


class TestS3Upload:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=[
        "src/main.py",
        "docs/README.md",
        "dist/bundle.js",   # under dist -- skipped
        "bin/app.exe",      # bad ext -- skipped
    ])
    @patch.object(handler, "get_file_content", return_value=b"file body")
    def test_uploads_only_indexable_files(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row()}
        result = handler.process_sync_message(_msg())
        assert result["status"] == "success"
        assert result["uploaded"] == 2
        assert mock_get_content.call_count == 2
        assert _mock_dual_write.s3_put_object.call_count == 2

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=["src/main.py"])
    @patch.object(handler, "get_file_content", return_value=b"file body")
    def test_s3_key_shape_and_metadata(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_table.get_item.return_value = {
            "Item": _repo_row(repo_type="application"),
        }
        handler.process_sync_message(_msg())
        call_kwargs = _mock_dual_write.s3_put_object.call_args.kwargs
        assert call_kwargs["key"] == (
            f"workspaces/w1/gitlab/{_SLUG}/src/main.py"
        )
        assert call_kwargs["Metadata"]["source"] == "gitlab"
        assert call_kwargs["Metadata"]["gitlab_slug"] == _SLUG
        assert call_kwargs["Metadata"]["gitlab_commit_sha"] == "sha-B"
        assert call_kwargs["Metadata"]["gitlab_repo_type"] == "application"
        assert call_kwargs["bucket"] == "test-kb-bucket"

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=["a.py", "b.py"])
    def test_vanished_file_doesnt_fail_whole_sync(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        # First file vanished (404) between tree-walk + fetch; second succeeds.
        with patch.object(handler, "get_file_content", side_effect=[
            _http_error(404),
            b"second file body",
        ]):
            mock_table.get_item.return_value = {"Item": _repo_row()}
            result = handler.process_sync_message(_msg())
        assert result["status"] == "success"
        assert result["uploaded"] == 1
        assert _mock_dual_write.s3_put_object.call_count == 1


# ---------------------------------------------------------------------------
# Repo-summaries fan-out (code-map + symbol-graph)
# ---------------------------------------------------------------------------


class TestRepoSummariesFanOut:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=["src/main.py"])
    @patch.object(handler, "get_file_content", return_value=b"body")
    def test_application_repo_fans_out_with_provider_gitlab(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write, _mock_sqs,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row(repo_type="application")}
        handler.process_sync_message(_msg())
        send = _mock_sqs.send_message
        assert send.called
        body = json.loads(send.call_args.kwargs["MessageBody"])
        assert body["provider"] == "gitlab"
        assert body["slug"] == _SLUG
        assert body["connection_id"] == "c1"
        assert body["current_commit_sha"] == "sha-B"
        assert body["all_files"] == ["src/main.py"]
        assert send.call_args.kwargs["MessageGroupId"] == _SLUG

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=["docs/standard.md"])
    @patch.object(handler, "get_file_content", return_value=b"body")
    def test_standards_repo_does_not_fan_out(
        self, mock_get_content, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write, _mock_sqs,
    ):
        mock_table.get_item.return_value = {"Item": _repo_row(repo_type="standards")}
        handler.process_sync_message(_msg())
        assert not _mock_sqs.send_message.called


# ---------------------------------------------------------------------------
# Failure classification
# ---------------------------------------------------------------------------


class TestFailures:

    @patch.object(handler, "get_valid_access_token", return_value=None)
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
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree")
    def test_auth_error_on_tree_walk_classifies_reconnect_required(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_tree.side_effect = _http_error(401)
        mock_table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        with pytest.raises(urllib.error.HTTPError):
            handler.process_sync_message(_msg())
        statuses_and_errors = [
            (c.kwargs["ExpressionAttributeValues"].get(":status"),
             c.kwargs["ExpressionAttributeValues"].get(":error"))
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        # "reconnect_required" is the contract the UI keys off for the banner.
        assert ("failed", "reconnect_required") in statuses_and_errors

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree")
    def test_not_found_on_tree_walk_classifies_repo_or_branch_not_found(
        self, mock_tree, mock_tok, mock_sha, mock_branch,
        mock_table, _mock_dual_write,
    ):
        mock_tree.side_effect = _http_error(404)
        mock_table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        with pytest.raises(urllib.error.HTTPError):
            handler.process_sync_message(_msg())
        errors = [
            c.kwargs["ExpressionAttributeValues"].get(":error")
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        assert "repo_or_branch_not_found" in errors


class TestDeletedRowToleranceOnUpdate:

    @patch.object(handler, "get_default_branch", return_value="main")
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_valid_access_token", return_value="access-token")
    @patch.object(handler, "get_tree", return_value=[])
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
# Concurrency: per-repo dedup lock + 401 force-refresh retry (ADR-050 v1.1)
# ---------------------------------------------------------------------------


class TestConcurrencyHardening:

    def test_concurrent_message_skipped_when_lock_held(
        self, mock_table, _mock_dual_write,
    ):
        # Another worker holds the per-repo sync lock: the conditional
        # acquire fails, so this message no-ops instead of double-walking.
        mock_table.get_item.return_value = {"Item": _repo_row()}
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        result = handler.process_sync_message(_msg())
        assert result["status"] == "skip-in-progress"
        # Never started the actual sync -> no in_progress status write.
        statuses = [
            c.kwargs["ExpressionAttributeValues"].get(":status")
            for c in _mock_dual_write.ddb_update_item.call_args_list
        ]
        assert "in_progress" not in statuses

    @patch.object(handler, "get_tree", return_value=[])
    @patch.object(handler, "get_latest_commit_sha", return_value="sha-B")
    @patch.object(handler, "get_default_branch",
                  side_effect=[_http_error(401), "main"])
    @patch.object(handler, "get_valid_access_token",
                  side_effect=["stale-token", "fresh-token"])
    def test_401_forces_refresh_and_retries_once(
        self, mock_tok, mock_branch, mock_sha, mock_tree,
        mock_table, _mock_dual_write,
    ):
        # First attempt 401s on the cached token; the worker force-refreshes
        # (single-flight) and retries the sync once, which succeeds.
        mock_table.get_item.return_value = {"Item": _repo_row(last_commit_sha="sha-A")}
        result = handler.process_sync_message(_msg(triggered_by="manual"))
        assert result["status"] == "success"
        assert mock_branch.call_count == 2       # initial + retry
        assert mock_tok.call_count == 2
        # The retry token fetch is a forced refresh.
        assert mock_tok.call_args_list[1].kwargs.get("force_refresh") is True


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
                {"messageId": "m2", "body": json.dumps(_msg(slug="g/r2"))},
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
                {"messageId": "m2", "body": json.dumps(_msg(slug="g/r2"))},
            ]
        }
        result = handler.lambda_handler(event, None)
        assert result["batchItemFailures"] == [{"itemIdentifier": "m2"}]
