"""Unit tests for generate-code-maps Lambda handler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
import sys
import os
import importlib.util

# Load the generate-code-maps handler module
handler_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/handler.py')
spec = importlib.util.spec_from_file_location("generate_code_maps_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['generate_code_maps_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from the loaded module
compute_content_hash = handler_module.compute_content_hash
generate_architectural_summary = handler_module.generate_architectural_summary
generate_embedding = handler_module.generate_embedding
group_files_into_batches = handler_module.group_files_into_batches
has_recent_commits = handler_module.has_recent_commits
identify_key_files = handler_module.identify_key_files
send_batch_to_sqs = handler_module.send_batch_to_sqs
store_code_map_embedding = handler_module.store_code_map_embedding


class TestComputeContentHash:
    """Test content hash computation."""

    def test_compute_content_hash_valid_content(self):
        """Test computing hash for valid content."""
        # Arrange
        content = "Test content for hashing"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Act
        result = compute_content_hash(content)

        # Assert
        assert result == expected_hash
        assert len(result) == 64  # SHA-256 produces 64 hex characters

    def test_compute_content_hash_empty_content(self):
        """Test computing hash for empty content."""
        # Arrange
        content = ""
        expected_hash = hashlib.sha256("".encode("utf-8")).hexdigest()

        # Act
        result = compute_content_hash(content)

        # Assert
        assert result == expected_hash


class TestIdentifyKeyFiles:
    """Test key file identification."""

    def test_identify_key_files_lambda_handlers(self):
        """Test identifying Lambda handler files with highest priority."""
        # Arrange
        files = [
            {"path": "lambda/my-handler/handler.py", "type": "blob"},
            {"path": "lambda/another-handler/handler.py", "type": "blob"},
            {"path": "lambda/my-handler/utils.py", "type": "blob"},
        ]

        # Act
        result = identify_key_files(files)

        # Assert
        assert len(result) == 3
        # Handler files should come first (priority 1)
        assert result[0]["path"] == "lambda/another-handler/handler.py"
        assert result[1]["path"] == "lambda/my-handler/handler.py"
        # Utils come second (priority 2)
        assert result[2]["path"] == "lambda/my-handler/utils.py"

    def test_identify_key_files_terraform(self):
        """Test identifying Terraform files."""
        # Arrange
        files = [
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "terraform/lambda.tf", "type": "blob"},
            {"path": "terraform/variables.tf", "type": "blob"},
        ]

        # Act
        result = identify_key_files(files)

        # Assert
        assert len(result) == 3
        assert all(f["path"].endswith(".tf") for f in result)

    def test_identify_key_files_tests(self):
        """Test identifying test files."""
        # Arrange
        files = [
            {"path": "tests/unit/test_handler.py", "type": "blob"},
            {"path": "tests/integration/test_flow.py", "type": "blob"},
            {"path": "lambda/my-handler/test_utils.py", "type": "blob"},
        ]

        # Act
        result = identify_key_files(files)

        # Assert
        assert len(result) == 3
        assert all("test" in f["path"] for f in result)

    def test_identify_key_files_excludes_directories(self):
        """Test that excluded directories are filtered out."""
        # Arrange
        files = [
            {"path": "lambda/handler/handler.py", "type": "blob"},
            {"path": "node_modules/package/index.js", "type": "blob"},
            {"path": "__pycache__/handler.cpython-312.pyc", "type": "blob"},
            {"path": ".git/config", "type": "blob"},
            {"path": "dist/bundle.js", "type": "blob"},
        ]

        # Act
        result = identify_key_files(files)

        # Assert
        assert len(result) == 1
        assert result[0]["path"] == "lambda/handler/handler.py"

    def test_identify_key_files_documentation(self):
        """Test identifying documentation files."""
        # Arrange
        files = [
            {"path": "README.md", "type": "blob"},
            {"path": "docs/architecture.md", "type": "blob"},
            {"path": "docs/adr/ADR-001.md", "type": "blob"},
        ]

        # Act
        result = identify_key_files(files)

        # Assert
        assert len(result) == 3
        assert all(f["path"].endswith(".md") for f in result)


class TestGroupFilesIntoBatches:
    """Test file grouping into batches."""

    def test_group_files_infrastructure_batch(self):
        """Test grouping Terraform files into infrastructure batch."""
        # Arrange
        files = [
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "terraform/lambda.tf", "type": "blob"},
            {"path": "terraform/variables.tf", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        infra_batches = [b for b in result if b["batch_type"] == "infrastructure"]
        assert len(infra_batches) == 1
        assert infra_batches[0]["group_name"] == "infrastructure"
        assert len(infra_batches[0]["files"]) == 3
        assert infra_batches[0]["storage_key"] == "summary#infrastructure"

    def test_group_files_handler_groups(self):
        """Test grouping Lambda handler files by function directory."""
        # Arrange
        files = [
            {"path": "lambda/ingest-docs/handler.py", "type": "blob"},
            {"path": "lambda/ingest-docs/utils.py", "type": "blob"},
            {"path": "lambda/generate-code-maps/handler.py", "type": "blob"},
            {"path": "lambda/generate-code-maps/processor.py", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        handler_batches = [b for b in result if b["batch_type"] == "handler-group"]
        assert len(handler_batches) == 2

        # Check ingest-docs batch
        ingest_batch = next(b for b in handler_batches if b["group_name"] == "ingest-docs")
        assert len(ingest_batch["files"]) == 2
        assert ingest_batch["storage_key"] == "summary#handler#ingest-docs"

        # Check generate-code-maps batch
        gen_batch = next(b for b in handler_batches if b["group_name"] == "generate-code-maps")
        assert len(gen_batch["files"]) == 2
        assert gen_batch["storage_key"] == "summary#handler#generate-code-maps"

    def test_group_files_test_batches(self):
        """Test grouping test files by type."""
        # Arrange
        files = [
            {"path": "tests/unit/test_handler.py", "type": "blob"},
            {"path": "tests/unit/test_utils.py", "type": "blob"},
            {"path": "tests/integration/test_flow.py", "type": "blob"},
            {"path": "tests/fixtures/sample_data.py", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        test_batches = [b for b in result if b["batch_type"] == "tests"]
        assert len(test_batches) >= 2

        # Check unit tests batch
        unit_batch = next((b for b in test_batches if b["group_name"] == "unit"), None)
        assert unit_batch is not None
        assert len(unit_batch["files"]) == 2

        # Check integration tests batch
        integration_batch = next((b for b in test_batches if b["group_name"] == "integration"), None)
        assert integration_batch is not None
        assert len(integration_batch["files"]) == 1

    def test_group_files_shared_utilities(self):
        """Test grouping shared utility files."""
        # Arrange
        files = [
            {"path": "src/utils/helpers.py", "type": "blob"},
            {"path": "src/common/validators.py", "type": "blob"},
            {"path": "shared/config.py", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        shared_batches = [b for b in result if b["batch_type"] == "shared"]
        assert len(shared_batches) == 1
        assert shared_batches[0]["group_name"] == "shared-utilities"
        assert len(shared_batches[0]["files"]) == 3

    def test_group_files_schemas(self):
        """Test grouping schema files."""
        # Arrange
        files = [
            {"path": "lambda/ingest-docs/schema.py", "type": "blob"},
            {"path": "src/models/user_model.py", "type": "blob"},
            {"path": "shared/schemas/request_schema.py", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        schema_batches = [b for b in result if b["batch_type"] == "schemas"]
        assert len(schema_batches) == 1
        assert schema_batches[0]["group_name"] == "schemas-and-models"
        assert len(schema_batches[0]["files"]) == 3

    def test_group_files_documentation(self):
        """Test grouping documentation files."""
        # Arrange
        files = [
            {"path": "README.md", "type": "blob"},
            {"path": "docs/architecture.md", "type": "blob"},
            {"path": "docs/deployment.md", "type": "blob"},
        ]

        # Act
        result = group_files_into_batches(files)

        # Assert
        doc_batches = [b for b in result if b["batch_type"] == "docs"]
        assert len(doc_batches) == 1
        assert doc_batches[0]["group_name"] == "documentation"
        assert len(doc_batches[0]["files"]) == 3


class TestGenerateArchitecturalSummary:
    """Test architectural summary generation."""

    @patch("generate_code_maps_handler.bedrock_client")
    def test_generate_architectural_summary_success(self, mock_bedrock):
        """Test successful architectural summary generation."""
        # Arrange
        repo = "outcome-ops-ai-assist"
        repo_type = "application"
        files = [
            {"path": "lambda/handler/handler.py", "type": "blob"},
            {"path": "terraform/main.tf", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ]

        mock_summary = "This is a serverless application using AWS Lambda and Terraform..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_architectural_summary(repo, repo_type, files)

        # Assert
        assert result == mock_summary
        mock_bedrock.converse.assert_called_once()
        call_kwargs = mock_bedrock.converse.call_args[1]
        assert call_kwargs["modelId"] == "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

    @patch("generate_code_maps_handler.bedrock_client")
    def test_generate_architectural_summary_bedrock_error(self, mock_bedrock):
        """Test handling Bedrock error."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_bedrock.converse.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "Converse"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            generate_architectural_summary("repo", "application", [])


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch("generate_code_maps_handler.bedrock_client")
    def test_generate_embedding_success(self, mock_bedrock):
        """Test successful embedding generation."""
        # Arrange
        text = "Test text for embedding"
        mock_embedding = [0.1] * 1024
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": mock_embedding}).encode())
        }

        # Act
        result = generate_embedding(text)

        # Assert
        assert result == mock_embedding
        assert len(result) == 1024
        mock_bedrock.invoke_model.assert_called_once()

    @patch("generate_code_maps_handler.bedrock_client")
    def test_generate_embedding_empty_response(self, mock_bedrock):
        """Test handling empty embedding response."""
        # Arrange
        text = "Test text"
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": []}).encode())
        }

        # Act
        result = generate_embedding(text)

        # Assert
        assert result == []


class TestStoreCodeMapEmbedding:
    """Test storing code map embeddings in DynamoDB."""

    @patch("generate_code_maps_handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("generate_code_maps_handler.dynamodb_client")
    def test_store_code_map_embedding_success(self, mock_dynamodb):
        """Test successful code map storage."""
        # Arrange
        doc_id = "outcome-ops-ai-assist/code-map"
        content = "Architectural summary..."
        content_hash = "abc123"
        embedding = [0.1] * 1024
        repo = "outcome-ops-ai-assist"
        doc_type = "code-map"
        path = "code-map"

        mock_dynamodb.put_item.return_value = {}

        # Act
        result = store_code_map_embedding(
            doc_id, content, content_hash, embedding, repo, doc_type, path
        )

        # Assert
        assert result is True
        mock_dynamodb.put_item.assert_called_once()
        call_kwargs = mock_dynamodb.put_item.call_args[1]
        assert call_kwargs["TableName"] == "dev-outcome-ops-ai-assist-code-maps"
        assert call_kwargs["Item"]["PK"]["S"] == doc_id
        assert call_kwargs["Item"]["SK"]["S"] == "METADATA"
        assert call_kwargs["Item"]["repo"]["S"] == repo
        assert call_kwargs["Item"]["path"]["S"] == path
        assert call_kwargs["Item"]["content_hash"]["S"] == content_hash
        assert "timestamp" in call_kwargs["Item"]

    @patch("generate_code_maps_handler.dynamodb_client")
    def test_store_code_map_embedding_failure(self, mock_dynamodb):
        """Test handling DynamoDB storage failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "PutItem"
        )

        # Act
        result = store_code_map_embedding(
            "doc-id", "content", "hash", [0.1] * 1024, "repo", "code-map", "code-map"
        )

        # Assert
        assert result is False


class TestSendBatchToSQS:
    """Test sending batches to SQS."""

    @patch("generate_code_maps_handler.SQS_QUEUE_URL", "https://sqs.us-west-2.amazonaws.com/123456789012/code-maps-queue.fifo")
    @patch("generate_code_maps_handler.sqs_client")
    def test_send_batch_to_sqs_success(self, mock_sqs):
        """Test successful batch send to SQS."""
        # Arrange
        batch = {
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "files": [{"path": "lambda/ingest-docs/handler.py"}],
            "storage_key": "summary#handler#ingest-docs",
        }
        repo = "outcome-ops-ai-assist"
        repo_project = "bcarpio/outcome-ops-ai-assist"

        mock_sqs.send_message.return_value = {"MessageId": "test-message-id"}

        # Act
        result = send_batch_to_sqs(batch, repo, repo_project)

        # Assert
        assert result is True
        mock_sqs.send_message.assert_called_once()
        call_kwargs = mock_sqs.send_message.call_args[1]
        assert call_kwargs["MessageGroupId"] == repo
        assert call_kwargs["MessageDeduplicationId"] == f"{repo}-{batch['storage_key']}"

    @patch("generate_code_maps_handler.sqs_client")
    def test_send_batch_to_sqs_failure(self, mock_sqs):
        """Test handling SQS send failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_sqs.send_message.side_effect = ClientError(
            {"Error": {"Code": "QueueDoesNotExist"}}, "SendMessage"
        )

        batch = {
            "batch_type": "infrastructure",
            "group_name": "infrastructure",
            "files": [],
            "storage_key": "summary#infrastructure",
        }

        # Act
        result = send_batch_to_sqs(batch, "repo", "owner/repo")

        # Assert
        assert result is False


class TestHasRecentCommits:
    """Test recent commit checking."""

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_true(self, mock_api):
        """Test repo with recent commits."""
        # Arrange
        from datetime import datetime, timezone

        recent_date = datetime.now(timezone.utc).isoformat()
        mock_api.return_value = {
            "commit": {
                "commit": {
                    "committer": {"date": recent_date}
                }
            }
        }

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is True

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_false(self, mock_api):
        """Test repo without recent commits."""
        # Arrange
        from datetime import datetime, timezone, timedelta

        old_date = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
        mock_api.return_value = {
            "commit": {
                "commit": {
                    "committer": {"date": old_date}
                }
            }
        }

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is False

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_error_returns_true(self, mock_api):
        """Test that errors result in True (fail open)."""
        # Arrange
        mock_api.side_effect = Exception("API Error")

        # Act
        result = has_recent_commits("owner/repo")

        # Assert
        assert result is True  # Fail open: process repo if we can't determine
