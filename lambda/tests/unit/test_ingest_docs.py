"""Unit tests for ingest-docs Lambda handler."""

import hashlib
import json
from unittest.mock import MagicMock, patch

import pytest
import sys
import os
import importlib.util

# Load the ingest-docs handler module
handler_path = os.path.join(os.path.dirname(__file__), '../../ingest-docs/handler.py')
spec = importlib.util.spec_from_file_location("ingest_docs_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['ingest_docs_handler'] = handler_module
spec.loader.exec_module(handler_module)

# Import functions from the loaded module
compute_content_hash = handler_module.compute_content_hash
generate_embedding = handler_module.generate_embedding
github_api_raw_content = handler_module.github_api_raw_content
github_api_request = handler_module.github_api_request
ingest_adr = handler_module.ingest_adr
ingest_readme = handler_module.ingest_readme
ingest_doc = handler_module.ingest_doc
load_config = handler_module.load_config
store_in_dynamodb = handler_module.store_in_dynamodb
upload_to_s3 = handler_module.upload_to_s3
handler = handler_module.handler


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

    @patch("ingest_docs_handler.bedrock_client")
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

    @patch("ingest_docs_handler.bedrock_client")
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

    @patch("ingest_docs_handler.bedrock_client")
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

    @patch("ingest_docs_handler.KB_BUCKET", "dev-outcome-ops-ai-assist-kb")
    @patch("ingest_docs_handler.s3_client")
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

    @patch("ingest_docs_handler.s3_client")
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

    @patch("ingest_docs_handler.CODE_MAPS_TABLE", "dev-outcome-ops-ai-assist-code-maps")
    @patch("ingest_docs_handler.dynamodb_client")
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

    @patch("ingest_docs_handler.dynamodb_client")
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

    @patch("ingest_docs_handler.store_in_dynamodb")
    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
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

    @patch("ingest_docs_handler.upload_to_s3")
    def test_ingest_adr_upload_failure(self, mock_upload):
        """Test ADR ingestion with upload failure."""
        # Arrange
        mock_upload.return_value = False

        # Act
        result = ingest_adr("repo", "docs/adr/ADR-001.md", "content")

        # Assert
        assert result is False

    @patch("ingest_docs_handler.store_in_dynamodb")
    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
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

    @patch("ingest_docs_handler.store_in_dynamodb")
    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
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

    @patch("ingest_docs_handler.store_in_dynamodb")
    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
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

    @patch("ingest_docs_handler.urlopen")
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

    @patch("ingest_docs_handler.urlopen")
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


class TestLoadConfig:
    """Test configuration loading from SSM."""

    @patch("ingest_docs_handler.ssm_client")
    def test_load_config_success(self, mock_ssm):
        """Test successful configuration loading."""
        # Arrange
        mock_ssm.get_parameter.side_effect = [
            {"Parameter": {"Value": "test-kb-bucket"}},
            {"Parameter": {"Value": "test-code-maps-table"}},
            {"Parameter": {"Value": "ghp_test_token"}}
        ]

        # Act
        load_config()

        # Assert
        assert handler_module.KB_BUCKET == "test-kb-bucket"
        assert handler_module.CODE_MAPS_TABLE == "test-code-maps-table"
        assert handler_module.GITHUB_TOKEN == "ghp_test_token"
        assert mock_ssm.get_parameter.call_count == 3

    @patch("ingest_docs_handler.ssm_client")
    def test_load_config_failure(self, mock_ssm):
        """Test configuration loading failure."""
        # Arrange
        from botocore.exceptions import ClientError
        mock_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound"}}, "get_parameter"
        )

        # Act & Assert
        with pytest.raises(ClientError):
            load_config()


class TestIngestDoc:
    """Test generic document ingestion."""

    @patch("ingest_docs_handler.store_in_dynamodb")
    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
    def test_ingest_doc_success(self, mock_upload, mock_embedding, mock_store):
        """Test successful document ingestion."""
        # Arrange
        doc_path = "docs/architecture.md"
        content = "# Architecture\nTest documentation content"
        mock_upload.return_value = True
        mock_embedding.return_value = [0.1] * 1024
        mock_store.return_value = True

        # Act
        result = ingest_doc("repo", doc_path, content)

        # Assert
        assert result is True
        mock_upload.assert_called_once()
        mock_embedding.assert_called_once()
        mock_store.assert_called_once()

    @patch("ingest_docs_handler.upload_to_s3")
    def test_ingest_doc_upload_failure(self, mock_upload):
        """Test document ingestion with upload failure."""
        # Arrange
        mock_upload.return_value = False

        # Act
        result = ingest_doc("repo", "docs/test.md", "content")

        # Assert
        assert result is False

    @patch("ingest_docs_handler.generate_embedding")
    @patch("ingest_docs_handler.upload_to_s3")
    def test_ingest_doc_empty_embedding(self, mock_upload, mock_embedding):
        """Test document ingestion with empty embedding."""
        # Arrange
        mock_upload.return_value = True
        mock_embedding.return_value = []

        # Act
        result = ingest_doc("repo", "docs/test.md", "content")

        # Assert
        assert result is False


class TestHandler:
    """Test main Lambda handler."""

    @patch("ingest_docs_handler.KB_BUCKET", None)
    @patch("ingest_docs_handler.load_config")
    @patch("ingest_docs_handler.ssm_client")
    @patch("ingest_docs_handler.ingest_adr")
    @patch("ingest_docs_handler.ingest_readme")
    @patch("ingest_docs_handler.ingest_doc")
    @patch("ingest_docs_handler.list_directory_files")
    @patch("ingest_docs_handler.github_api_raw_content")
    def test_handler_success(
        self, mock_raw_content, mock_list_files, mock_ingest_doc,
        mock_ingest_readme, mock_ingest_adr, mock_ssm, mock_load_config
    ):
        """Test successful handler execution."""
        # Arrange
        event = {}
        context = {}

        # Mock SSM allowlist
        mock_ssm.get_parameter.return_value = {
            "Parameter": {
                "Value": json.dumps({
                    "repos": [
                        {
                            "name": "test-repo",
                            "project": "owner/test-repo",
                            "type": "standards"
                        }
                    ]
                })
            }
        }

        # Mock GitHub responses
        mock_list_files.side_effect = [
            ["docs/adr/ADR-001.md"],  # ADRs
            []  # docs directory
        ]
        mock_raw_content.return_value = "# Test Content"
        mock_ingest_adr.return_value = True
        mock_ingest_readme.return_value = True

        # Act
        response = handler(event, context)

        # Assert
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        assert "message" in body
        assert "documents_ingested" in body
        mock_load_config.assert_called_once()

    @patch("ingest_docs_handler.KB_BUCKET", "test-bucket")
    @patch("ingest_docs_handler.ssm_client")
    def test_handler_invalid_allowlist(self, mock_ssm):
        """Test handler with invalid allowlist structure."""
        # Arrange
        event = {}
        context = {}
        mock_ssm.get_parameter.return_value = {
            "Parameter": {"Value": json.dumps({"invalid": "structure"})}
        }

        # Act
        response = handler(event, context)

        # Assert
        assert response["statusCode"] == 400
        body = json.loads(response["body"])
        assert "Invalid allowlist structure" in body["error"]
