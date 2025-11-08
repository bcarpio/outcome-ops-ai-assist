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

# Load the backends module for CodeUnit
backends_base_path = os.path.join(os.path.dirname(__file__), '../../generate-code-maps/backends/base.py')
spec_base = importlib.util.spec_from_file_location("backends_base", backends_base_path)
backends_base = importlib.util.module_from_spec(spec_base)
spec_base.loader.exec_module(backends_base)
CodeUnit = backends_base.CodeUnit

# Import functions from the loaded module
# Note: Some functions (identify_key_files, group_files_into_batches, send_batch_to_sqs)
# were removed in favor of backend abstraction. Tests for those are in test_lambda_backend.py
compute_content_hash = handler_module.compute_content_hash
generate_architectural_summary = handler_module.generate_architectural_summary
generate_embedding = handler_module.generate_embedding
send_code_unit_to_sqs = handler_module.send_code_unit_to_sqs
store_code_map_embedding = handler_module.store_code_map_embedding
has_recent_commits = handler_module.has_recent_commits
filter_changed_code_units = handler_module.filter_changed_code_units


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


# NOTE: Tests for identify_key_files, group_files_into_batches, has_recent_commits,
# and send_batch_to_sqs were removed as these functions are now handled by the backend
# abstraction layer. See test_lambda_backend.py for equivalent tests.


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


class TestHasRecentCommits:
    """Test checking for recent commits in repositories."""

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_within_window(self, mock_api):
        """Test repo with commits within the time window."""
        # Arrange
        from datetime import datetime, timezone
        recent_time = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        mock_api.return_value = {
            "commit": {
                "commit": {
                    "committer": {
                        "date": recent_time
                    }
                }
            }
        }

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is True
        mock_api.assert_called_once_with("/repos/owner/repo/branches/main")

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_outside_window(self, mock_api):
        """Test repo with commits outside the time window."""
        # Arrange
        from datetime import datetime, timedelta, timezone
        old_time = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

        mock_api.return_value = {
            "commit": {
                "commit": {
                    "committer": {
                        "date": old_time
                    }
                }
            }
        }

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is False

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_no_date(self, mock_api):
        """Test handling missing commit date (process anyway)."""
        # Arrange
        mock_api.return_value = {
            "commit": {
                "commit": {
                    "committer": {}
                }
            }
        }

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is True  # Process anyway if we can't determine

    @patch("generate_code_maps_handler.github_api_request")
    def test_has_recent_commits_api_error(self, mock_api):
        """Test handling API error (process anyway)."""
        # Arrange
        mock_api.side_effect = Exception("API error")

        # Act
        result = has_recent_commits("owner/repo", minutes_ago=61)

        # Assert
        assert result is True  # Process anyway on error


class TestFilterChangedCodeUnits:
    """Test filtering code units to only changed ones."""

    def test_filter_changed_code_units_with_changes(self):
        """Test filtering when files have changed."""
        # Arrange
        unit1 = CodeUnit(
            name="handler1",
            unit_type="lambda-handler",
            file_paths=["lambda/handler1/handler.py", "lambda/handler1/utils.py"]
        )
        unit2 = CodeUnit(
            name="handler2",
            unit_type="lambda-handler",
            file_paths=["lambda/handler2/handler.py"]
        )
        unit3 = CodeUnit(
            name="infrastructure",
            unit_type="infrastructure",
            file_paths=["terraform/lambda.tf", "terraform/main.tf"]
        )

        all_units = [unit1, unit2, unit3]
        changed_files = ["lambda/handler1/utils.py", "terraform/lambda.tf"]

        # Act
        result = filter_changed_code_units(all_units, changed_files)

        # Assert
        assert len(result) == 2
        assert unit1 in result  # handler1 has changed file
        assert unit2 not in result  # handler2 has no changed files
        assert unit3 in result  # infrastructure has changed file

    def test_filter_changed_code_units_no_changes(self):
        """Test filtering when no files have changed."""
        # Arrange
        unit1 = CodeUnit(
            name="handler1",
            unit_type="lambda-handler",
            file_paths=["lambda/handler1/handler.py"]
        )

        all_units = [unit1]
        changed_files = ["README.md"]  # No overlap with handler files

        # Act
        result = filter_changed_code_units(all_units, changed_files)

        # Assert
        assert len(result) == 0

    def test_filter_changed_code_units_empty_changed_files(self):
        """Test filtering with empty changed files list (full regeneration)."""
        # Arrange
        unit1 = CodeUnit(
            name="handler1",
            unit_type="lambda-handler",
            file_paths=["lambda/handler1/handler.py"]
        )

        all_units = [unit1]
        changed_files = []

        # Act
        result = filter_changed_code_units(all_units, changed_files)

        # Assert
        assert len(result) == 1  # Empty changed_files means return all
        assert result == all_units


# NOTE: TestSendBatchToSQS removed - function moved to backend abstraction
