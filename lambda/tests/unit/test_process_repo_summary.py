"""
Unit tests for process-repo-summary Lambda handler.

Tests the repository summary generation and storage functions.
"""

import json
import os
import sys
import pytest
import importlib.util
from unittest.mock import patch, MagicMock
from types import ModuleType
from botocore.exceptions import ClientError


# Load handler using importlib to avoid module name collision
def load_handler_module():
    """Load the process-repo-summary handler with proper mocking."""
    handler_path = os.path.join(
        os.path.dirname(__file__),
        '../../process-repo-summary/handler.py'
    )

    # Create mock clients
    mock_dynamodb = MagicMock()
    mock_bedrock = MagicMock()
    mock_s3 = MagicMock()
    mock_sqs = MagicMock()
    mock_ssm = MagicMock()
    mock_s3vectors = MagicMock()

    def get_mock_client(service, **kwargs):
        clients = {
            'dynamodb': mock_dynamodb,
            'bedrock-runtime': mock_bedrock,
            's3': mock_s3,
            'sqs': mock_sqs,
            'ssm': mock_ssm,
            's3vectors': mock_s3vectors,
        }
        return clients.get(service, MagicMock())

    # Mock boto3
    mock_boto3 = MagicMock()
    mock_boto3.client.side_effect = get_mock_client

    # Temporarily modify sys.modules
    original_boto3 = sys.modules.get('boto3')
    sys.modules['boto3'] = mock_boto3

    try:
        spec = importlib.util.spec_from_file_location(
            "process_repo_summary_handler",
            handler_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        # Restore original boto3 if it existed
        if original_boto3 is not None:
            sys.modules['boto3'] = original_boto3


# Load the handler module
repo_summary_handler = load_handler_module()


class TestComputeContentHash:
    """Test content hash computation."""

    def test_compute_hash_returns_string(self):
        """Test: compute_content_hash returns a string."""
        result = repo_summary_handler.compute_content_hash("test content")
        assert isinstance(result, str)

    def test_compute_hash_is_deterministic(self):
        """Test: Same content produces same hash."""
        content = "test content"
        hash1 = repo_summary_handler.compute_content_hash(content)
        hash2 = repo_summary_handler.compute_content_hash(content)
        assert hash1 == hash2

    def test_compute_hash_different_content(self):
        """Test: Different content produces different hash."""
        hash1 = repo_summary_handler.compute_content_hash("content 1")
        hash2 = repo_summary_handler.compute_content_hash("content 2")
        assert hash1 != hash2

    def test_compute_hash_length(self):
        """Test: Hash is SHA-256 length (64 hex chars)."""
        result = repo_summary_handler.compute_content_hash("test")
        assert len(result) == 64


class TestRetryBedrockCall:
    """Test Bedrock retry logic."""

    def test_successful_call_no_retry(self):
        """Test: Successful call returns immediately."""
        mock_func = MagicMock(return_value="success")

        result = repo_summary_handler.retry_bedrock_call(mock_func, max_retries=3)

        assert result == "success"
        assert mock_func.call_count == 1

    def test_returns_result_from_function(self):
        """Test: Returns the result from the wrapped function."""
        expected = {"key": "value", "nested": {"data": [1, 2, 3]}}
        mock_func = MagicMock(return_value=expected)

        result = repo_summary_handler.retry_bedrock_call(mock_func)

        assert result == expected


class TestListRepositoryFiles:
    """Test GitHub file listing."""

    @patch.object(repo_summary_handler, 'urlopen')
    @patch.object(repo_summary_handler, 'GITHUB_TOKEN', 'test-token')
    def test_list_files_returns_paths(self, mock_urlopen):
        """Test: Returns list of file paths."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "tree": [
                {"path": "file1.py", "type": "blob"},
                {"path": "file2.py", "type": "blob"},
                {"path": "dir", "type": "tree"},
            ]
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = repo_summary_handler.list_repository_files("owner/repo")

        assert result == ["file1.py", "file2.py"]

    @patch.object(repo_summary_handler, 'urlopen')
    @patch.object(repo_summary_handler, 'GITHUB_TOKEN', 'test-token')
    def test_list_files_excludes_directories(self, mock_urlopen):
        """Test: Excludes directories from results."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "tree": [
                {"path": "src", "type": "tree"},
                {"path": "src/main.py", "type": "blob"},
            ]
        }).encode()
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = repo_summary_handler.list_repository_files("owner/repo")

        assert "src" not in result
        assert "src/main.py" in result


class TestHandler:
    """Test main Lambda handler."""

    @pytest.fixture(autouse=True)
    def _license_layer_mocks(self, monkeypatch):
        """Default license-layer mocks so existing tests run as if the
        layer were attached and validation succeeded. License-specific
        tests below override these fixtures."""
        monkeypatch.setattr(
            repo_summary_handler,
            "validate_license",
            MagicMock(return_value={"sub": "test-customer"}),
        )
        monkeypatch.setattr(
            repo_summary_handler,
            "check_repo_limit",
            MagicMock(return_value=MagicMock(allowed=True)),
        )
        monkeypatch.setattr(
            repo_summary_handler,
            "track_repo_active",
            MagicMock(return_value=True),
        )
        monkeypatch.setattr(
            repo_summary_handler,
            "track_code_map_updated",
            MagicMock(return_value=True),
        )

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_handler_processes_record(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config
    ):
        """Test: Handler processes SQS record successfully."""
        mock_summary.return_value = "Test summary"
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["file1.py", "file2.py"],
                    "code_units": []
                })
            }]
        }

        result = repo_summary_handler.handler(event, None)

        assert result["batchItemFailures"] == []
        mock_summary.assert_called_once()
        mock_embed.assert_called_once()
        mock_store.assert_called_once()

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    def test_handler_returns_failures(self, mock_summary, mock_config):
        """Test: Handler returns failed message IDs."""
        mock_summary.side_effect = Exception("Generation failed")

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["file1.py"],
                    "code_units": []
                })
            }]
        }

        result = repo_summary_handler.handler(event, None)

        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-1"

    @patch.object(repo_summary_handler, 'load_config')
    def test_handler_empty_records(self, mock_config):
        """Test: Handler handles empty records."""
        event = {"Records": []}

        result = repo_summary_handler.handler(event, None)

        assert result["batchItemFailures"] == []


# ============================================================================
# License layer integration tests (ADR-019)
# ============================================================================


class TestLicenseLayerIntegration:
    """Verify the four license-layer call sites in handler."""

    def _make_event(self, message_id="msg-1", repo_project="owner/repo-a"):
        return {
            "Records": [{
                "messageId": message_id,
                "receiptHandle": f"handle-{message_id}",
                "body": json.dumps({
                    "repo": repo_project.split("/")[-1],
                    "repo_project": repo_project,
                    "repo_type": "application",
                    "all_files": ["file1.py"],
                    "code_units": [],
                })
            }]
        }

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_handler_license_valid(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config,
        monkeypatch,
    ):
        """validate_license() success path: handler proceeds, all four
        license calls fire, batch returns no failures."""
        mock_summary.return_value = "Test summary"
        mock_embed.return_value = [0.1] * 1024

        mock_validate = MagicMock(return_value={"sub": "test-customer"})
        mock_check = MagicMock(return_value=MagicMock(allowed=True))
        mock_track_repo = MagicMock(return_value=True)
        mock_track_map = MagicMock(return_value=True)
        monkeypatch.setattr(repo_summary_handler, "validate_license", mock_validate)
        monkeypatch.setattr(repo_summary_handler, "check_repo_limit", mock_check)
        monkeypatch.setattr(repo_summary_handler, "track_repo_active", mock_track_repo)
        monkeypatch.setattr(
            repo_summary_handler, "track_code_map_updated", mock_track_map,
        )

        result = repo_summary_handler.handler(self._make_event(), None)

        assert result["batchItemFailures"] == []
        mock_validate.assert_called_once()
        mock_check.assert_called_once_with(repo="owner/repo-a")
        mock_track_repo.assert_called_once_with(repo="owner/repo-a")
        mock_track_map.assert_called_once_with(repo="owner/repo-a", branch="main")

    def test_handler_license_import_error(self, monkeypatch):
        """Layer not attached (validate_license is None): batch fails
        every record so SQS retries / DLQs, and a LICENSE_FAIL_OPEN
        audit row is emitted as the regression detector."""
        monkeypatch.setattr(repo_summary_handler, "validate_license", None)
        mock_audit = MagicMock()
        monkeypatch.setattr(
            repo_summary_handler, "log_license_fail_open", mock_audit,
        )

        result = repo_summary_handler.handler(self._make_event(), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}
        mock_audit.assert_called_once()
        # Second positional arg is the lambda short-name.
        assert mock_audit.call_args.args[1] == "process-repo-summary"

    def test_handler_license_invalid(self, monkeypatch):
        """validate_license() raises (expired/invalid/etc): every record
        in the batch is marked failed."""
        monkeypatch.setattr(
            repo_summary_handler,
            "validate_license",
            MagicMock(side_effect=Exception("License expired")),
        )

        result = repo_summary_handler.handler(self._make_event(), None)

        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-1"}]}

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_repo_limit_blocked_skips_repo(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config,
        monkeypatch,
    ):
        """check_repo_limit returns allowed=False for first repo: it is
        skipped (not in batchItemFailures because limit-blocked is not
        a retryable failure), second repo processes normally."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024

        blocked = MagicMock(
            allowed=False,
            reason="Repo limit reached",
            limit=2,
            current=2,
        )
        allowed = MagicMock(allowed=True)
        monkeypatch.setattr(
            repo_summary_handler,
            "validate_license",
            MagicMock(return_value={"sub": "x"}),
        )
        monkeypatch.setattr(
            repo_summary_handler,
            "check_repo_limit",
            MagicMock(side_effect=[blocked, allowed]),
        )
        mock_track_repo = MagicMock()
        mock_track_map = MagicMock()
        monkeypatch.setattr(
            repo_summary_handler, "track_repo_active", mock_track_repo,
        )
        monkeypatch.setattr(
            repo_summary_handler, "track_code_map_updated", mock_track_map,
        )

        event = {
            "Records": [
                {
                    "messageId": "msg-a",
                    "receiptHandle": "h-a",
                    "body": json.dumps({
                        "repo": "repo-a",
                        "repo_project": "owner/repo-a",
                        "repo_type": "application",
                        "all_files": ["f.py"],
                        "code_units": [],
                    }),
                },
                {
                    "messageId": "msg-b",
                    "receiptHandle": "h-b",
                    "body": json.dumps({
                        "repo": "repo-b",
                        "repo_project": "owner/repo-b",
                        "repo_type": "application",
                        "all_files": ["f.py"],
                        "code_units": [],
                    }),
                },
            ]
        }

        result = repo_summary_handler.handler(event, None)

        # Skipped record is NOT a batch failure; only the blocked repo
        # never reaches generate_architectural_summary.
        assert result["batchItemFailures"] == []
        assert mock_summary.call_count == 1
        # Only the allowed repo was tracked.
        mock_track_repo.assert_called_once_with(repo="owner/repo-b")
        mock_track_map.assert_called_once_with(
            repo="owner/repo-b", branch="main",
        )

    @patch.object(repo_summary_handler, 'load_config')
    @patch.object(repo_summary_handler, 'generate_architectural_summary')
    @patch.object(repo_summary_handler, 'generate_embedding')
    @patch.object(repo_summary_handler, 'store_code_map_embedding')
    @patch.object(repo_summary_handler, 's3_client')
    @patch.object(repo_summary_handler, 'send_code_units_to_sqs')
    def test_tracking_called_on_successful_processing(
        self,
        mock_send_sqs,
        mock_s3,
        mock_store,
        mock_embed,
        mock_summary,
        mock_config,
        monkeypatch,
    ):
        """track_repo_active and track_code_map_updated fire exactly
        once per successfully processed record, with branch defaulting
        to 'main' when the SQS message body omits it."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024
        mock_track_repo = MagicMock(return_value=True)
        mock_track_map = MagicMock(return_value=True)
        monkeypatch.setattr(
            repo_summary_handler,
            "validate_license",
            MagicMock(return_value={"sub": "x"}),
        )
        monkeypatch.setattr(
            repo_summary_handler,
            "check_repo_limit",
            MagicMock(return_value=MagicMock(allowed=True)),
        )
        monkeypatch.setattr(
            repo_summary_handler, "track_repo_active", mock_track_repo,
        )
        monkeypatch.setattr(
            repo_summary_handler, "track_code_map_updated", mock_track_map,
        )

        result = repo_summary_handler.handler(
            self._make_event(repo_project="owner/repo-a"), None,
        )

        assert result["batchItemFailures"] == []
        mock_track_repo.assert_called_once_with(repo="owner/repo-a")
        mock_track_map.assert_called_once_with(
            repo="owner/repo-a", branch="main",
        )


# ============================================================================
# Code-map orphan cleanup emit (chunk 02)
# ============================================================================


class TestExpectedCodeMapVectorKeys:
    """The deterministic key computation must match what
    process-batch-summary actually writes (handler.py:936)."""

    def test_workspace_keys_include_workspace_prefix(self):
        units = [
            {"batch_type": "handler-group-summary", "group_name": "chat"},
            {"batch_type": "handler-group-summary", "group_name": "github-sync"},
            {"batch_type": "docs-summary", "group_name": "documentation"},
        ]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", "ws_abc"
        )
        assert keys == [
            "ws_abc#github#myrepo#handler-group-summary#chat",
            "ws_abc#github#myrepo#handler-group-summary#github-sync",
            "ws_abc#github#myrepo#docs-summary#documentation",
        ]

    def test_no_workspace_uses_short_key_shape(self):
        """Mirrors the workspace_id-less branch in
        process-batch-summary/handler.py:938."""
        units = [{"batch_type": "handler-group-summary", "group_name": "chat"}]
        keys = repo_summary_handler._expected_code_map_vector_keys(
            units, "myrepo", None
        )
        assert keys == ["myrepo#handler-group-summary#chat"]

    def test_empty_units_returns_empty(self):
        keys = repo_summary_handler._expected_code_map_vector_keys(
            [], "myrepo", "ws"
        )
        assert keys == []


class TestQueueOrphanCodeMapCleanup:
    """The producer-side: process-repo-summary queues the cleanup job after
    fan-out so any handler/group dropped from the tree gets cleaned up."""

    def test_sends_correct_payload(self, monkeypatch):
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1", "k2"], "ws_abc"
        )

        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args[1]["MessageBody"])
        assert body["action"] == "delete_orphan_code_maps"
        assert body["workspace_id"] == "ws_abc"
        assert body["repo"] == "myrepo"
        assert body["expected_keys"] == ["k1", "k2"]
        # Sent to the delete queue, NOT the code-maps queue
        assert mock_sqs.send_message.call_args[1]["QueueUrl"] == "https://sqs/delete"

    def test_skips_when_no_workspace(self, monkeypatch):
        """Non-workspace repos: the delete-worker action only handles
        workspace-scoped key shapes; skip the round-trip entirely."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], None
        )
        mock_sqs.send_message.assert_not_called()

    def test_skips_when_no_delete_queue_configured(self, monkeypatch):
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(repo_summary_handler, "DELETE_QUEUE", None)

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], "ws_abc"
        )
        mock_sqs.send_message.assert_not_called()

    def test_refuses_empty_expected_keys(self, monkeypatch):
        """Belt+suspenders: delete-worker also defends, but skip the
        round-trip when there's nothing to compare against."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", [], "ws_abc"
        )
        mock_sqs.send_message.assert_not_called()

    def test_sqs_send_failure_is_swallowed(self, monkeypatch):
        """Cleanup is best-effort. If the SQS send fails, log and continue -
        don't break the regen path."""
        mock_sqs = MagicMock()
        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "Throttle", "Message": "x"}}, "SendMessage"
        )
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        # Must not raise
        repo_summary_handler._queue_orphan_code_map_cleanup(
            "myrepo", ["k1"], "ws_abc"
        )


class TestSendCodeUnitsAlsoQueuesCleanup:
    """Integration: send_code_units_to_sqs must (a) queue each unit to
    code-maps-queue (existing behavior) AND (b) queue the orphan cleanup
    to delete-worker (new behavior)."""

    def test_fanout_then_cleanup(self, monkeypatch):
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "CODE_MAPS_QUEUE_URL", "https://sqs/codemaps"
        )
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        units = [
            {
                "batch_type": "handler-group-summary",
                "group_name": "chat",
                "file_paths": ["lambda/chat/handler.py"],
                "storage_key": "k1",
            },
            {
                "batch_type": "handler-group-summary",
                "group_name": "github-sync",
                "file_paths": ["lambda/github-sync/handler.py"],
                "storage_key": "k2",
            },
        ]

        repo_summary_handler.send_code_units_to_sqs(
            units, "myrepo", "owner/myrepo", workspace_id="ws_abc"
        )

        # 2 unit messages + 1 cleanup = 3 sends total
        assert mock_sqs.send_message.call_count == 3

        # Last call is the cleanup
        last_call = mock_sqs.send_message.call_args_list[-1]
        assert last_call[1]["QueueUrl"] == "https://sqs/delete"
        cleanup_body = json.loads(last_call[1]["MessageBody"])
        assert cleanup_body["action"] == "delete_orphan_code_maps"
        assert cleanup_body["workspace_id"] == "ws_abc"
        assert cleanup_body["repo"] == "myrepo"
        assert cleanup_body["expected_keys"] == [
            "ws_abc#github#myrepo#handler-group-summary#chat",
            "ws_abc#github#myrepo#handler-group-summary#github-sync",
        ]

    def test_no_workspace_skips_cleanup_only(self, monkeypatch):
        """Non-workspace mode: per-unit fan-out still happens, cleanup skipped."""
        mock_sqs = MagicMock()
        monkeypatch.setattr(repo_summary_handler, "sqs_client", mock_sqs)
        monkeypatch.setattr(
            repo_summary_handler, "CODE_MAPS_QUEUE_URL", "https://sqs/codemaps"
        )
        monkeypatch.setattr(
            repo_summary_handler, "DELETE_QUEUE", "https://sqs/delete"
        )

        units = [{
            "batch_type": "handler-group-summary",
            "group_name": "chat",
            "file_paths": ["x.py"],
            "storage_key": "k",
        }]

        repo_summary_handler.send_code_units_to_sqs(
            units, "myrepo", "owner/myrepo", workspace_id=None
        )

        # 1 unit message, NO cleanup
        assert mock_sqs.send_message.call_count == 1
        sent_url = mock_sqs.send_message.call_args[1]["QueueUrl"]
        assert sent_url == "https://sqs/codemaps"
