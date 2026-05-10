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
