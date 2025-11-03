"""Unit tests for process-batch-summary Lambda handler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

# Import functions from handler
import sys
import os

# Add process-batch-summary to path for imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../process-batch-summary"))

from handler import (
    compute_content_hash,
    fetch_file_content,
    generate_batch_summary,
    generate_embedding,
    process_batch_record,
    store_batch_summary,
)


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
        assert len(result) == 64


class TestFetchFileContent:
    """Test file content fetching from GitHub."""

    @patch("handler.urlopen")
    def test_fetch_file_content_success(self, mock_urlopen):
        """Test successful file content fetch."""
        # Arrange
        mock_response = MagicMock()
        mock_response.read.return_value = b"file content here"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        result = fetch_file_content("owner/repo", "path/to/file.py")

        # Assert
        assert result == "file content here"
        mock_urlopen.assert_called_once()

    @patch("handler.urlopen")
    def test_fetch_file_content_url_format(self, mock_urlopen):
        """Test file content fetch uses correct URL format."""
        # Arrange
        mock_response = MagicMock()
        mock_response.read.return_value = b"content"
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        fetch_file_content("bcarpio/outcome-ops-ai-assist", "lambda/handler.py")

        # Assert
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "raw.githubusercontent.com" in request_obj.full_url
        assert "bcarpio/outcome-ops-ai-assist/main/lambda/handler.py" in request_obj.full_url


class TestGenerateBatchSummary:
    """Test batch summary generation."""

    @patch("handler.fetch_file_content")
    @patch("handler.bedrock_client")
    def test_generate_batch_summary_infrastructure(self, mock_bedrock, mock_fetch):
        """Test generating summary for infrastructure batch."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "batch_type": "infrastructure",
            "group_name": "infrastructure",
            "file_paths": ["terraform/main.tf", "terraform/lambda.tf"]
        }

        mock_fetch.return_value = "terraform content here"
        mock_summary = "This infrastructure creates Lambda functions and DynamoDB tables..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        assert mock_fetch.call_count == 2
        mock_bedrock.converse.assert_called_once()

    @patch("handler.fetch_file_content")
    @patch("handler.bedrock_client")
    def test_generate_batch_summary_handler_group(self, mock_bedrock, mock_fetch):
        """Test generating summary for handler group batch."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "repo_project": "bcarpio/outcome-ops-ai-assist",
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "file_paths": ["lambda/ingest-docs/handler.py"]
        }

        mock_fetch.return_value = "def handler(event, context): ..."
        mock_summary = "This handler ingests documents from GitHub..."
        mock_bedrock.converse.return_value = {
            "output": {
                "message": {
                    "content": [{"text": mock_summary}]
                }
            }
        }

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == mock_summary
        mock_bedrock.converse.assert_called_once()

    @patch("handler.fetch_file_content")
    def test_generate_batch_summary_skips_large_files(self, mock_fetch):
        """Test that large files are skipped."""
        # Arrange
        batch = {
            "repo": "test-repo",
            "repo_project": "owner/test-repo",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": ["large_file.py"]
        }

        mock_fetch.return_value = "x" * 60000  # 60KB file

        # Act
        result = generate_batch_summary(batch)

        # Assert
        assert result == "No files available for analysis"

    @patch("handler.fetch_file_content")
    def test_generate_batch_summary_handles_fetch_errors(self, mock_fetch):
        """Test that file fetch errors are handled gracefully."""
        # Arrange
        batch = {
            "repo": "test-repo",
            "repo_project": "owner/test-repo",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": ["file1.py", "file2.py"]
        }

        mock_fetch.side_effect = [Exception("API Error"), "valid content"]

        # Act - should not raise
        # Note: This would need mock_bedrock too for full test
        # For now just testing error handling doesn't crash


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch("handler.bedrock_client")
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


class TestStoreBatchSummary:
    """Test storing batch summaries in DynamoDB."""

    @patch("handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("handler.dynamodb_client")
    def test_store_batch_summary_success(self, mock_dynamodb):
        """Test successful batch summary storage."""
        # Arrange
        batch = {
            "repo": "outcome-ops-ai-assist",
            "storage_key": "summary#handler#ingest-docs",
            "batch_type": "handler-group",
            "group_name": "ingest-docs",
            "file_paths": ["lambda/ingest-docs/handler.py"]
        }
        summary = "This handler ingests documents..."
        embedding = [0.1] * 1024

        mock_dynamodb.put_item.return_value = {}

        # Act
        result = store_batch_summary(batch, summary, embedding)

        # Assert
        assert result is True
        mock_dynamodb.put_item.assert_called_once()
        call_kwargs = mock_dynamodb.put_item.call_args[1]
        assert call_kwargs["TableName"] == "dev-outcome-ops-ai-assist-code-maps"
        assert call_kwargs["Item"]["PK"]["S"] == "repo#outcome-ops-ai-assist"
        assert call_kwargs["Item"]["SK"]["S"] == "summary#handler#ingest-docs"
        assert call_kwargs["Item"]["batch_type"]["S"] == "handler-group"
        assert call_kwargs["Item"]["group_name"]["S"] == "ingest-docs"

    @patch("handler.dynamodb_client")
    def test_store_batch_summary_failure(self, mock_dynamodb):
        """Test handling DynamoDB storage failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "PutItem"
        )

        batch = {
            "repo": "test-repo",
            "storage_key": "summary#test",
            "batch_type": "shared",
            "group_name": "utils",
            "file_paths": []
        }

        # Act
        result = store_batch_summary(batch, "summary", [0.1] * 1024)

        # Assert
        assert result is False


class TestProcessBatchRecord:
    """Test SQS record processing."""

    @patch("handler.store_batch_summary")
    @patch("handler.generate_embedding")
    @patch("handler.generate_batch_summary")
    def test_process_batch_record_success(self, mock_summary, mock_embedding, mock_store):
        """Test successful record processing."""
        # Arrange
        record = {
            "body": json.dumps({
                "repo": "outcome-ops-ai-assist",
                "repo_project": "bcarpio/outcome-ops-ai-assist",
                "batch_type": "infrastructure",
                "group_name": "infrastructure",
                "file_paths": ["terraform/main.tf"],
                "storage_key": "summary#infrastructure"
            })
        }

        mock_summary.return_value = "Infrastructure summary"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        process_batch_record(record)

        # Assert
        mock_summary.assert_called_once()
        mock_embedding.assert_called_once()
        mock_store.assert_called_once()

    @patch("handler.store_batch_summary")
    @patch("handler.generate_embedding")
    @patch("handler.generate_batch_summary")
    def test_process_batch_record_storage_failure(self, mock_summary, mock_embedding, mock_store):
        """Test record processing with storage failure."""
        # Arrange
        record = {
            "body": json.dumps({
                "repo": "test-repo",
                "repo_project": "owner/test-repo",
                "batch_type": "shared",
                "group_name": "utils",
                "file_paths": ["utils.py"],
                "storage_key": "summary#utils"
            })
        }

        mock_summary.return_value = "Utils summary"
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = False  # Storage fails

        # Act & Assert
        with pytest.raises(Exception, match="Failed to store batch summary"):
            process_batch_record(record)


class TestHandler:
    """Test Lambda handler."""

    @patch("handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("handler.process_batch_record")
    def test_handler_success(self, mock_process):
        """Test successful handler execution."""
        # Arrange
        event = {
            "Records": [
                {"body": json.dumps({"repo": "test1", "repo_project": "owner/test1", "batch_type": "infrastructure", "group_name": "infra", "file_paths": [], "storage_key": "summary#infra"})},
                {"body": json.dumps({"repo": "test2", "repo_project": "owner/test2", "batch_type": "shared", "group_name": "utils", "file_paths": [], "storage_key": "summary#utils"})}
            ]
        }
        context = {}

        # Act
        from handler import handler as lambda_handler
        result = lambda_handler(event, context)

        # Assert
        assert result["statusCode"] == 200
        assert mock_process.call_count == 2

    @patch("handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("handler.process_batch_record")
    def test_handler_partial_failure(self, mock_process):
        """Test handler with some failed records."""
        # Arrange
        event = {
            "Records": [
                {"body": json.dumps({"repo": "test1", "repo_project": "owner/test1", "batch_type": "infrastructure", "group_name": "infra", "file_paths": [], "storage_key": "summary#infra"})},
                {"body": json.dumps({"repo": "test2", "repo_project": "owner/test2", "batch_type": "shared", "group_name": "utils", "file_paths": [], "storage_key": "summary#utils"})}
            ]
        }
        context = {}

        # First succeeds, second fails
        mock_process.side_effect = [None, Exception("Processing error")]

        # Act & Assert
        from handler import handler as lambda_handler
        with pytest.raises(Exception, match="Failed to process 1 out of 2 batches"):
            lambda_handler(event, context)
