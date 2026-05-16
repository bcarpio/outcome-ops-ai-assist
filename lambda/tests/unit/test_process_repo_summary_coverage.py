"""
Unit tests to increase coverage for process-repo-summary/handler.py.

Covers:
- generate_architectural_summary: prompt building, Bedrock call, audit logging
- generate_embedding: Bedrock Titan call
- store_code_map_embedding: S3 Vectors storage with/without workspace_id
- send_code_units_to_sqs: SQS message sending with/without workspace_id
- retry_bedrock_call: throttling retries, non-retryable errors
- handler: workspace mode with discover_code_units, code unit dispatching
- get_backend_for_repo: backend selection, fallback
- load_config: SSM parameter loading
- get_github_app_config: GitHub App config retrieval
"""

import json
import os
import sys
import importlib.util
from unittest.mock import MagicMock, Mock, patch, call
from types import ModuleType

import pytest
from botocore.exceptions import ClientError


# In the full test suite, test_check_handlers_init.py replaces
# botocore.exceptions.ClientError with Exception. Detect this so we can
# adapt tests that rely on ClientError having .response attribute.
_CLIENT_ERROR_IS_MOCKED = (ClientError is Exception)


def _make_client_error(error_response, operation_name):
    """Create an error compatible with code that accesses .response attribute.

    When ClientError is replaced with Exception in the full suite, real
    ClientError objects lose their .response attribute. This helper creates
    errors that always have .response so the code under test works correctly.
    """
    if _CLIENT_ERROR_IS_MOCKED:
        err = Exception(f"{operation_name}: {error_response}")
        err.response = error_response
        return err
    return ClientError(error_response, operation_name)


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
            "process_repo_summary_handler_cov",
            handler_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        if original_boto3 is not None:
            sys.modules['boto3'] = original_boto3


# Load the handler module
rsh = load_handler_module()


# ############################################################################
# generate_architectural_summary TESTS
# ############################################################################


class TestGenerateArchitecturalSummary:
    """Test architectural summary generation."""

    @patch.object(rsh, 'bedrock_client')
    @patch.object(rsh, 'log_audit_entry')
    def test_generates_summary_from_file_list(self, mock_audit, mock_bedrock):
        """Test summary generation with file list."""
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "This is a Python serverless app"}]
                }
            },
            "usage": {"inputTokens": 100, "outputTokens": 50}
        }

        result = rsh.generate_architectural_summary(
            "test-repo", "application", ["handler.py", "utils.py"]
        )
        assert result == "This is a Python serverless app"
        mock_bedrock.converse.assert_called_once()

    @patch.object(rsh, 'bedrock_client')
    @patch.object(rsh, 'log_audit_entry')
    def test_handles_dict_file_format(self, mock_audit, mock_bedrock):
        """Test summary generation with dict file format (legacy)."""
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Summary"}]
                }
            },
            "usage": {"inputTokens": 50, "outputTokens": 25}
        }

        files = [
            {"path": "handler.py"},
            {"path": "utils.py"},
        ]
        result = rsh.generate_architectural_summary("repo", "app", files)
        assert result == "Summary"

    @patch.object(rsh, 'bedrock_client')
    @patch.object(rsh, 'log_audit_entry')
    def test_truncates_large_file_list(self, mock_audit, mock_bedrock):
        """Test file list truncation over 500 files."""
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": "Summary of large repo"}]
                }
            },
            "usage": {"inputTokens": 500, "outputTokens": 100}
        }

        files = [f"file_{i}.py" for i in range(600)]
        result = rsh.generate_architectural_summary("repo", "app", files)
        assert result == "Summary of large repo"

    @patch.object(rsh, 'bedrock_client')
    def test_raises_on_empty_response(self, mock_bedrock):
        """Test error when Claude returns empty content."""
        mock_bedrock.converse.return_value = {
            "output": {"message": {"content": []}},
            "usage": {}
        }

        with pytest.raises(Exception):
            rsh.generate_architectural_summary("repo", "app", ["f.py"])

    @patch.object(rsh, 'bedrock_client')
    def test_raises_on_client_error(self, mock_bedrock):
        """Test ClientError is raised."""
        mock_bedrock.converse.side_effect = _make_client_error(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "Converse"
        )

        with pytest.raises(Exception):
            rsh.generate_architectural_summary("repo", "app", ["f.py"])


# ############################################################################
# generate_embedding TESTS
# ############################################################################


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch.object(rsh, 'bedrock_client')
    def test_generates_embedding(self, mock_bedrock):
        """Test embedding generation returns vector."""
        mock_body = MagicMock()
        mock_body.read.return_value = json.dumps({
            "embedding": [0.1] * 1024
        }).encode()
        mock_bedrock.invoke_model.return_value = {"body": mock_body}

        result = rsh.generate_embedding("test text")
        assert len(result) == 1024

    @patch.object(rsh, 'bedrock_client')
    def test_raises_on_client_error(self, mock_bedrock):
        """Test ClientError is raised."""
        mock_bedrock.invoke_model.side_effect = _make_client_error(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "InvokeModel"
        )

        with pytest.raises(Exception):
            rsh.generate_embedding("test text")


# ############################################################################
# store_code_map_embedding TESTS
# ############################################################################


class TestStoreCodeMapEmbedding:
    """Test code map embedding storage."""

    @patch.object(rsh, 's3vectors_client')
    @patch.object(rsh, 'VECTOR_BUCKET', 'test-bucket')
    @patch.object(rsh, 'VECTOR_INDEX', 'test-index')
    def test_stores_without_workspace(self, mock_s3v):
        """Test storing embedding without workspace_id."""
        rsh.store_code_map_embedding(
            doc_id="repo/code-map",
            content="summary content",
            content_hash="abc123",
            embedding=[0.1] * 1024,
            repo="owner/repo",
            doc_type="code-map",
            path="code-map"
        )
        mock_s3v.put_vectors.assert_called_once()
        call_kwargs = mock_s3v.put_vectors.call_args.kwargs
        vector = call_kwargs["vectors"][0]
        assert "workspace_id" not in vector["metadata"]

    @patch.object(rsh, 's3vectors_client')
    @patch.object(rsh, 'VECTOR_BUCKET', 'test-bucket')
    @patch.object(rsh, 'VECTOR_INDEX', 'test-index')
    def test_stores_with_workspace(self, mock_s3v):
        """Test storing embedding with workspace_id."""
        rsh.store_code_map_embedding(
            doc_id="repo/code-map",
            content="summary",
            content_hash="abc",
            embedding=[0.1] * 1024,
            repo="owner/repo",
            doc_type="code-map",
            path="code-map",
            workspace_id="ws-123"
        )
        call_kwargs = mock_s3v.put_vectors.call_args.kwargs
        vector = call_kwargs["vectors"][0]
        assert vector["metadata"]["workspace_id"] == "ws-123"
        assert vector["metadata"]["source"] == "github"
        assert vector["key"].startswith("ws-123#github#")

    @patch.object(rsh, 's3vectors_client')
    @patch.object(rsh, 'VECTOR_BUCKET', 'test-bucket')
    @patch.object(rsh, 'VECTOR_INDEX', 'test-index')
    def test_raises_on_client_error(self, mock_s3v):
        """Test ClientError is raised."""
        mock_s3v.put_vectors.side_effect = _make_client_error(
            {"Error": {"Code": "InternalError", "Message": "error"}},
            "PutVectors"
        )
        with pytest.raises(Exception):
            rsh.store_code_map_embedding(
                doc_id="id", content="c", content_hash="h",
                embedding=[0.1], repo="r", doc_type="t", path="p"
            )


# ############################################################################
# send_code_units_to_sqs TESTS
# ############################################################################


class TestSendCodeUnitsToSqs:
    """Test code unit SQS dispatch."""

    @patch.object(rsh, 'sqs_client')
    @patch.object(rsh, 'CODE_MAPS_QUEUE_URL', 'https://sqs.example.com/queue')
    def test_sends_units_without_workspace(self, mock_sqs):
        """Test sending code units without workspace_id."""
        code_units = [
            {"batch_type": "handler", "group_name": "my-handler",
             "file_paths": ["handler.py"], "storage_key": "summary#handler"}
        ]
        rsh.send_code_units_to_sqs(code_units, "repo", "owner/repo")
        mock_sqs.send_message.assert_called_once()
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert "workspace_id" not in body

    @patch.object(rsh, 'sqs_client')
    @patch.object(rsh, 'CODE_MAPS_QUEUE_URL', 'https://sqs.example.com/queue')
    def test_sends_units_with_workspace(self, mock_sqs):
        """Test sending code units with workspace_id."""
        code_units = [
            {"batch_type": "handler", "group_name": "h1",
             "file_paths": ["h.py"], "storage_key": "key"}
        ]
        rsh.send_code_units_to_sqs(code_units, "repo", "owner/repo", workspace_id="ws-1")
        body = json.loads(mock_sqs.send_message.call_args.kwargs["MessageBody"])
        assert body["workspace_id"] == "ws-1"

    @patch.object(rsh, 'sqs_client')
    @patch.object(rsh, 'CODE_MAPS_QUEUE_URL', 'https://sqs.example.com/queue')
    def test_per_unit_client_error_does_not_abort_batch(self, mock_sqs):
        """plan-git-truth chunk 02b: ClientError on a single code-unit
        dispatch no longer re-raises. The function returns the count
        of successful publishes; failed units are recorded out-of-band
        via mark_unit_failed when ledger context is available.

        Replaces the previous test_raises_on_client_error which
        asserted the pre-chunk-02b behavior (re-raise → DLQ the
        whole record). The new contract is "record the failure, keep
        going" so the rest of the batch survives.
        """
        # First unit dispatch raises; second succeeds.
        mock_sqs.send_message.side_effect = [
            _make_client_error(
                {"Error": {"Code": "QueueDoesNotExist", "Message": "gone"}},
                "SendMessage",
            ),
            None,  # second call returns OK
        ]

        # No workspace_id / commit_sha passed -> no failed-files write,
        # but the function still must not raise.
        sent = rsh.send_code_units_to_sqs(
            [
                {"batch_type": "t", "group_name": "g1",
                 "file_paths": ["f1"], "storage_key": "k1"},
                {"batch_type": "t", "group_name": "g2",
                 "file_paths": ["f2"], "storage_key": "k2"},
            ],
            "repo",
            "owner/repo",
        )
        assert sent == 1
        assert mock_sqs.send_message.call_count == 2


# ############################################################################
# retry_bedrock_call TESTS
# ############################################################################


class TestRetryBedrockCall:
    """Test Bedrock retry logic with throttling."""

    @patch("time.sleep")
    def test_retries_on_throttling(self, mock_sleep):
        """Test retries on ThrottlingException."""
        throttle_error = _make_client_error(
            {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
            "Converse"
        )
        mock_func = MagicMock()
        mock_func.side_effect = [throttle_error, "success"]

        result = rsh.retry_bedrock_call(mock_func, max_retries=3)
        assert result == "success"
        assert mock_func.call_count == 2

    def test_raises_non_retryable_error(self):
        """Test non-retryable errors are raised immediately."""
        access_error = _make_client_error(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}},
            "Converse"
        )
        mock_func = MagicMock()
        mock_func.side_effect = access_error

        with pytest.raises(Exception):
            rsh.retry_bedrock_call(mock_func, max_retries=3)
        assert mock_func.call_count == 1

    @patch("time.sleep")
    def test_raises_after_max_retries(self, mock_sleep):
        """Test raises after exhausting all retries."""
        throttle_error = _make_client_error(
            {"Error": {"Code": "ThrottlingException", "Message": "throttled"}},
            "Converse"
        )
        mock_func = MagicMock()
        mock_func.side_effect = throttle_error

        with pytest.raises(Exception):
            rsh.retry_bedrock_call(mock_func, max_retries=2)
        assert mock_func.call_count == 2


# ############################################################################
# handler TESTS (additional coverage)
# ############################################################################


class TestHandlerExtended:
    """Extended handler tests for workspace mode and code units."""

    @pytest.fixture(autouse=True)
    def _license_layer_mocks(self, monkeypatch):
        """Patch ADR-019 license layer calls so existing handler tests
        run as if the layer were attached and validation succeeded."""
        monkeypatch.setattr(
            rsh, "validate_license",
            MagicMock(return_value={"sub": "test-customer"}),
        )
        monkeypatch.setattr(
            rsh, "check_repo_limit",
            MagicMock(return_value=MagicMock(allowed=True)),
        )
        monkeypatch.setattr(
            rsh, "track_code_map_updated", MagicMock(return_value=True),
        )

    @patch.object(rsh, 'load_config')
    @patch.object(rsh, 'generate_architectural_summary')
    @patch.object(rsh, 'generate_embedding')
    @patch.object(rsh, 'store_code_map_embedding')
    @patch.object(rsh, 's3_client')
    @patch.object(rsh, 'send_code_units_to_sqs')
    def test_handler_with_code_units(
        self, mock_send, mock_s3, mock_store, mock_embed, mock_summary, mock_config
    ):
        """Test handler dispatches code units to SQS."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["f1.py"],
                    "code_units": [
                        {"batch_type": "handler", "group_name": "h1",
                         "file_paths": ["f1.py"], "storage_key": "key"}
                    ]
                })
            }]
        }

        result = rsh.handler(event, None)
        assert result["batchItemFailures"] == []
        mock_send.assert_called_once()

    @patch.object(rsh, 'load_config')
    @patch.object(rsh, 'generate_architectural_summary')
    @patch.object(rsh, 'generate_embedding')
    @patch.object(rsh, 'store_code_map_embedding')
    @patch.object(rsh, 's3_client')
    @patch.object(rsh, 'send_code_units_to_sqs')
    def test_handler_workspace_mode_s3_key(
        self, mock_send, mock_s3, mock_store, mock_embed, mock_summary, mock_config
    ):
        """Test handler uses workspace-scoped S3 key."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["f1.py"],
                    "workspace_id": "ws-123",
                    "code_units": []
                })
            }]
        }

        result = rsh.handler(event, None)
        assert result["batchItemFailures"] == []

        # Verify S3 key includes workspace
        s3_call = mock_s3.put_object.call_args
        s3_key = s3_call.kwargs.get("Key") or s3_call[1].get("Key")
        assert "workspaces/ws-123" in s3_key

    @patch.object(rsh, 'load_config')
    @patch.object(rsh, 'generate_architectural_summary')
    @patch.object(rsh, 'generate_embedding')
    @patch.object(rsh, 'store_code_map_embedding')
    @patch.object(rsh, 's3_client')
    @patch.object(rsh, 'send_code_units_to_sqs')
    @patch.object(rsh, 'get_installation_token')
    @patch.object(rsh, 'discover_code_units')
    def test_handler_workspace_discover_units(
        self, mock_discover, mock_install_token,
        mock_send, mock_s3, mock_store, mock_embed, mock_summary, mock_config
    ):
        """Test handler discovers code units for workspace repos."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024
        mock_install_token.return_value = "installation-token"
        mock_discover.return_value = [
            {"batch_type": "handler", "group_name": "h1",
             "file_paths": ["h.py"], "storage_key": "key"}
        ]

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["h.py"],
                    "workspace_id": "ws-1",
                    "installation_id": "12345",
                    "discover_code_units": True,
                })
            }]
        }

        result = rsh.handler(event, None)
        assert result["batchItemFailures"] == []
        mock_install_token.assert_called_once_with("12345")
        mock_discover.assert_called_once()
        mock_send.assert_called_once()

    @patch.object(rsh, 'load_config')
    @patch.object(rsh, 'generate_architectural_summary')
    @patch.object(rsh, 'generate_embedding')
    @patch.object(rsh, 'store_code_map_embedding')
    @patch.object(rsh, 's3_client')
    @patch.object(rsh, 'send_code_units_to_sqs')
    @patch.object(rsh, 'get_installation_token')
    def test_handler_workspace_discover_failure_fallback(
        self, mock_install_token,
        mock_send, mock_s3, mock_store, mock_embed, mock_summary, mock_config
    ):
        """Test handler handles code unit discovery failure gracefully."""
        mock_summary.return_value = "Summary"
        mock_embed.return_value = [0.1] * 1024
        mock_install_token.side_effect = Exception("Token exchange failed")

        event = {
            "Records": [{
                "messageId": "msg-1",
                "receiptHandle": "handle-1",
                "body": json.dumps({
                    "repo": "test-repo",
                    "repo_project": "owner/test-repo",
                    "repo_type": "application",
                    "all_files": ["h.py"],
                    "workspace_id": "ws-1",
                    "installation_id": "12345",
                    "discover_code_units": True,
                })
            }]
        }

        result = rsh.handler(event, None)
        # Should succeed but with empty code units
        assert result["batchItemFailures"] == []
        mock_send.assert_not_called()

    @patch.object(rsh, 'load_config')
    @patch.object(rsh, 'generate_architectural_summary')
    @patch.object(rsh, 'generate_embedding')
    @patch.object(rsh, 'store_code_map_embedding')
    @patch.object(rsh, 's3_client')
    def test_handler_multiple_records_partial_failure(
        self, mock_s3, mock_store, mock_embed, mock_summary, mock_config
    ):
        """Test handler handles partial batch failures."""
        # First record succeeds, second fails
        mock_summary.side_effect = ["Summary", Exception("Bedrock down")]
        mock_embed.return_value = [0.1] * 1024

        event = {
            "Records": [
                {
                    "messageId": "msg-1",
                    "receiptHandle": "handle-1",
                    "body": json.dumps({
                        "repo": "repo1",
                        "repo_project": "owner/repo1",
                        "repo_type": "app",
                        "all_files": ["f1.py"],
                        "code_units": []
                    })
                },
                {
                    "messageId": "msg-2",
                    "receiptHandle": "handle-2",
                    "body": json.dumps({
                        "repo": "repo2",
                        "repo_project": "owner/repo2",
                        "repo_type": "app",
                        "all_files": ["f2.py"],
                        "code_units": []
                    })
                }
            ]
        }

        result = rsh.handler(event, None)
        assert len(result["batchItemFailures"]) == 1
        assert result["batchItemFailures"][0]["itemIdentifier"] == "msg-2"


# ############################################################################
# load_config TESTS
# ############################################################################


class TestLoadConfig:
    """Test SSM configuration loading."""

    @patch.object(rsh, 'ssm_client')
    def test_load_config_success(self, mock_ssm):
        """Test successful configuration loading."""
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "test-value"}
        }

        rsh.load_config()

        assert rsh.CODE_MAPS_TABLE == "test-value"
        assert rsh.KB_BUCKET == "test-value"
        assert mock_ssm.get_parameter.call_count == 6  # 6 SSM parameters

    @patch.object(rsh, 'ssm_client')
    def test_load_config_failure(self, mock_ssm):
        """Test configuration loading failure raises."""
        mock_ssm.get_parameter.side_effect = _make_client_error(
            {"Error": {"Code": "ParameterNotFound", "Message": "not found"}},
            "GetParameter"
        )

        with pytest.raises(Exception):
            rsh.load_config()


# ############################################################################
# get_github_app_config TESTS
# ############################################################################


class TestGetGithubAppConfig:
    """Test GitHub App configuration retrieval."""

    @patch.object(rsh, 'ssm_client')
    def test_get_config_success(self, mock_ssm):
        """Test successful GitHub App config retrieval."""
        mock_ssm.get_parameters.return_value = {
            "Parameters": [
                {"Name": f"/{rsh.ENVIRONMENT}/{rsh.APP_NAME}/github/app-id", "Value": "12345"},
                {"Name": f"/{rsh.ENVIRONMENT}/{rsh.APP_NAME}/github/private-key", "Value": "key-data"},
            ]
        }

        result = rsh.get_github_app_config()
        assert result["app_id"] == "12345"
        assert result["private_key"] == "key-data"

    @patch.object(rsh, 'ssm_client')
    def test_get_config_failure(self, mock_ssm):
        """Test GitHub App config retrieval failure."""
        mock_ssm.get_parameters.side_effect = _make_client_error(
            {"Error": {"Code": "AccessDenied", "Message": "denied"}},
            "GetParameters"
        )

        with pytest.raises(Exception):
            rsh.get_github_app_config()
