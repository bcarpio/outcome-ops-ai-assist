"""Unit tests for ingest-docs Lambda handler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest

from handler import (
    compute_content_hash,
    generate_embedding,
    github_api_raw_content,
    github_api_request,
    ingest_adr,
    ingest_readme,
    store_in_dynamodb,
    upload_to_s3,
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

    def test_compute_content_hash_unicode_content(self):
        """Test computing hash for unicode content."""
        # Arrange
        content = "Test content with émojis 🎉"
        expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        # Act
        result = compute_content_hash(content)

        # Assert
        assert result == expected_hash


class TestGenerateEmbedding:
    """Test embedding generation."""

    @patch("handler.bedrock_client")
    def test_generate_embedding_valid_response(self, mock_bedrock):
        """Test generating embedding with valid Bedrock response."""
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

    @patch("handler.bedrock_client")
    def test_generate_embedding_empty_response(self, mock_bedrock):
        """Test generating embedding with empty response from Bedrock."""
        # Arrange
        text = "Test text"
        mock_bedrock.invoke_model.return_value = {
            "body": MagicMock(read=lambda: json.dumps({"embedding": []}).encode())
        }

        # Act
        result = generate_embedding(text)

        # Assert
        assert result == []

    @patch("handler.bedrock_client")
    def test_generate_embedding_bedrock_error(self, mock_bedrock):
        """Test handling Bedrock client error."""
        # Arrange
        text = "Test text"
        from botocore.exceptions import ClientError

        mock_bedrock.invoke_model.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException"}}, "InvokeModel"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            generate_embedding(text)


class TestUploadToS3:
    """Test S3 upload functionality."""

    @patch("handler.KB_BUCKET", "dev-outcome-ops-ai-assist-kb")
    @patch("handler.s3_client")
    def test_upload_to_s3_success(self, mock_s3):
        """Test successful S3 upload."""
        # Arrange
        file_key = "adr/ADR-001.md"
        content = "# ADR-001\nTest content"
        file_path = "docs/adr/ADR-001.md"
        mock_s3.put_object.return_value = {}

        # Act
        result = upload_to_s3(file_key, content, file_path)

        # Assert
        assert result is True
        mock_s3.put_object.assert_called_once()
        call_kwargs = mock_s3.put_object.call_args[1]
        assert call_kwargs["Bucket"] == "dev-outcome-ops-ai-assist-kb"
        assert call_kwargs["Key"] == file_key

    @patch("handler.s3_client")
    def test_upload_to_s3_failure(self, mock_s3):
        """Test S3 upload failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_s3.put_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchBucket"}}, "PutObject"
        )

        # Act
        result = upload_to_s3("test.md", "content", "test.md")

        # Assert
        assert result is False


class TestStoreInDynamoDB:
    """Test DynamoDB storage."""

    @patch("handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("handler.dynamodb_client")
    def test_store_in_dynamodb_success(self, mock_dynamodb):
        """Test successful DynamoDB storage."""
        # Arrange
        pk = "repo#outcome-ops-ai-assist"
        sk = "adr#ADR-001"
        doc_type = "adr"
        content = "Test content"
        embedding = [0.1] * 1024
        file_path = "docs/adr/ADR-001.md"
        content_hash = "abc123"
        repo = "outcome-ops-ai-assist"
        mock_dynamodb.put_item.return_value = {}

        # Act
        result = store_in_dynamodb(pk, sk, doc_type, content, embedding, file_path, content_hash, repo)

        # Assert
        assert result is True
        mock_dynamodb.put_item.assert_called_once()
        call_kwargs = mock_dynamodb.put_item.call_args[1]
        assert call_kwargs["TableName"] == "dev-outcome-ops-ai-assist-code-maps"
        assert call_kwargs["Item"]["PK"]["S"] == pk
        assert call_kwargs["Item"]["SK"]["S"] == sk
        assert call_kwargs["Item"]["repo"]["S"] == repo

    @patch("handler.dynamodb_client")
    def test_store_in_dynamodb_failure(self, mock_dynamodb):
        """Test DynamoDB storage failure."""
        # Arrange
        from botocore.exceptions import ClientError

        mock_dynamodb.put_item.side_effect = ClientError(
            {"Error": {"Code": "ValidationException"}}, "PutItem"
        )

        # Act
        result = store_in_dynamodb(
            "pk", "sk", "adr", "content", [0.1] * 1024, "file.md", "hash", "test-repo"
        )

        # Assert
        assert result is False


class TestIngestAdr:
    """Test ADR ingestion."""

    @patch("handler.store_in_dynamodb")
    @patch("handler.generate_embedding")
    @patch("handler.upload_to_s3")
    def test_ingest_adr_success(self, mock_upload, mock_embedding, mock_store):
        """Test successful ADR ingestion."""
        # Arrange
        adr_path = "docs/adr/ADR-001-test.md"
        content = "# ADR-001\nTest ADR content"
        mock_upload.return_value = True
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        result = ingest_adr("outcome-ops-ai-assist", adr_path, content)

        # Assert
        assert result is True
        mock_upload.assert_called_once()
        mock_embedding.assert_called_once()
        mock_store.assert_called_once()

    @patch("handler.upload_to_s3")
    def test_ingest_adr_upload_failure(self, mock_upload):
        """Test ADR ingestion with upload failure."""
        # Arrange
        mock_upload.return_value = False

        # Act
        result = ingest_adr("repo", "docs/adr/ADR-001.md", "content")

        # Assert
        assert result is False

    @patch("handler.store_in_dynamodb")
    @patch("handler.generate_embedding")
    @patch("handler.upload_to_s3")
    def test_ingest_adr_empty_embedding(self, mock_upload, mock_embedding, mock_store):
        """Test ADR ingestion with empty embedding."""
        # Arrange
        mock_upload.return_value = True
        mock_embedding.return_value = []

        # Act
        result = ingest_adr("repo", "docs/adr/ADR-001.md", "content")

        # Assert
        assert result is False
        mock_store.assert_not_called()


class TestIngestReadme:
    """Test README ingestion."""

    @patch("handler.store_in_dynamodb")
    @patch("handler.generate_embedding")
    @patch("handler.upload_to_s3")
    def test_ingest_readme_root(self, mock_upload, mock_embedding, mock_store):
        """Test ingesting root README."""
        # Arrange
        readme_path = "README.md"
        content = "# Project Documentation"
        mock_upload.return_value = True
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        result = ingest_readme("repo", readme_path, content)

        # Assert
        assert result is True
        call_kwargs = mock_store.call_args[0]
        assert call_kwargs[1] == "readme#root"  # SK should be readme#root
        mock_upload.assert_called_once()

    @patch("handler.store_in_dynamodb")
    @patch("handler.generate_embedding")
    @patch("handler.upload_to_s3")
    def test_ingest_readme_subdirectory(self, mock_upload, mock_embedding, mock_store):
        """Test ingesting README from subdirectory."""
        # Arrange
        readme_path = "docs/README.md"
        content = "# Documentation"
        mock_upload.return_value = True
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        result = ingest_readme("repo", readme_path, content)

        # Assert
        assert result is True
        call_kwargs = mock_store.call_args[0]
        assert call_kwargs[1] == "readme#docs"  # SK should be readme#docs


class TestGithubApiRequest:
    """Test GitHub API request handling."""

    @patch("handler.urlopen")
    def test_github_api_request_success(self, mock_urlopen):
        """Test successful GitHub API request."""
        # Arrange
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([{"name": "file.md"}]).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        result = github_api_request("/repos/owner/repo/contents/dir")

        # Assert
        assert isinstance(result, list)
        assert result[0]["name"] == "file.md"

    @patch("handler.urlopen")
    def test_github_api_request_with_token(self, mock_urlopen):
        """Test GitHub API request includes authorization header."""
        # Arrange
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps([]).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_response

        # Act
        github_api_request("/repos/owner/repo")

        # Assert
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert "Authorization" in request_obj.headers
        assert "Bearer" in request_obj.headers["Authorization"]
