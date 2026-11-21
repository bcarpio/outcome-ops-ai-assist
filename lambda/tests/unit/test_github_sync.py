# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for github-sync Lambda handler."""

import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch, call

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

# Pre-mock boto3 clients before module import
mock_dynamodb_resource = MagicMock()
mock_sqs_client = MagicMock()
mock_ssm_client = MagicMock()
mock_s3_client = MagicMock()


def _load_handler_module():
    """Load or reload the github-sync handler module."""
    if "github_sync_handler" in sys.modules:
        del sys.modules["github_sync_handler"]

    # Add github-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), "../../github-sync")
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)

    handler_path = os.path.join(sync_dir, "handler.py")
    spec = importlib.util.spec_from_file_location("github_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["github_sync_handler"] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace cached boto3 clients with mocks
    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._sqs = mock_sqs_client
    handler_module._ssm = mock_ssm_client
    handler_module._s3 = mock_s3_client

    return handler_module


handler_module = _load_handler_module()

# Import functions under test
lambda_handler = handler_module.lambda_handler
process_sync_request = handler_module.process_sync_request
get_repo = handler_module.get_repo
update_sync_status = handler_module.update_sync_status
should_process_code_file = handler_module.should_process_code_file
should_process_doc_file = handler_module.should_process_doc_file
classify_doc_type = handler_module.classify_doc_type
fetch_ingestion_config = handler_module.fetch_ingestion_config
queue_file_for_ingestion = handler_module.queue_file_for_ingestion
upload_file_to_s3 = handler_module.upload_file_to_s3
queue_repo_for_code_maps = handler_module.queue_repo_for_code_maps
_process_standards_repo = handler_module._process_standards_repo
_process_application_repo = handler_module._process_application_repo


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests and clear cached config."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_ssm_client.reset_mock()
    mock_s3_client.reset_mock()
    # Clear side_effect on all nested mocks
    mock_sqs_client.send_message.side_effect = None
    mock_s3_client.put_object.side_effect = None
    mock_dynamodb_resource.Table.return_value.get_item.side_effect = None
    mock_dynamodb_resource.Table.return_value.update_item.side_effect = None
    # Clear cached GitHub config
    handler_module._github_config = None
    yield


@pytest.fixture(autouse=True)
def _presence():
    """Repo presence (ADR-052): connected unless a test says otherwise."""
    with patch.object(handler_module, "is_repo_connected", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_repo_record():
    """Sample GitHub repo record from DynamoDB."""
    return {
        "PK": "WS#ws_test",
        "SK": "GITHUB_REPO#acme/my-repo",
        "workspace_id": "ws_test",
        "repo_name": "acme/my-repo",
        "repo_type": "application",
        "include_code_maps": True,
        "last_sync_status": "queued",
        "installation_id": "12345",
    }


@pytest.fixture
def sample_standards_repo_record():
    """Sample standards repo record from DynamoDB."""
    return {
        "PK": "WS#ws_test",
        "SK": "GITHUB_REPO#acme/standards",
        "workspace_id": "ws_test",
        "repo_name": "acme/standards",
        "repo_type": "standards",
        "include_code_maps": True,
        "last_sync_status": "queued",
        "installation_id": "12345",
    }


@pytest.fixture
def sample_tree():
    """Sample GitHub repo tree response."""
    return [
        {"path": "README.md", "type": "blob", "sha": "aaa111", "size": 500},
        {"path": "src/main.py", "type": "blob", "sha": "bbb222", "size": 1200},
        {"path": "src/utils.py", "type": "blob", "sha": "ccc333", "size": 800},
        {"path": "docs/adr/001-architecture.md", "type": "blob", "sha": "ddd444", "size": 300},
        {"path": "terraform/main.tf", "type": "blob", "sha": "eee555", "size": 600},
        {"path": "tests/test_main.py", "type": "blob", "sha": "fff666", "size": 400},
        {"path": "src", "type": "tree", "sha": "ggg777"},
        {"path": "node_modules/pkg/index.js", "type": "blob", "sha": "hhh888", "size": 200},
        {"path": "config.yaml", "type": "blob", "sha": "iii999", "size": 150},
    ]


@pytest.fixture
def sample_sync_message():
    """Sample SQS sync request message body."""
    return {
        "workspace_id": "ws_test",
        "repo_name": "acme/my-repo",
        "installation_id": "12345",
    }


# ============================================================================
# Get Repo
# ============================================================================


class TestGetRepo:
    """Test DynamoDB repo record retrieval."""

    def test_get_repo_found(self, mock_table, sample_repo_record):
        """Test retrieving an existing repo record."""
        mock_table.get_item.return_value = {"Item": sample_repo_record}

        result = get_repo("ws_test", "acme/my-repo")

        assert result is not None
        assert result["repo_name"] == "acme/my-repo"
        assert result["repo_type"] == "application"
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "GITHUB_REPO#acme/my-repo"}
        )

    def test_get_repo_not_found(self, mock_table):
        """Test retrieving a non-existent repo record returns None."""
        mock_table.get_item.return_value = {}

        result = get_repo("ws_test", "acme/nonexistent")

        assert result is None

    def test_get_repo_uses_correct_key_format(self, mock_table):
        """Test that PK and SK are formatted correctly."""
        mock_table.get_item.return_value = {}

        get_repo("workspace_123", "org/repo-name")

        mock_table.get_item.assert_called_once_with(
            Key={"PK": "WS#workspace_123", "SK": "GITHUB_REPO#org/repo-name"}
        )


# ============================================================================
# Update Sync Status
# ============================================================================


class TestUpdateSyncStatus:
    """Test updating sync status in DynamoDB."""

    def test_update_status_in_progress(self, mock_table):
        """Test updating to in_progress status."""
        update_sync_status("ws_test", "acme/my-repo", "in_progress")

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"] == {
            "PK": "WS#ws_test",
            "SK": "GITHUB_REPO#acme/my-repo",
        }
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "in_progress"
        # in_progress should not set last_sync_at
        assert ":sync_at" not in expr_values

    def test_update_status_success(self, mock_table):
        """Test updating to success status sets last_sync_at."""
        update_sync_status("ws_test", "acme/my-repo", "success")

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "success"
        assert ":sync_at" in expr_values
        # Success without error should REMOVE last_sync_error
        assert "REMOVE" in call_args["UpdateExpression"]
        assert "last_sync_error" in call_args["UpdateExpression"]

    def test_update_status_failed_with_error(self, mock_table):
        """Test updating to failed status with error message."""
        update_sync_status("ws_test", "acme/my-repo", "failed", error="Connection timeout")

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "failed"
        assert expr_values[":error"] == "Connection timeout"
        assert ":sync_at" in expr_values  # failed also sets last_sync_at

    def test_update_status_with_commit_sha(self, mock_table):
        """Test updating status with commit SHA."""
        update_sync_status(
            "ws_test", "acme/my-repo", "success", commit_sha="abc123def456"
        )

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":sha"] == "abc123def456"
        assert "last_commit_sha = :sha" in call_args["UpdateExpression"]

    def test_update_status_success_removes_error(self, mock_table):
        """Test success status removes last_sync_error when no error given."""
        update_sync_status("ws_test", "acme/my-repo", "success")

        call_args = mock_table.update_item.call_args[1]
        update_expr = call_args["UpdateExpression"]
        assert "REMOVE last_sync_error" in update_expr
        assert ":error" not in call_args["ExpressionAttributeValues"]


# ============================================================================
# Should Process Code File
# ============================================================================


class TestShouldProcessCodeFile:
    """Test code file filtering logic."""

    def test_accepts_python_file(self):
        """Test Python files are accepted."""
        assert should_process_code_file(
            {"path": "src/main.py", "type": "blob", "size": 1000}
        )

    def test_accepts_typescript_file(self):
        """Test TypeScript files are accepted."""
        assert should_process_code_file(
            {"path": "app/index.ts", "type": "blob", "size": 500}
        )

    def test_accepts_terraform_file(self):
        """Test Terraform files are accepted."""
        assert should_process_code_file(
            {"path": "infra/main.tf", "type": "blob", "size": 800}
        )

    def test_accepts_javascript_file(self):
        """Test JavaScript files are accepted."""
        assert should_process_code_file(
            {"path": "lib/utils.js", "type": "blob", "size": 300}
        )

    def test_rejects_non_blob_type(self):
        """Test non-blob items (trees/directories) are rejected."""
        assert not should_process_code_file(
            {"path": "src", "type": "tree", "sha": "aaa"}
        )

    def test_rejects_unsupported_extension(self):
        """Test files with unsupported extensions are rejected."""
        assert not should_process_code_file(
            {"path": "image.png", "type": "blob", "size": 500}
        )

    def test_rejects_markdown_file(self):
        """Test markdown files are not treated as code."""
        assert not should_process_code_file(
            {"path": "README.md", "type": "blob", "size": 100}
        )

    def test_rejects_file_in_node_modules(self):
        """Test files under node_modules are skipped."""
        assert not should_process_code_file(
            {"path": "node_modules/pkg/index.js", "type": "blob", "size": 100}
        )

    def test_rejects_file_in_pycache(self):
        """Test files under __pycache__ are skipped."""
        assert not should_process_code_file(
            {"path": "__pycache__/module.cpython-310.pyc", "type": "blob", "size": 100}
        )

    def test_rejects_file_in_venv(self):
        """Test files under venv are skipped."""
        assert not should_process_code_file(
            {"path": "venv/lib/site.py", "type": "blob", "size": 100}
        )

    def test_rejects_file_in_git_dir(self):
        """Test files under .git are skipped."""
        assert not should_process_code_file(
            {"path": ".git/hooks/pre-commit", "type": "blob", "size": 100}
        )

    def test_rejects_oversized_file(self):
        """Test files exceeding MAX_FILE_SIZE are rejected."""
        assert not should_process_code_file(
            {"path": "huge.py", "type": "blob", "size": 2 * 1024 * 1024}
        )

    def test_accepts_file_at_max_size(self):
        """Test files exactly at MAX_FILE_SIZE are accepted."""
        assert should_process_code_file(
            {"path": "exact.py", "type": "blob", "size": 1024 * 1024}
        )

    def test_rejects_file_in_build_dir(self):
        """Test files under build directory are skipped."""
        assert not should_process_code_file(
            {"path": "build/output.js", "type": "blob", "size": 100}
        )

    def test_rejects_file_in_dist_dir(self):
        """Test files under dist directory are skipped."""
        assert not should_process_code_file(
            {"path": "dist/bundle.js", "type": "blob", "size": 100}
        )

    def test_accepts_nested_code_file(self):
        """Test deeply nested code files are accepted."""
        assert should_process_code_file(
            {"path": "src/api/handlers/v2/create.py", "type": "blob", "size": 500}
        )

    def test_skip_dir_only_in_directory_parts(self):
        """Test that SKIP_DIRS only apply to directory parts, not filenames."""
        # A file named 'build.py' in root should be accepted
        assert should_process_code_file(
            {"path": "build.py", "type": "blob", "size": 100}
        )


# ============================================================================
# Should Process Doc File
# ============================================================================


class TestShouldProcessDocFile:
    """Test documentation file filtering logic."""

    def test_accepts_markdown_file(self):
        """Test markdown files are accepted."""
        assert should_process_doc_file(
            {"path": "README.md", "type": "blob", "size": 500}
        )

    def test_accepts_yaml_file(self):
        """Test YAML files are accepted."""
        assert should_process_doc_file(
            {"path": "config.yaml", "type": "blob", "size": 300}
        )

    def test_accepts_yml_file(self):
        """Test .yml extension is accepted."""
        assert should_process_doc_file(
            {"path": "ci.yml", "type": "blob", "size": 200}
        )

    def test_accepts_json_file(self):
        """Test JSON files are accepted."""
        assert should_process_doc_file(
            {"path": "package.json", "type": "blob", "size": 400}
        )

    def test_accepts_txt_file(self):
        """Test text files are accepted."""
        assert should_process_doc_file(
            {"path": "notes.txt", "type": "blob", "size": 100}
        )

    def test_accepts_html_file(self):
        """Test HTML files are accepted."""
        assert should_process_doc_file(
            {"path": "docs/index.html", "type": "blob", "size": 800}
        )

    def test_accepts_htm_file(self):
        """Test .htm extension is accepted."""
        assert should_process_doc_file(
            {"path": "page.htm", "type": "blob", "size": 200}
        )

    def test_accepts_rst_file(self):
        """Test reStructuredText files are accepted."""
        assert should_process_doc_file(
            {"path": "docs/guide.rst", "type": "blob", "size": 400}
        )

    def test_accepts_pdf_file(self):
        """Test PDF files are accepted."""
        assert should_process_doc_file(
            {"path": "docs/manual.pdf", "type": "blob", "size": 5000}
        )

    def test_accepts_csv_file(self):
        """Test CSV files are accepted."""
        assert should_process_doc_file(
            {"path": "data.csv", "type": "blob", "size": 2000}
        )

    def test_accepts_xml_file(self):
        """Test XML files are accepted."""
        assert should_process_doc_file(
            {"path": "config.xml", "type": "blob", "size": 600}
        )

    def test_rejects_python_file(self):
        """Test Python files are not treated as docs."""
        assert not should_process_doc_file(
            {"path": "main.py", "type": "blob", "size": 500}
        )

    def test_rejects_non_blob_type(self):
        """Test non-blob items are rejected."""
        assert not should_process_doc_file(
            {"path": "docs", "type": "tree", "sha": "aaa"}
        )

    def test_rejects_file_in_node_modules(self):
        """Test doc files under node_modules are skipped."""
        assert not should_process_doc_file(
            {"path": "node_modules/pkg/README.md", "type": "blob", "size": 200}
        )

    def test_rejects_oversized_doc(self):
        """Test oversized doc files are rejected."""
        assert not should_process_doc_file(
            {"path": "huge.md", "type": "blob", "size": 2 * 1024 * 1024}
        )


# ============================================================================
# Classify Doc Type
# ============================================================================


class TestClassifyDocType:
    """Test document type classification."""

    def test_file_under_standards_path(self):
        """Test file under a standards path returns 'adr'."""
        result = classify_doc_type("docs/adr/001-architecture.md", ["docs/adr"])
        assert result == "adr"

    def test_file_exactly_matching_standards_path(self):
        """Test file path exactly matching a standards path."""
        result = classify_doc_type("docs/adr", ["docs/adr"])
        assert result == "adr"

    def test_file_not_under_standards_path(self):
        """Test file not under any standards path returns None."""
        result = classify_doc_type("src/main.py", ["docs/adr"])
        assert result is None

    def test_multiple_standards_paths(self):
        """Test classification with multiple standards paths."""
        result = classify_doc_type(
            "standards/guidelines/naming.md",
            ["docs/adr", "standards/guidelines"],
        )
        assert result == "adr"

    def test_trailing_slash_in_standards_path(self):
        """Test standards paths with trailing slashes are handled."""
        result = classify_doc_type("docs/adr/001.md", ["docs/adr/"])
        assert result == "adr"

    def test_partial_path_match_rejected(self):
        """Test partial directory name match does not classify."""
        result = classify_doc_type("docs/adr-archive/old.md", ["docs/adr"])
        assert result is None


# ============================================================================
# Fetch Ingestion Config
# ============================================================================


class TestFetchIngestionConfig:
    """Test fetching .outcomeops.yaml ingestion config."""

    @patch.object(handler_module, "get_file_content")
    def test_returns_configured_paths(self, mock_get_content):
        """Test returns standards_paths from config file."""
        mock_get_content.return_value = (
            "ingestion:\n  standards_paths:\n    - docs/standards\n    - adrs\n"
        )

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/standards", "adrs"]
        mock_get_content.assert_called_once_with(
            "token", "acme", "my-repo", ".outcomeops.yaml", "main"
        )

    @patch.object(handler_module, "get_file_content")
    def test_returns_default_when_file_missing(self, mock_get_content):
        """Test returns default ['docs/adr'] when config file not found."""
        mock_get_content.return_value = None

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/adr"]

    @patch.object(handler_module, "get_file_content")
    def test_returns_default_when_yaml_invalid(self, mock_get_content):
        """Test returns default when YAML is unparseable."""
        mock_get_content.return_value = "{{ invalid yaml"

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/adr"]

    @patch.object(handler_module, "get_file_content")
    def test_returns_default_when_no_ingestion_key(self, mock_get_content):
        """Test returns default when config has no ingestion key."""
        mock_get_content.return_value = "other_key: value\n"

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/adr"]

    @patch.object(handler_module, "get_file_content")
    def test_returns_default_when_standards_paths_not_list(self, mock_get_content):
        """Test returns default when standards_paths is not a list."""
        mock_get_content.return_value = (
            "ingestion:\n  standards_paths: just-a-string\n"
        )

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/adr"]

    @patch.object(handler_module, "get_file_content")
    def test_returns_default_when_empty_yaml(self, mock_get_content):
        """Test returns default when YAML content is empty."""
        mock_get_content.return_value = ""

        result = fetch_ingestion_config("token", "acme", "my-repo", "main")

        assert result == ["docs/adr"]


# ============================================================================
# Queue File for Ingestion
# ============================================================================


class TestQueueFileForIngestion:
    """Test queuing files to file-ingestion SQS queue."""

    def test_queue_file_success(self):
        """Test successful file queuing."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        result = queue_file_for_ingestion(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            file_path="docs/adr/001.md",
            file_sha="abc123",
            commit_sha="def456",
            repo_type="application",
        )

        assert result is True
        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "github"
        assert msg["workspace_id"] == "ws_test"
        assert msg["repo_name"] == "acme/my-repo"
        assert msg["file_path"] == "docs/adr/001.md"
        assert msg["file_sha"] == "abc123"
        assert msg["commit_sha"] == "def456"
        assert msg["repo_type"] == "application"

    def test_queue_file_standards_repo_type(self):
        """Test queuing with standards repo type."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        result = queue_file_for_ingestion(
            workspace_id="ws_test",
            repo_name="acme/standards",
            file_path="adr-001.md",
            file_sha="abc",
            commit_sha="def",
            repo_type="standards",
        )

        assert result is True
        msg = json.loads(
            mock_sqs_client.send_message.call_args[1]["MessageBody"]
        )
        assert msg["repo_type"] == "standards"

    def test_queue_file_no_queue_configured(self):
        """Test returns False when queue URL not configured."""
        handler_module.FILE_INGESTION_QUEUE = None

        result = queue_file_for_ingestion(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
        )

        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_file_empty_queue_url(self):
        """Test returns False when queue URL is empty string."""
        handler_module.FILE_INGESTION_QUEUE = ""

        result = queue_file_for_ingestion(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
        )

        assert result is False

    def test_queue_file_sqs_error(self):
        """Test returns False on SQS client error."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "SendMessage"
        )

        result = queue_file_for_ingestion(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
        )

        assert result is False


# ============================================================================
# Upload File to S3
# ============================================================================


class TestUploadFileToS3:
    """Test uploading files directly to S3."""

    def test_upload_success_markdown(self):
        """Test successful upload of a markdown file."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        result = upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/standards",
            file_path="docs/adr/001-arch.md",
            file_sha="abc123",
            commit_sha="def456",
            content="# Architecture Decision\nWe chose X.",
            repo_type="standards",
            doc_type="adr",
        )

        assert result == "workspaces/ws_test/github/acme/standards/docs/adr/001-arch.md"
        mock_s3_client.put_object.assert_called_once()
        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "text/markdown"
        assert call_args["Metadata"]["source"] == "github"
        assert call_args["Metadata"]["github_doc_type"] == "adr"
        assert call_args["Metadata"]["github_repo_type"] == "standards"

    def test_upload_success_yaml(self):
        """Test upload with YAML content type."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        result = upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="config.yaml",
            file_sha="aaa",
            commit_sha="bbb",
            content="key: value",
        )

        assert result is not None
        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "text/yaml"

    def test_upload_success_json(self):
        """Test upload with JSON content type."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="data.json",
            file_sha="aaa",
            commit_sha="bbb",
            content='{"key": "value"}',
        )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/json"

    def test_upload_success_html(self):
        """Test upload with HTML content type."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="index.html",
            file_sha="aaa",
            commit_sha="bbb",
            content="<html></html>",
        )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "text/html"

    def test_upload_unknown_extension_defaults_text_plain(self):
        """Test unknown extension defaults to text/plain."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="notes.unknown",
            file_sha="aaa",
            commit_sha="bbb",
            content="Some content",
        )

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "text/plain"

    def test_upload_no_doc_type_excludes_metadata_key(self):
        """Test metadata excludes github_doc_type when doc_type is None."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="readme.md",
            file_sha="aaa",
            commit_sha="bbb",
            content="Hello",
            doc_type=None,
        )

        call_args = mock_s3_client.put_object.call_args[1]
        assert "github_doc_type" not in call_args["Metadata"]

    def test_upload_no_bucket_configured(self):
        """Test returns None when KB_BUCKET is not configured."""
        handler_module.KB_BUCKET = None

        result = upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
            content="Hello",
        )

        assert result is None
        mock_s3_client.put_object.assert_not_called()

    def test_upload_empty_bucket_configured(self):
        """Test returns None when KB_BUCKET is empty string."""
        handler_module.KB_BUCKET = ""

        result = upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
            content="Hello",
        )

        assert result is None

    def test_upload_s3_error(self):
        """Test returns None on S3 client error."""
        handler_module.KB_BUCKET = "test-kb-bucket"
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "PutObject"
        )

        result = upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="file.md",
            file_sha="aaa",
            commit_sha="bbb",
            content="Hello",
        )

        assert result is None

    def test_upload_s3_key_format(self):
        """Test S3 key follows expected format."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        result = upload_file_to_s3(
            workspace_id="ws_abc",
            repo_name="org/repo",
            file_path="docs/guide.md",
            file_sha="aaa",
            commit_sha="bbb",
            content="Guide",
        )

        assert result == "workspaces/ws_abc/github/org/repo/docs/guide.md"

    def test_upload_metadata_fields(self):
        """Test all expected metadata fields are present."""
        handler_module.KB_BUCKET = "test-kb-bucket"

        upload_file_to_s3(
            workspace_id="ws_test",
            repo_name="acme/repo",
            file_path="file.md",
            file_sha="sha111",
            commit_sha="sha222",
            content="Content",
            repo_type="application",
            doc_type="adr",
        )

        metadata = mock_s3_client.put_object.call_args[1]["Metadata"]
        assert metadata["source"] == "github"
        assert metadata["workspace_id"] == "ws_test"
        assert metadata["github_repo_name"] == "acme/repo"
        assert metadata["github_file_path"] == "file.md"
        assert metadata["github_file_sha"] == "sha111"
        assert metadata["github_commit_sha"] == "sha222"
        assert metadata["github_repo_type"] == "application"
        assert metadata["github_doc_type"] == "adr"


# ============================================================================
# Queue Repo for Code Maps
# ============================================================================


class TestQueueRepoForCodeMaps:
    """Test queuing repos to repo-summaries SQS FIFO queue."""

    def test_queue_repo_success(self):
        """Test successful repo queuing with correct message format."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/repo-summaries.fifo"

        all_files = [
            {"path": "src/main.py", "type": "blob"},
            {"path": "src/utils.py", "type": "blob"},
        ]

        result = queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            repo_type="application",
            installation_id="12345",
            all_files=all_files,
        )

        assert result is True
        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["repo"] == "my-repo"
        assert msg["repo_project"] == "acme/my-repo"
        assert msg["repo_type"] == "application"
        assert msg["workspace_id"] == "ws_test"
        assert msg["installation_id"] == "12345"
        assert msg["all_files"] == ["src/main.py", "src/utils.py"]
        assert msg["discover_code_units"] is True
        assert call_args["MessageGroupId"] == "acme/my-repo"

    def test_queue_repo_extracts_short_name(self):
        """Test repo short name is extracted from owner/repo format."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/queue.fifo"

        queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="my-org/cool-service",
            repo_type="application",
            installation_id="999",
            all_files=[],
        )

        msg = json.loads(
            mock_sqs_client.send_message.call_args[1]["MessageBody"]
        )
        assert msg["repo"] == "cool-service"
        assert msg["repo_project"] == "my-org/cool-service"

    def test_queue_repo_without_slash_in_name(self):
        """Test repo name without slash uses full name."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/queue.fifo"

        queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="standalone-repo",
            repo_type="application",
            installation_id="999",
            all_files=[],
        )

        msg = json.loads(
            mock_sqs_client.send_message.call_args[1]["MessageBody"]
        )
        assert msg["repo"] == "standalone-repo"

    def test_queue_repo_extracts_paths_from_dicts(self):
        """Test file paths are extracted from dict items."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/queue.fifo"

        all_files = [
            {"path": "a.py", "type": "blob", "sha": "aaa"},
            {"path": "b.js", "type": "blob", "sha": "bbb"},
        ]

        queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="org/repo",
            repo_type="application",
            installation_id="123",
            all_files=all_files,
        )

        msg = json.loads(
            mock_sqs_client.send_message.call_args[1]["MessageBody"]
        )
        assert msg["all_files"] == ["a.py", "b.js"]

    def test_queue_repo_no_queue_configured(self):
        """Test returns False when queue URL not configured."""
        handler_module.REPO_SUMMARIES_QUEUE = None

        result = queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="org/repo",
            repo_type="application",
            installation_id="123",
            all_files=[],
        )

        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_repo_empty_queue_url(self):
        """Test returns False when queue URL is empty string."""
        handler_module.REPO_SUMMARIES_QUEUE = ""

        result = queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="org/repo",
            repo_type="application",
            installation_id="123",
            all_files=[],
        )

        assert result is False

    def test_queue_repo_sqs_error(self):
        """Test returns False on SQS client error."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/queue.fifo"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal"}}, "SendMessage"
        )

        result = queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="org/repo",
            repo_type="application",
            installation_id="123",
            all_files=[],
        )

        assert result is False

    def test_queue_repo_fifo_dedup_id_contains_workspace_and_repo(self):
        """Test FIFO dedup ID contains workspace_id and repo_name."""
        handler_module.REPO_SUMMARIES_QUEUE = "https://sqs.example.com/queue.fifo"

        queue_repo_for_code_maps(
            workspace_id="ws_test",
            repo_name="org/repo",
            repo_type="application",
            installation_id="123",
            all_files=[],
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        dedup_id = call_args["MessageDeduplicationId"]
        assert "ws_test" in dedup_id
        assert "org/repo" in dedup_id


# ============================================================================
# Process Standards Repo
# ============================================================================


class TestProcessStandardsRepo:
    """Test standards repo processing flow."""

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    def test_standards_repo_uploads_doc_files(
        self, mock_get_content, mock_upload, mock_update_status
    ):
        """Test standards repo uploads doc files to S3."""
        mock_get_content.return_value = "# Some content"
        mock_upload.return_value = "workspaces/ws/github/org/repo/file.md"

        tree = [
            {"path": "docs/adr/001.md", "type": "blob", "sha": "aaa", "size": 500},
            {"path": "guide.yaml", "type": "blob", "sha": "bbb", "size": 200},
            {"path": "src/main.py", "type": "blob", "sha": "ccc", "size": 300},  # Not a doc
            {"path": "src", "type": "tree", "sha": "ddd"},  # Not a blob
        ]

        result = _process_standards_repo(
            workspace_id="ws_test",
            repo_name="acme/standards",
            repo_type="standards",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
            token="fake_token",
            owner="acme",
            repo_short="standards",
            default_branch="main",
        )

        assert result["status"] == "success"
        assert result["repo_type"] == "standards"
        assert result["files_uploaded"] == 2
        assert result["commit_sha"] == "commit123"
        # Should call get_file_content for each doc file
        assert mock_get_content.call_count == 2
        # Should call upload for each file with content
        assert mock_upload.call_count == 2
        # All standards files get doc_type="adr"
        for c in mock_upload.call_args_list:
            assert c[1]["doc_type"] == "adr"
        # plan-git-truth added diagnostic kwargs; assert by
        # positional shape + the legacy commit_sha kwarg, allow the
        # extra diagnostic kwargs.
        mock_update_status.assert_called_once()
        args, kwargs = mock_update_status.call_args
        assert args == ("ws_test", "acme/standards", "success")
        assert kwargs.get("commit_sha") == "commit123"

    @patch.object(handler_module, "_open_docs_pending_batch")
    @patch.object(handler_module, "_queue_orphan_detection")
    @patch.object(handler_module, "fetch_changed_files")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    def test_standards_incremental_sync_does_not_orphan_unchanged_docs(
        self, mock_get_content, mock_upload, mock_update_status,
        mock_changed, mock_orphan, mock_open_ledger,
    ):
        """Regression: an INCREMENTAL standards sync (only some docs
        changed) must pass the FULL current doc set to orphan detection,
        not just the changed subset. Passing the changed subset made the
        delete-worker treat every unchanged doc as an orphan and delete
        it — the data loss observed in prd (a workspace's KB dropped from
        12 docs to the 4 that changed)."""
        mock_get_content.return_value = "# content"
        mock_upload.side_effect = lambda **kw: f"workspaces/ws/.../{kw['file_path']}"
        # Repo tree has 3 docs; only ONE changed since last sync.
        tree = [
            {"path": "docs/adr/001.md", "type": "blob", "sha": "a", "size": 100},
            {"path": "docs/adr/002.md", "type": "blob", "sha": "b", "size": 100},
            {"path": "docs/adr/003.md", "type": "blob", "sha": "c", "size": 100},
        ]
        mock_changed.return_value = {"docs/adr/003.md"}  # only 003 changed

        result = _process_standards_repo(
            workspace_id="ws_test",
            repo_name="acme/standards",
            repo_type="standards",
            tree=tree,
            current_sha="newsha",
            current_commit_timestamp=1700000000,
            token="fake_token",
            owner="acme",
            repo_short="standards",
            default_branch="main",
            last_commit_sha="oldsha",  # triggers the incremental path
        )

        assert result["status"] == "success"
        # Only the changed doc is uploaded (incremental is preserved)...
        assert result["files_uploaded"] == 1
        # ...but orphan detection's keep-set is the FULL tree, so the
        # unchanged 001/002 are NOT deleted.
        mock_orphan.assert_called_once()
        keep_set = set(mock_orphan.call_args[0][2])
        assert keep_set == {"docs/adr/001.md", "docs/adr/002.md", "docs/adr/003.md"}

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    def test_standards_repo_skips_unfetchable_files(
        self, mock_get_content, mock_upload, mock_update_status
    ):
        """Test standards repo skips files that cannot be fetched."""
        mock_get_content.return_value = None

        tree = [
            {"path": "docs/adr/001.md", "type": "blob", "sha": "aaa", "size": 500},
        ]

        result = _process_standards_repo(
            workspace_id="ws_test",
            repo_name="acme/standards",
            repo_type="standards",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
            token="fake_token",
            owner="acme",
            repo_short="standards",
            default_branch="main",
        )

        assert result["files_uploaded"] == 0
        mock_upload.assert_not_called()

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    def test_standards_repo_handles_upload_failure(
        self, mock_get_content, mock_upload, mock_update_status
    ):
        """Test standards repo counts only successful uploads."""
        mock_get_content.return_value = "Content"
        mock_upload.return_value = None  # Upload failed

        tree = [
            {"path": "docs/001.md", "type": "blob", "sha": "aaa", "size": 500},
        ]

        result = _process_standards_repo(
            workspace_id="ws_test",
            repo_name="acme/standards",
            repo_type="standards",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
            token="fake_token",
            owner="acme",
            repo_short="standards",
            default_branch="main",
        )

        assert result["files_uploaded"] == 0


# ============================================================================
# Process Application Repo
# ============================================================================


class TestProcessApplicationRepo:
    """Test application repo processing flow."""

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "queue_repo_for_code_maps")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    @patch.object(handler_module, "fetch_ingestion_config")
    def test_application_repo_success(
        self,
        mock_fetch_config,
        mock_get_content,
        mock_upload,
        mock_queue_code_maps,
        mock_update_status,
    ):
        """Test application repo uploads docs and queues code maps."""
        mock_fetch_config.return_value = ["docs/adr"]
        mock_get_content.return_value = "# README"
        mock_upload.return_value = "workspaces/ws/github/org/repo/README.md"
        mock_queue_code_maps.return_value = True

        tree = [
            {"path": "README.md", "type": "blob", "sha": "aaa", "size": 500},
            {"path": "src/main.py", "type": "blob", "sha": "bbb", "size": 1200},
            {"path": "docs/adr/001.md", "type": "blob", "sha": "ccc", "size": 300},
        ]

        result = _process_application_repo(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            repo_type="application",
            installation_id="12345",
            owner="acme",
            repo_short="my-repo",
            token="fake_token",
            default_branch="main",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
        )

        assert result["status"] == "success"
        assert result["repo_type"] == "application"
        assert result["commit_sha"] == "commit123"
        # README.md and docs/adr/001.md are doc files, src/main.py is not
        assert result["docs_uploaded"] == 2
        # All tree items are passed to queue_repo_for_code_maps
        assert result["files_queued"] == 3
        mock_queue_code_maps.assert_called_once()
        # plan-git-truth added diagnostic kwargs.
        mock_update_status.assert_called_once()
        args, kwargs = mock_update_status.call_args
        assert args == ("ws_test", "acme/my-repo", "success")
        assert kwargs.get("commit_sha") == "commit123"

    @patch.object(handler_module, "_open_code_map_dispatch_ledger")
    @patch.object(handler_module, "_queue_orphan_detection")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "queue_repo_for_code_maps")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    @patch.object(handler_module, "fetch_changed_files")
    @patch.object(handler_module, "fetch_ingestion_config")
    def test_code_maps_already_complete_skips_dispatch(
        self,
        mock_fetch_config,
        mock_changed,
        mock_get_content,
        mock_upload,
        mock_queue_code_maps,
        mock_update_status,
        _mock_orphans,
        mock_open_ledger,
    ):
        """Manual retry where only docs lagged: the code-map baseline is
        already HEAD, so no code-map work is dispatched; only the changed
        docs since the docs baseline are re-uploaded."""
        mock_fetch_config.return_value = []
        mock_changed.return_value = {"README.md"}
        mock_get_content.return_value = "# README"
        mock_upload.return_value = "key"

        tree = [
            {"path": "README.md", "type": "blob", "sha": "aaa", "size": 500},
            {"path": "src/main.py", "type": "blob", "sha": "bbb", "size": 1200},
        ]
        with patch.object(handler_module, "_open_docs_pending_batch"):
            result = _process_application_repo(
                workspace_id="ws_test",
                repo_name="acme/my-repo",
                repo_type="application",
                installation_id="12345",
                owner="acme",
                repo_short="my-repo",
                token="fake_token",
                default_branch="main",
                tree=tree,
                current_sha="head_sha",
                current_commit_timestamp=1700000000,
                last_commit_sha="older_sha",
                code_map_last_sha="head_sha",
            )

        assert result["status"] == "success"
        assert result["files_queued"] == 0
        assert result["docs_uploaded"] == 1
        mock_queue_code_maps.assert_not_called()
        mock_open_ledger.assert_not_called()

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "queue_repo_for_code_maps")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    @patch.object(handler_module, "fetch_ingestion_config")
    def test_application_repo_classifies_adr_files(
        self,
        mock_fetch_config,
        mock_get_content,
        mock_upload,
        mock_queue_code_maps,
        mock_update_status,
    ):
        """Test ADR classification is applied to doc files."""
        mock_fetch_config.return_value = ["docs/adr"]
        mock_get_content.return_value = "Content"
        mock_upload.return_value = "s3-key"
        mock_queue_code_maps.return_value = True

        tree = [
            {"path": "docs/adr/001.md", "type": "blob", "sha": "aaa", "size": 300},
            {"path": "README.md", "type": "blob", "sha": "bbb", "size": 500},
        ]

        _process_application_repo(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            repo_type="application",
            installation_id="12345",
            owner="acme",
            repo_short="my-repo",
            token="fake_token",
            default_branch="main",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
        )

        # Check upload calls for doc_type classification
        upload_calls = mock_upload.call_args_list
        # docs/adr/001.md should have doc_type="adr"
        adr_call = [c for c in upload_calls if c[1]["file_path"] == "docs/adr/001.md"]
        assert adr_call[0][1]["doc_type"] == "adr"
        # README.md should have doc_type=None
        readme_call = [c for c in upload_calls if c[1]["file_path"] == "README.md"]
        assert readme_call[0][1]["doc_type"] is None

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "queue_repo_for_code_maps")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    @patch.object(handler_module, "fetch_ingestion_config")
    def test_application_repo_queue_failure(
        self,
        mock_fetch_config,
        mock_get_content,
        mock_upload,
        mock_queue_code_maps,
        mock_update_status,
    ):
        """Test application repo returns error when queue_repo_for_code_maps fails."""
        mock_fetch_config.return_value = ["docs/adr"]
        mock_get_content.return_value = "Content"
        mock_upload.return_value = "s3-key"
        mock_queue_code_maps.return_value = False  # Queue failed

        tree = [
            {"path": "README.md", "type": "blob", "sha": "aaa", "size": 500},
        ]

        result = _process_application_repo(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            repo_type="application",
            installation_id="12345",
            owner="acme",
            repo_short="my-repo",
            token="fake_token",
            default_branch="main",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
        )

        assert result["status"] == "error"
        assert "Failed to queue" in result["error"]
        # plan-git-truth added diagnostic kwargs
        # (last_failed_sha + last_failure_reason).
        mock_update_status.assert_called_once()
        args, kwargs = mock_update_status.call_args
        assert args == (
            "ws_test", "acme/my-repo", "failed",
            "Failed to queue to repo-summaries",
        )
        assert kwargs.get("last_failed_sha") == "commit123"
        assert kwargs.get("last_failure_reason") == "sqs_send_failed"

    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "queue_repo_for_code_maps")
    @patch.object(handler_module, "upload_file_to_s3")
    @patch.object(handler_module, "get_file_content")
    @patch.object(handler_module, "fetch_ingestion_config")
    def test_application_repo_skips_unfetchable_docs(
        self,
        mock_fetch_config,
        mock_get_content,
        mock_upload,
        mock_queue_code_maps,
        mock_update_status,
    ):
        """Test application repo skips doc files that cannot be fetched."""
        mock_fetch_config.return_value = ["docs/adr"]
        mock_get_content.return_value = None  # Cannot fetch
        mock_queue_code_maps.return_value = True

        tree = [
            {"path": "README.md", "type": "blob", "sha": "aaa", "size": 500},
        ]

        result = _process_application_repo(
            workspace_id="ws_test",
            repo_name="acme/my-repo",
            repo_type="application",
            installation_id="12345",
            owner="acme",
            repo_short="my-repo",
            token="fake_token",
            default_branch="main",
            tree=tree,
            current_sha="commit123",
            current_commit_timestamp=1700000000,
        )

        assert result["status"] == "success"
        assert result["docs_uploaded"] == 0
        mock_upload.assert_not_called()


# ============================================================================
# Process Sync Request
# ============================================================================


class TestResolveSyncBranch:
    """``resolve_sync_branch`` reads the customer's stored branch from
    the GITHUB_REPO# row first (honors the override, avoids a
    live GitHub call), lazy-backfilling on rows added before
    shipped."""

    @patch.object(handler_module, "get_default_branch")
    @patch.object(handler_module, "_backfill_default_branch")
    def test_uses_stored_branch_when_row_has_it(
        self, mock_backfill, mock_live_call,
    ):
        # Row has the field -> skip the API call, skip the backfill,
        # return the stored value. This is the happy-path for every
        # repo added or synced since .
        repo_row = {
            "repo_name": "acme/my-repo",
            "default_branch": "develop",  # customer picked non-default
        }
        branch = handler_module.resolve_sync_branch(
            repo_row, "tok", "acme", "my-repo", "ws_test", "acme/my-repo",
        )
        assert branch == "develop"
        mock_live_call.assert_not_called()
        mock_backfill.assert_not_called()

    @patch.object(handler_module, "get_default_branch")
    @patch.object(handler_module, "_backfill_default_branch")
    def test_backfills_when_row_lacks_the_field(
        self, mock_backfill, mock_live_call,
    ):
        # Legacy row (added before ) has no default_branch.
        # Resolve via live API + write back so the next sync skips
        # the live call.
        mock_live_call.return_value = "master"
        repo_row = {"repo_name": "corp/legacy-app"}

        branch = handler_module.resolve_sync_branch(
            repo_row, "tok", "corp", "legacy-app", "ws_test", "corp/legacy-app",
        )
        assert branch == "master"
        mock_live_call.assert_called_once_with("tok", "corp", "legacy-app")
        mock_backfill.assert_called_once_with(
            "ws_test", "corp/legacy-app", "master",
        )


class TestBackfillDefaultBranch:
    """Isolated coverage for the lazy-backfill write path so a
    dual_write failure is caught + logged, not surfaced to callers."""

    @patch.object(handler_module, "dual_write")
    def test_writes_via_dual_write_with_conditional(self, mock_dw):
        handler_module._backfill_default_branch(
            "ws_test", "acme/my-repo", "master",
        )
        mock_dw.ddb_update_item.assert_called_once()
        kwargs = mock_dw.ddb_update_item.call_args.kwargs
        # UpdateItem uses SET default_branch = :b
        assert "default_branch" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"] == {":b": "master"}
        # Conditional prevents recreating deleted rows.
        assert "attribute_exists(repo_name)" in kwargs["ConditionExpression"]

    @patch.object(handler_module, "dual_write")
    def test_swallows_failures_so_sync_still_runs(self, mock_dw):
        mock_dw.ddb_update_item.side_effect = Exception("DDB unavailable")
        # Should not raise -- resolve_sync_branch's caller depends on
        # this method's best-effort semantics.
        handler_module._backfill_default_branch(
            "ws_test", "acme/my-repo", "master",
        )


class TestProcessSyncRequest:
    """Test the main sync processing orchestration."""

    @patch.object(handler_module, "_process_application_repo")
    @patch.object(handler_module, "get_repo_tree")
    @patch.object(handler_module, "get_latest_commit")
    @patch.object(handler_module, "get_default_branch")
    @patch.object(handler_module, "get_installation_token")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "get_repo")
    def test_sync_application_repo_success(
        self,
        mock_get_repo,
        mock_update_status,
        mock_get_token,
        mock_get_branch,
        mock_get_sha,
        mock_get_tree,
        mock_process_app,
    ):
        """Test successful sync of an application repo."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": True,
        }
        mock_get_token.return_value = "fake_token"
        mock_get_branch.return_value = "main"
        mock_get_sha.return_value = ("abc123", 1700000000)
        mock_get_tree.return_value = [{"path": "main.py", "type": "blob"}]
        mock_process_app.return_value = {
            "status": "success",
            "repo_type": "application",
            "files_queued": 1,
            "docs_uploaded": 0,
            "commit_sha": "abc123",
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "success"
        mock_update_status.assert_any_call("ws_test", "acme/my-repo", "in_progress")
        mock_process_app.assert_called_once()

    @patch.object(handler_module, "_process_standards_repo")
    @patch.object(handler_module, "get_repo_tree")
    @patch.object(handler_module, "get_latest_commit")
    @patch.object(handler_module, "get_default_branch")
    @patch.object(handler_module, "get_installation_token")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "get_repo")
    def test_sync_standards_repo_routes_correctly(
        self,
        mock_get_repo,
        mock_update_status,
        mock_get_token,
        mock_get_branch,
        mock_get_sha,
        mock_get_tree,
        mock_process_std,
    ):
        """Test standards repo is routed to _process_standards_repo."""
        mock_get_repo.return_value = {
            "repo_type": "standards",
            "include_code_maps": True,
        }
        mock_get_token.return_value = "fake_token"
        mock_get_branch.return_value = "main"
        mock_get_sha.return_value = ("abc123", 1700000000)
        mock_get_tree.return_value = []
        mock_process_std.return_value = {
            "status": "success",
            "repo_type": "standards",
            "files_uploaded": 0,
            "commit_sha": "abc123",
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/standards",
            "installation_id": "12345",
        })

        assert result["status"] == "success"
        mock_process_std.assert_called_once()

    @patch.object(handler_module, "get_repo")
    def test_sync_repo_not_found(self, mock_get_repo):
        """Test sync returns error when repo not found in DynamoDB."""
        mock_get_repo.return_value = None

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/missing",
            "installation_id": "12345",
        })

        assert result["status"] == "error"
        assert "not found" in result["error"].lower()

    @patch.object(handler_module, "get_repo")
    def test_sync_code_maps_disabled(self, mock_get_repo):
        """Test sync is skipped when include_code_maps is False."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": False,
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "skipped"
        assert "disabled" in result["reason"].lower()

    @patch.object(handler_module, "get_latest_commit")
    @patch.object(handler_module, "get_default_branch")
    @patch.object(handler_module, "get_installation_token")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "get_repo")
    def test_sync_no_changes_since_last_sync(
        self,
        mock_get_repo,
        mock_update_status,
        mock_get_token,
        mock_get_branch,
        mock_get_sha,
    ):
        """Test sync detects no changes when SHA matches."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "same_sha_123",
        }
        mock_get_token.return_value = "fake_token"
        mock_get_branch.return_value = "main"
        mock_get_sha.return_value = ("same_sha_123", 1700000000)

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "no_changes"
        assert result["commit_sha"] == "same_sha_123"

    def _manual_sync(self, repo_row, trigger_key="trigger", running=()):
        """Run process_sync_request as a manual sync at HEAD=head_sha with
        the given repo row; returns (result, update_sync_status mock,
        _process_application_repo mock). ``running`` names the ledger
        pipelines that have a live batch for HEAD."""
        def in_flight(_ws, _repo, pipelines, sha):
            assert sha == "head_sha"
            return any(p in running for p in pipelines)

        with patch.object(handler_module, "get_repo", return_value=repo_row), \
                patch.object(handler_module, "_pipelines_in_flight",
                             side_effect=in_flight), \
                patch.object(handler_module, "update_sync_status") as status, \
                patch.object(handler_module, "get_installation_token",
                             return_value="tok"), \
                patch.object(handler_module, "get_default_branch",
                             return_value="main"), \
                patch.object(handler_module, "get_latest_commit",
                             return_value=("head_sha", 1700000000)), \
                patch.object(handler_module, "get_repo_tree", return_value=[]), \
                patch.object(handler_module, "_process_application_repo",
                             return_value={"status": "success"}) as process_app:
            result = process_sync_request({
                "workspace_id": "ws_test",
                "repo_name": "acme/my-repo",
                "installation_id": "12345",
                trigger_key: "manual",
            })
        return result, status, process_app

    @pytest.mark.parametrize("trigger_key", ["trigger", "triggered_by"])
    def test_manual_sync_up_to_date_reports_nothing_changed(self, trigger_key):
        """Every pipeline already completed HEAD: a manual sync does no work
        and records up_to_date so the UI can say nothing changed. Both the
        current and the legacy message key are recognised as manual."""
        result, status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "head_sha",
            "last_code_map_sha": "head_sha",
        }, trigger_key)

        assert result["status"] == "no_changes"
        process_app.assert_not_called()
        assert status.call_args.args[2] == "up_to_date"

    def test_manual_sync_retries_from_last_completed_sha(self):
        """A run dispatched HEAD but code maps never completed (e.g. GitHub
        outage -> DLQ). last_commit_sha == HEAD, so the scheduled sync skips
        it; a manual sync re-processes code maps from the last COMPLETED SHA
        only, and leaves docs (already at HEAD) alone."""
        result, _status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "head_sha",
            "last_code_map_sha": "older_sha",
        })

        assert result["status"] == "success"
        kwargs = process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == "head_sha"      # docs: nothing to do
        assert kwargs["code_map_last_sha"] == "older_sha"   # code maps: the gap
        assert kwargs["current_sha"] == "head_sha"

    def test_manual_sync_while_code_maps_running_queues_nothing(self):
        """Pressed again while the code maps from the previous press are
        still running: report already_running and dispatch nothing."""
        result, status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "head_sha",
            "last_code_map_sha": "older_sha",
        }, running=("code_map_units",))

        assert result["status"] == "already_running"
        process_app.assert_not_called()
        assert status.call_args.args[2] == "already_running"

    def test_manual_sync_while_everything_running_queues_nothing(self):
        result, status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "older_sha",
            "last_code_map_sha": "older_sha",
        }, running=("docs", "code_map_dispatch"))

        assert result["status"] == "already_running"
        process_app.assert_not_called()

    def test_manual_sync_redoes_docs_but_not_running_code_maps(self):
        """Docs died, code maps are still running: redo docs only. Code
        maps get a HEAD baseline, which dispatches nothing."""
        result, _status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "older_sha",
            "last_code_map_sha": "older_sha",
        }, running=("code_map_units",))

        assert result["status"] == "success"
        kwargs = process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == "older_sha"
        assert kwargs["code_map_last_sha"] == "head_sha"

    def test_manual_sync_running_docs_do_not_block_dead_code_maps(self):
        """Docs still running but code maps died: the code maps are redone.
        Docs keep their completed baseline (a HEAD baseline would advance
        last_docs_sha before the running batch finished)."""
        result, _status, process_app = self._manual_sync({
            "repo_type": "application",
            "include_code_maps": True,
            "last_commit_sha": "head_sha",
            "last_docs_sha": "older_sha",
            "last_code_map_sha": "older_sha",
        }, running=("docs",))

        assert result["status"] == "success"
        kwargs = process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == "older_sha"
        assert kwargs["code_map_last_sha"] == "older_sha"

    def test_pipelines_in_flight_checks_each_pipeline(self):
        from shared import sha_truth  # type: ignore

        with patch.object(sha_truth, "batch_in_flight",
                          side_effect=[False, True]) as check:
            assert handler_module._pipelines_in_flight(
                "ws", "o/r", ("code_map_dispatch", "code_map_units"), "sha")
        assert [c.args[2] for c in check.call_args_list] == [
            "code_map_dispatch", "code_map_units"]
        assert check.call_args.kwargs["stale_after_seconds"] == (
            handler_module._IN_FLIGHT_STALE_SECONDS)

    def test_pipelines_in_flight_errors_mean_not_running(self, caplog):
        """A failed check reports not running (redo beats skip) and logs
        which repo and commit it could not check."""
        from shared import sha_truth  # type: ignore

        with patch.object(sha_truth, "batch_in_flight",
                          side_effect=RuntimeError("ddb down")), \
                caplog.at_level("WARNING"):
            assert not handler_module._pipelines_in_flight(
                "ws", "o/r", ("docs",), "sha123")
        assert "in-flight check failed for o/r@sha123: ddb down" in caplog.text

    def test_scheduled_sync_legacy_row_at_head_skips(self):
        """A legacy row (no completed columns) at HEAD is up to date: the
        baselines fall back to last_commit_sha and the status is success."""
        with patch.object(handler_module, "get_repo", return_value={
                    "repo_type": "application",
                    "include_code_maps": True,
                    "last_commit_sha": "head_sha",
                }), \
                patch.object(handler_module, "update_sync_status") as status, \
                patch.object(handler_module, "get_installation_token",
                             return_value="tok"), \
                patch.object(handler_module, "get_default_branch",
                             return_value="main"), \
                patch.object(handler_module, "get_latest_commit",
                             return_value=("head_sha", 1700000000)), \
                patch.object(handler_module, "_process_application_repo") as process_app:
            result = process_sync_request({
                "workspace_id": "ws_test",
                "repo_name": "acme/my-repo",
                "installation_id": "12345",
                "trigger": "scheduled",
            })
        assert result["status"] == "no_changes"
        process_app.assert_not_called()
        assert status.call_args.args[2] == "success"

    def test_filter_docs_same_sha_returns_nothing(self):
        """Docs baseline already at HEAD: no compare call, nothing to redo."""
        with patch.object(handler_module, "fetch_changed_files") as compare:
            out = handler_module._filter_docs_to_changed(
                "tok", "acme", "my-repo", [{"path": "README.md"}],
                "head_sha", "head_sha")
        assert out == []
        compare.assert_not_called()

    @patch.object(handler_module, "get_installation_token")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "get_repo")
    def test_sync_invalid_repo_name_format(
        self, mock_get_repo, mock_update_status, mock_get_token
    ):
        """Test sync fails for repo name without owner/repo format."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": True,
        }
        mock_get_token.return_value = "fake_token"

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "no-slash-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "error"
        assert "Invalid repo name" in result["error"]
        mock_update_status.assert_any_call(
            "ws_test", "no-slash-repo", "failed", "Invalid repo name format"
        )

    @patch.object(handler_module, "get_installation_token")
    @patch.object(handler_module, "update_sync_status")
    @patch.object(handler_module, "get_repo")
    def test_sync_exception_marks_failed(
        self, mock_get_repo, mock_update_status, mock_get_token
    ):
        """Test sync exception updates status to failed."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": True,
        }
        mock_get_token.side_effect = Exception("Token generation failed")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "error"
        assert "Token generation failed" in result["error"]
        # Should set in_progress first, then failed
        update_calls = mock_update_status.call_args_list
        assert update_calls[0] == call("ws_test", "acme/my-repo", "in_progress")
        assert update_calls[1][0][2] == "failed"

    @patch.object(handler_module, "get_repo")
    def test_sync_defaults_to_application_repo_type(self, mock_get_repo):
        """Test repo_type defaults to 'application' when not set."""
        mock_get_repo.return_value = {
            "include_code_maps": True,
            # No repo_type key
        }

        # This will fail at get_installation_token but we can verify the default
        with patch.object(handler_module, "update_sync_status"):
            with patch.object(
                handler_module, "get_installation_token", side_effect=Exception("stop")
            ):
                result = process_sync_request({
                    "workspace_id": "ws_test",
                    "repo_name": "acme/my-repo",
                    "installation_id": "12345",
                })

        assert result["status"] == "error"

    @patch.object(handler_module, "get_repo")
    def test_sync_include_code_maps_defaults_true(self, mock_get_repo):
        """Test include_code_maps defaults to True when not set."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            # No include_code_maps key - should default to True
        }

        with patch.object(handler_module, "update_sync_status"):
            with patch.object(
                handler_module, "get_installation_token", side_effect=Exception("stop")
            ):
                result = process_sync_request({
                    "workspace_id": "ws_test",
                    "repo_name": "acme/my-repo",
                    "installation_id": "12345",
                })

        # Should not be skipped - defaults to True
        assert result["status"] == "error"  # Error from token, not skipped


class TestScheduledHeal:
    """Scheduled syncs diff from completed SHAs and heal a commit whose run
    died, within a per-target budget, never by a full rebuild."""

    HEAD = "head_sha_0123456"
    OLD = "older_sha_abcdef"

    def _row(self, **extra):
        row = {
            "repo_type": "application",
            "include_code_maps": True,
            "last_sync_status": "success",
            "added_by": "owner@example.com",
        }
        row.update(extra)
        return row

    def _heal_row(self, **extra):
        """HEAD already dispatched, docs complete, code maps behind."""
        return self._row(
            last_commit_sha=self.HEAD,
            last_docs_sha=self.HEAD,
            last_code_map_sha=self.OLD,
            **extra,
        )

    def _scheduled_sync(
        self,
        repo_row,
        heal_allowed=False,
        running=(),
        compare=frozenset({"README.md"}),
        trigger="scheduled",
        advance_reason="not_closed",
        failed_files=(["a.md"], 1),
        ddb_side_effect=None,
        notify_side_effect=None,
        list_side_effect=None,
    ):
        """Run process_sync_request at HEAD with ``repo_row``. Returns a
        namespace of the result and the mocks."""
        from types import SimpleNamespace

        from shared import notifications, sha_truth  # type: ignore

        def in_flight(_ws, _repo, pipelines, sha):
            assert sha == self.HEAD
            return any(p in running for p in pipelines)

        def compare_fn(*_args):
            return set(compare) if compare is not None else None

        message = {
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
            "trigger": trigger,
        }
        if heal_allowed:
            message["heal_allowed"] = True

        parent = MagicMock()
        with patch.object(handler_module, "get_repo", return_value=repo_row), \
                patch.object(handler_module, "_pipelines_in_flight",
                             side_effect=in_flight), \
                patch.object(handler_module, "update_sync_status") as status, \
                patch.object(handler_module.dual_write, "ddb_update_item",
                             side_effect=ddb_side_effect) as ddb, \
                patch.object(handler_module, "get_installation_token",
                             return_value="tok"), \
                patch.object(handler_module, "resolve_sync_branch",
                             return_value="main"), \
                patch.object(handler_module, "get_latest_commit",
                             return_value=(self.HEAD, 1700000000)), \
                patch.object(handler_module, "get_repo_tree", return_value=[]), \
                patch.object(handler_module, "fetch_changed_files",
                             side_effect=compare_fn) as compare_mock, \
                patch.object(handler_module, "_process_application_repo",
                             return_value={"status": "success"}) as process_app, \
                patch.object(handler_module, "_process_standards_repo",
                             return_value={"status": "success"}) as process_std, \
                patch.object(sha_truth, "advance_past_failures",
                             return_value=MagicMock(reason=advance_reason)) as advance, \
                patch.object(notifications, "notify_workspace",
                             return_value=True,
                             side_effect=notify_side_effect) as notify, \
                patch.object(notifications, "list_failed_files",
                             return_value=failed_files,
                             side_effect=list_side_effect) as list_failed:
            parent.attach_mock(ddb, "ddb")
            parent.attach_mock(process_app, "process_app")
            result = process_sync_request(message)
        return SimpleNamespace(
            result=result, status=status, ddb=ddb, compare=compare_mock,
            process_app=process_app, process_std=process_std,
            advance=advance, notify=notify, list_failed=list_failed,
            parent=parent,
        )

    @staticmethod
    def _ddb_calls(ddb, needle):
        return [
            c for c in ddb.call_args_list
            if needle in c.kwargs.get("UpdateExpression", "")
        ]

    @staticmethod
    def _statuses(status_mock):
        return [c.args[2] for c in status_mock.call_args_list]

    # -- new commit / up to date --------------------------------------

    def test_process_sync_new_commit_dispatches_from_completed_baselines(self):
        """Commit A died, commit B arrived: the diff starts at the last
        COMPLETED SHA, so A's changes are included; heal attrs removed."""
        run = self._scheduled_sync(self._row(
            last_commit_sha="dispatched_a",
            last_docs_sha="docs_done",
            last_code_map_sha="cm_done",
            heal_target_sha="dispatched_a", heal_attempts=2, heal_next_at=1,
        ))
        assert run.result["status"] == "success"
        kwargs = run.process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == "docs_done"
        assert kwargs["code_map_last_sha"] == "cm_done"
        assert kwargs["docs_changed_paths"] is None
        removes = self._ddb_calls(run.ddb, "REMOVE heal_target_sha")
        assert len(removes) == 1
        run.compare.assert_not_called()  # not budgeted, no pre-flight

    def test_process_sync_new_commit_legacy_row_falls_back_to_dispatched_sha(self):
        """No completed columns: both baselines are last_commit_sha (today's
        behaviour), never a full sync."""
        run = self._scheduled_sync(self._row(last_commit_sha="dispatched_a"))
        kwargs = run.process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == "dispatched_a"
        assert kwargs["code_map_last_sha"] == "dispatched_a"
        assert not self._ddb_calls(run.ddb, "REMOVE heal_target_sha")

    def test_process_sync_up_to_date_clears_heal_attrs(self):
        run = self._scheduled_sync(self._row(
            last_commit_sha=self.HEAD, last_docs_sha=self.HEAD,
            last_code_map_sha=self.HEAD,
            heal_target_sha=self.HEAD, heal_attempts=1,
        ))
        assert run.result["status"] == "no_changes"
        assert self._ddb_calls(run.ddb, "REMOVE heal_target_sha")
        assert run.status.call_args.args[2] == "success"

    def test_process_sync_stalled_repo_up_to_date_keeps_stalled(self):
        """units_failed advanced past the failures, so the row is at HEAD;
        the scheduled run must not flip the status back to success."""
        run = self._scheduled_sync(self._row(
            last_commit_sha=self.HEAD, last_docs_sha=self.HEAD,
            last_code_map_sha=self.HEAD, last_sync_status="stalled",
        ))
        assert run.result["status"] == "no_changes"
        assert "success" not in self._statuses(run.status)
        restores = self._ddb_calls(run.ddb, "SET last_sync_status = :prior")
        assert restores[0].kwargs["ExpressionAttributeValues"][":prior"] == "stalled"

    # -- heal gate ------------------------------------------------------

    def test_process_sync_heal_without_slot_is_deferred(self):
        run = self._scheduled_sync(self._heal_row())
        assert run.result["status"] == "heal_deferred"
        run.process_app.assert_not_called()
        assert set(self._statuses(run.status)) == {"in_progress"}
        restores = self._ddb_calls(run.ddb, "SET last_sync_status = :prior")
        assert restores[0].kwargs["ExpressionAttributeValues"][":prior"] == "success"

    def test_process_sync_heal_batch_live_is_already_running_without_status(self):
        run = self._scheduled_sync(
            self._heal_row(), heal_allowed=True, running=("code_map_units",),
        )
        assert run.result["status"] == "already_running"
        run.process_app.assert_not_called()
        assert "already_running" not in self._statuses(run.status)
        assert not self._ddb_calls(run.ddb, "heal_attempts = :n")

    def test_process_sync_heal_first_attempt_recorded_before_dispatch(self):
        before = int(time.time())
        run = self._scheduled_sync(self._heal_row(), heal_allowed=True)
        after = int(time.time())
        assert run.result["status"] == "success"

        records = self._ddb_calls(run.ddb, "SET heal_target_sha = :sha")
        assert len(records) == 1
        values = records[0].kwargs["ExpressionAttributeValues"]
        assert values[":sha"] == self.HEAD
        assert values[":n"] == 1
        assert before + 3600 <= values[":next"] <= after + 3600
        assert "heal_target_sha <> :sha" in records[0].kwargs["ConditionExpression"]

        order = [
            "record" if c[0] == "ddb"
            and "heal_attempts = :n" in c.kwargs.get("UpdateExpression", "")
            else c[0]
            for c in run.parent.mock_calls
        ]
        assert order.index("record") < order.index("process_app")

        kwargs = run.process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == self.HEAD
        assert kwargs["code_map_last_sha"] == self.OLD
        run.compare.assert_called_once_with(
            "tok", "acme", "my-repo", self.OLD, self.HEAD)

    def test_process_sync_heal_docs_behind_passes_preflight_compare(self):
        """The dispatch reuses the pre-flight compare for docs, so it can
        never fall back to a full upload."""
        run = self._scheduled_sync(self._row(
            last_commit_sha=self.HEAD, last_docs_sha=self.OLD,
            last_code_map_sha=self.HEAD,
        ), heal_allowed=True, compare={"docs/a.md"})
        kwargs = run.process_app.call_args.kwargs
        assert kwargs["last_commit_sha"] == self.OLD
        assert kwargs["docs_changed_paths"] == {"docs/a.md"}

    def test_process_sync_heal_shared_baseline_compares_once(self):
        run = self._scheduled_sync(self._row(
            last_commit_sha=self.HEAD, last_docs_sha=self.OLD,
            last_code_map_sha=self.OLD,
        ), heal_allowed=True, compare={"a.md"})
        assert run.result["status"] == "success"
        run.compare.assert_called_once()
        kwargs = run.process_app.call_args.kwargs
        assert kwargs["code_map_last_sha"] == self.OLD
        assert kwargs["docs_changed_paths"] == {"a.md"}

    def test_process_sync_heal_standards_repo_routes_with_compare(self):
        run = self._scheduled_sync(self._row(
            repo_type="standards",
            last_commit_sha=self.HEAD, last_docs_sha=self.OLD,
        ), heal_allowed=True, compare={"adr/1.md"})
        assert run.result["status"] == "success"
        kwargs = run.process_std.call_args.kwargs
        assert kwargs["last_commit_sha"] == self.OLD
        assert kwargs["docs_changed_paths"] == {"adr/1.md"}

    def test_process_sync_heal_inside_backoff_window_backs_off(self):
        run = self._scheduled_sync(self._heal_row(
            heal_target_sha=self.HEAD, heal_attempts=1,
            heal_next_at=int(time.time()) + 1000,
        ), heal_allowed=True)
        assert run.result["status"] == "heal_backoff"
        run.process_app.assert_not_called()
        assert not self._ddb_calls(run.ddb, "SET heal_target_sha = :sha")

    def test_process_sync_heal_second_attempt_doubles_backoff(self):
        before = int(time.time())
        run = self._scheduled_sync(self._heal_row(
            heal_target_sha=self.HEAD, heal_attempts=1,
            heal_next_at=before - 1,
        ), heal_allowed=True)
        assert run.result["status"] == "success"
        record = self._ddb_calls(run.ddb, "SET heal_target_sha = :sha")[0]
        values = record.kwargs["ExpressionAttributeValues"]
        assert values[":n"] == 2
        assert values[":prev"] == 1
        assert values[":next"] >= before + 7200
        assert "heal_attempts = :prev" in record.kwargs["ConditionExpression"]

    def test_process_sync_heal_target_changed_resets_attempts(self):
        run = self._scheduled_sync(self._heal_row(
            heal_target_sha="some_other_sha", heal_attempts=3,
            heal_next_at=int(time.time()) + 99999,
        ), heal_allowed=True)
        assert run.result["status"] == "success"
        values = self._ddb_calls(
            run.ddb, "SET heal_target_sha = :sha")[0].kwargs["ExpressionAttributeValues"]
        assert values[":n"] == 1
        run.advance.assert_not_called()

    def test_process_sync_heal_concurrent_record_does_not_dispatch(self):
        def ddb(*_args, **kwargs):
            if "heal_attempts = :n" in kwargs.get("UpdateExpression", ""):
                raise ClientError(
                    {"Error": {"Code": "ConditionalCheckFailedException"}},
                    "UpdateItem")
            return {}

        run = self._scheduled_sync(
            self._heal_row(), heal_allowed=True, ddb_side_effect=ddb)
        assert run.result["status"] == "heal_backoff"
        run.process_app.assert_not_called()

    def test_process_sync_heal_logs_attempt_line(self, caplog):
        with caplog.at_level("INFO"):
            self._scheduled_sync(self._heal_row(), heal_allowed=True)
        assert (
            "[github-sync] heal attempt 1/3 for acme/my-repo@head_sh: "
            "docs_baseline=head_sh code_map_baseline=older_s"
        ) in caplog.text

    # -- stalled --------------------------------------------------------

    def test_process_sync_budget_exhausted_closed_failures_is_units_failed(self):
        run = self._scheduled_sync(self._heal_row(
            heal_target_sha=self.HEAD, heal_attempts=3, heal_next_at=1,
        ), heal_allowed=True, advance_reason="ok",
            failed_files=(["src/a.py", "src/b.py"], 7))
        assert run.result == {
            "status": "stalled", "reason": "units_failed",
            "commit_sha": self.HEAD,
        }
        run.process_app.assert_not_called()
        run.advance.assert_called_once_with(
            "ws_test", "acme/my-repo", "code_map", self.HEAD, 1700000000)
        stalled = run.status.call_args
        assert stalled.args[2] == "stalled"
        assert stalled.kwargs["last_failure_reason"] == "units_failed"
        run.list_failed.assert_called_once_with(
            "ws_test", "acme/my-repo", self.HEAD)
        notify = run.notify.call_args
        assert notify.args == ("ws_test",)
        assert notify.kwargs["type"] == "repo_stalled"
        assert notify.kwargs["severity"] == "error"
        assert notify.kwargs["reason"] == "units_failed"
        assert notify.kwargs["added_by"] == "owner@example.com"
        assert notify.kwargs["repo_name"] == "acme/my-repo"
        assert notify.kwargs["target_sha"] == self.HEAD
        assert notify.kwargs["failed_files"] == ["src/a.py", "src/b.py"]
        assert notify.kwargs["failed_files_total"] == 7
        assert "Press Sync now to retry" in notify.kwargs["message"]

    def test_process_sync_budget_exhausted_open_batch_is_budget_exhausted(self):
        run = self._scheduled_sync(self._row(
            last_commit_sha=self.HEAD, last_docs_sha=self.OLD,
            last_code_map_sha=self.OLD,
            heal_target_sha=self.HEAD, heal_attempts=3, heal_next_at=1,
        ), heal_allowed=True, advance_reason="not_closed")
        assert run.result["reason"] == "heal_budget_exhausted"
        assert [c.args[2] for c in run.advance.call_args_list] == [
            "docs", "code_map"]
        assert run.status.call_args.kwargs["last_failure_reason"] == (
            "heal_budget_exhausted")
        run.list_failed.assert_not_called()
        notify = run.notify.call_args.kwargs
        assert notify["severity"] == "warning"
        assert notify["failed_files"] == []
        assert notify["failed_files_total"] == 0

    def test_process_sync_heal_compare_failure_is_baseline_unreachable(self):
        run = self._scheduled_sync(
            self._heal_row(), heal_allowed=True, compare=None)
        assert run.result["reason"] == "baseline_unreachable"
        run.process_app.assert_not_called()
        assert not self._ddb_calls(run.ddb, "SET heal_target_sha = :sha")
        assert run.status.call_args.args[2] == "stalled"
        assert run.status.call_args.kwargs["last_failure_reason"] == (
            "baseline_unreachable")
        assert run.notify.call_args.kwargs["reason"] == "baseline_unreachable"

    def test_process_sync_notifier_failure_does_not_fail_sync(self, caplog):
        with caplog.at_level("WARNING"):
            run = self._scheduled_sync(
                self._heal_row(), heal_allowed=True, compare=None,
                notify_side_effect=RuntimeError("table gone"))
        assert run.result["status"] == "stalled"
        assert "stalled notification failed for acme/my-repo" in caplog.text

    def test_process_sync_failed_files_read_error_still_notifies(self, caplog):
        with caplog.at_level("WARNING"):
            run = self._scheduled_sync(self._heal_row(
                heal_target_sha=self.HEAD, heal_attempts=3,
            ), heal_allowed=True, advance_reason="ok",
                list_side_effect=RuntimeError("throttled"))
        assert run.result["reason"] == "units_failed"
        assert run.status.call_args.args[2] == "stalled"
        notify = run.notify.call_args.kwargs
        assert notify["failed_files"] == []
        assert notify["failed_files_total"] == 0
        assert "failed-files read failed" in caplog.text

    # -- manual ---------------------------------------------------------

    def test_process_sync_manual_removes_heal_attrs(self):
        run = self._scheduled_sync(self._heal_row(
            heal_target_sha=self.HEAD, heal_attempts=3, heal_next_at=1,
        ), trigger="manual")
        assert run.result["status"] == "success"
        assert self._ddb_calls(run.ddb, "REMOVE heal_target_sha")
        run.advance.assert_not_called()
        run.compare.assert_not_called()
        assert run.process_app.call_args.kwargs["code_map_last_sha"] == self.OLD


class TestHealHelpers:
    """Row-write helpers behind the heal flow."""

    def test_restore_sync_status_without_prior_removes(self):
        with patch.object(handler_module.dual_write, "ddb_update_item") as ddb:
            handler_module._restore_sync_status("ws", "o/r", None)
        kwargs = ddb.call_args.kwargs
        assert kwargs["UpdateExpression"] == "REMOVE last_sync_status"
        assert "last_sync_status = :ip" in kwargs["ConditionExpression"]

    def test_restore_sync_status_swallows_errors(self, caplog):
        ccf = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        with patch.object(handler_module.dual_write, "ddb_update_item",
                          side_effect=ccf):
            handler_module._restore_sync_status("ws", "o/r", "success")
        with patch.object(handler_module.dual_write, "ddb_update_item",
                          side_effect=RuntimeError("boom")), \
                caplog.at_level("WARNING"):
            handler_module._restore_sync_status("ws", "o/r", "success")
        assert "status restore failed for o/r" in caplog.text

    def test_clear_heal_state_swallows_errors(self, caplog):
        ccf = ClientError(
            {"Error": {"Code": "ConditionalCheckFailedException"}}, "UpdateItem")
        with patch.object(handler_module.dual_write, "ddb_update_item",
                          side_effect=ccf):
            handler_module._clear_heal_state("ws", "o/r")
        with patch.object(handler_module.dual_write, "ddb_update_item",
                          side_effect=RuntimeError("boom")), \
                caplog.at_level("WARNING"):
            handler_module._clear_heal_state("ws", "o/r")
        assert "heal state clear failed for o/r" in caplog.text

    def test_record_heal_attempt_aws_error_propagates(self):
        """Dispatching without a recorded attempt would bypass the budget."""
        with patch.object(handler_module.dual_write, "ddb_update_item",
                          side_effect=ClientError(
                              {"Error": {"Code": "ProvisionedThroughputExceededException"}},
                              "UpdateItem")):
            with pytest.raises(ClientError):
                handler_module._record_heal_attempt(
                    "ws", "o/r", "sha", 0, False, 1)

    def test_advance_past_failures_error_means_not_advanced(self):
        from shared import sha_truth  # type: ignore

        with patch.object(sha_truth, "advance_past_failures",
                          side_effect=ValueError("bad pipeline")):
            assert not handler_module._advance_past_failures(
                "ws", "o/r", "docs", "sha1234", 1)

    @pytest.mark.parametrize("status", [
        "heal_deferred", "heal_backoff", "stalled", "already_running",
    ])
    def test_lambda_handler_heal_outcomes_are_not_redelivered(self, status):
        with patch.object(handler_module, "process_sync_request",
                          return_value={"status": status}):
            out = lambda_handler({"Records": [
                {"messageId": "m1", "body": json.dumps({"workspace_id": "w"})},
            ]}, None)
        assert out == {"batchItemFailures": []}


class TestDocContentFetch:
    """Doc content comes from the synced commit; bad bytes fail one unit."""

    def _github_response(self, raw: bytes):
        import base64

        body = json.dumps({
            "type": "file",
            "content": base64.b64encode(raw).decode(),
        }).encode()
        resp = MagicMock()
        resp.__enter__.return_value.read.return_value = body
        return resp

    def test_get_file_content_non_utf8_returns_none_and_records_reason(self, caplog):
        failures = {}
        with patch("urllib.request.urlopen",
                   return_value=self._github_response(b"\xff\xfe\x00bad")), \
                caplog.at_level("WARNING"):
            out = handler_module.get_file_content(
                "tok", "acme", "repo", "docs/latin1.txt", "sha1",
                failures=failures)
        assert out is None
        assert failures == {"docs/latin1.txt": "decode_error"}
        assert "docs/latin1.txt" in caplog.text
        assert "not valid UTF-8" in caplog.text

    def test_get_file_content_utf8_decodes(self):
        with patch("urllib.request.urlopen",
                   return_value=self._github_response("hello".encode())):
            assert handler_module.get_file_content(
                "tok", "acme", "repo", "a.md", "sha1") == "hello"

    @staticmethod
    def _content_side_effect(token, owner, repo, path, ref, failures=None):
        if path == "bad.md":
            failures[path] = "decode_error"
            return None
        return "# ok"

    def test_application_repo_decode_error_marks_unit_and_continues(self):
        tree = [
            {"path": "bad.md", "type": "blob", "sha": "a", "size": 10},
            {"path": "good.md", "type": "blob", "sha": "b", "size": 10},
        ]
        with patch.object(handler_module, "fetch_ingestion_config",
                          return_value=[]), \
                patch.object(handler_module, "get_file_content",
                             side_effect=self._content_side_effect) as get_content, \
                patch.object(handler_module, "upload_file_to_s3",
                             return_value="key"), \
                patch.object(handler_module, "_mark_docs_unit_fetch_failed") as mark, \
                patch.object(handler_module, "_open_docs_pending_batch"), \
                patch.object(handler_module, "_queue_orphan_detection"), \
                patch.object(handler_module, "_open_code_map_dispatch_ledger"), \
                patch.object(handler_module, "queue_repo_for_code_maps",
                             return_value=True), \
                patch.object(handler_module, "update_sync_status"):
            result = _process_application_repo(
                workspace_id="ws_test", repo_name="acme/my-repo",
                repo_type="application", installation_id="1",
                owner="acme", repo_short="my-repo", token="tok",
                default_branch="main", tree=tree,
                current_sha="commit123", current_commit_timestamp=1,
            )
        assert result["status"] == "success"
        assert result["docs_uploaded"] == 1
        mark.assert_called_once_with(
            "ws_test", "acme/my-repo", "commit123", "bad.md",
            failure_reason="decode_error")
        assert {c.args[4] for c in get_content.call_args_list} == {"commit123"}

    def test_standards_repo_fetches_docs_at_commit_sha(self):
        tree = [
            {"path": "bad.md", "type": "blob", "sha": "a", "size": 10},
            {"path": "adr.md", "type": "blob", "sha": "b", "size": 10},
        ]
        with patch.object(handler_module, "get_file_content",
                          side_effect=self._content_side_effect) as get_content, \
                patch.object(handler_module, "upload_file_to_s3",
                             return_value="key"), \
                patch.object(handler_module, "_mark_docs_unit_fetch_failed") as mark, \
                patch.object(handler_module, "_open_docs_pending_batch"), \
                patch.object(handler_module, "_queue_orphan_detection"), \
                patch.object(handler_module, "update_sync_status"):
            result = _process_standards_repo(
                workspace_id="ws_test", repo_name="acme/standards",
                repo_type="standards", tree=tree, current_sha="commit123",
                current_commit_timestamp=1, token="tok", owner="acme",
                repo_short="standards", default_branch="main",
            )
        assert result["files_uploaded"] == 1
        assert {c.args[4] for c in get_content.call_args_list} == {"commit123"}
        assert mark.call_args.kwargs == {"failure_reason": "decode_error"}

    def test_mark_docs_unit_fetch_failed_passes_reason(self):
        from shared import sha_truth  # type: ignore

        with patch.object(sha_truth, "mark_unit_failed") as mark:
            handler_module._mark_docs_unit_fetch_failed(
                "ws", "o/r", "sha", "x.md", failure_reason="decode_error")
        assert mark.call_args.kwargs["failure_reason"] == "decode_error"

    def test_filter_docs_uses_precomputed_changed_paths(self):
        with patch.object(handler_module, "fetch_changed_files") as compare:
            out = handler_module._filter_docs_to_changed(
                "tok", "acme", "r",
                [{"path": "a.md"}, {"path": "b.md"}],
                "old", "new", changed_paths={"b.md"})
        assert out == [{"path": "b.md"}]
        compare.assert_not_called()


# ============================================================================
# Lambda Handler
# ============================================================================


class TestLambdaHandler:
    """Test main Lambda handler SQS processing."""

    @patch.object(handler_module, "process_sync_request")
    def test_processes_single_sqs_record(self, mock_process):
        """Test handler processes a single SQS record."""
        mock_process.return_value = {"status": "success", "files_queued": 10}

        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "body": json.dumps({
                        "workspace_id": "ws_test",
                        "repo_name": "acme/my-repo",
                        "installation_id": "12345",
                    }),
                }
            ]
        }

        result = lambda_handler(event, None)

        # handler now returns SQS partial-batch-failure envelope.
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, "process_sync_request")
    def test_processes_multiple_sqs_records(self, mock_process):
        """Test handler processes multiple SQS records."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "body": json.dumps({
                        "workspace_id": "ws1",
                        "repo_name": "org/repo1",
                        "installation_id": "111",
                    }),
                },
                {
                    "messageId": "msg-002",
                    "body": json.dumps({
                        "workspace_id": "ws2",
                        "repo_name": "org/repo2",
                        "installation_id": "222",
                    }),
                },
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_handles_invalid_json_in_record(self):
        """Invalid JSON messages must land in batchItemFailures so SQS
        redelivers -> DLQ instead of silently deleting the poison pill."""
        event = {
            "Records": [
                {"messageId": "msg-bad", "body": "not-json!!!"}
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}

    def test_handles_empty_records(self):
        """Test handler handles empty Records list."""
        event = {"Records": []}

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    def test_handles_missing_records_key(self):
        """Test handler handles missing Records key gracefully."""
        result = lambda_handler({}, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, "process_sync_request")
    def test_handles_processing_exception(self, mock_process):
        """Processing exceptions must land in batchItemFailures so SQS
        redelivers instead of silently deleting the message on 200."""
        mock_process.side_effect = Exception("Unexpected boom")

        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "body": json.dumps({
                        "workspace_id": "ws_test",
                        "repo_name": "acme/repo",
                        "installation_id": "123",
                    }),
                }
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-001"}]}

    @patch.object(handler_module, "process_sync_request")
    def test_counts_no_changes_as_success(self, mock_process):
        """Test no_changes status is counted as success (no retry)."""
        mock_process.return_value = {"status": "no_changes", "commit_sha": "abc"}

        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "body": json.dumps({
                        "workspace_id": "ws_test",
                        "repo_name": "acme/repo",
                        "installation_id": "123",
                    }),
                }
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, "process_sync_request")
    def test_counts_skipped_as_success(self, mock_process):
        """Test skipped status is counted as success (no retry)."""
        mock_process.return_value = {"status": "skipped", "reason": "Code maps disabled"}

        event = {
            "Records": [
                {
                    "messageId": "msg-001",
                    "body": json.dumps({
                        "workspace_id": "ws_test",
                        "repo_name": "acme/repo",
                        "installation_id": "123",
                    }),
                }
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, "process_sync_request")
    def test_mixed_success_and_failure(self, mock_process):
        """only failed records land in batchItemFailures; success
        + no_changes are ack'd. Reference-implementation test for the
        partial-batch-failure contract."""
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "Failed"},
            {"status": "no_changes", "commit_sha": "abc"},
        ]

        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"workspace_id": "w1", "repo_name": "o/r1", "installation_id": "1"})},
                {"messageId": "msg-2", "body": json.dumps({"workspace_id": "w2", "repo_name": "o/r2", "installation_id": "2"})},
                {"messageId": "msg-3", "body": json.dumps({"workspace_id": "w3", "repo_name": "o/r3", "installation_id": "3"})},
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-2"}]}

    @patch.object(handler_module, "process_sync_request")
    def test_only_failed_record_retried(self, mock_process):
        """Middle record raises -> only that itemIdentifier is retried,
        surrounding records are ack'd (SQS deletes them)."""
        def side_effect(msg):
            if msg.get("repo_name") == "o/r2":
                raise RuntimeError("upstream 503")
            return {"status": "success"}

        mock_process.side_effect = side_effect

        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"workspace_id": "w1", "repo_name": "o/r1", "installation_id": "1"})},
                {"messageId": "msg-2", "body": json.dumps({"workspace_id": "w2", "repo_name": "o/r2", "installation_id": "2"})},
                {"messageId": "msg-3", "body": json.dumps({"workspace_id": "w3", "repo_name": "o/r3", "installation_id": "3"})},
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-2"}]}


# ============================================================================
# GitHub App Authentication
# ============================================================================


class TestGetGitHubConfig:
    """Test GitHub App configuration retrieval from SSM."""

    def test_get_github_config_success(self):
        """Test successful config retrieval from SSM."""
        handler_module._github_config = None
        handler_module.ENV = "dev"
        handler_module.APP_NAME = "outcome-ops-ai-assist"

        mock_ssm_client.get_parameters.return_value = {
            "Parameters": [
                {"Name": "/dev/outcome-ops-ai-assist/github/app-id", "Value": "123456"},
                {"Name": "/dev/outcome-ops-ai-assist/github/private-key", "Value": "-----BEGIN RSA PRIVATE KEY-----"},
            ]
        }

        config = handler_module.get_github_config()

        assert config["app_id"] == "123456"
        assert config["private_key"] == "-----BEGIN RSA PRIVATE KEY-----"
        mock_ssm_client.get_parameters.assert_called_once()

    def test_get_github_config_cached(self):
        """Test config is cached after first retrieval."""
        handler_module._github_config = {
            "app_id": "cached_id",
            "private_key": "cached_key",
        }

        config = handler_module.get_github_config()

        assert config["app_id"] == "cached_id"
        mock_ssm_client.get_parameters.assert_not_called()

    def test_get_github_config_ssm_error(self):
        """Test SSM error is propagated."""
        handler_module._github_config = None
        mock_ssm_client.get_parameters.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameters",
        )

        with pytest.raises(ClientError):
            handler_module.get_github_config()


class TestGenerateJwt:
    """Test JWT generation for GitHub App."""

    @patch.object(handler_module, "get_github_config")
    def test_generate_jwt_placeholder_app_id(self, mock_config):
        """Test raises ValueError for placeholder app_id."""
        mock_config.return_value = {
            "app_id": "PLACEHOLDER_CONFIGURE_ME",
            "private_key": "some-key",
        }

        with pytest.raises(ValueError, match="App ID not configured"):
            handler_module.generate_jwt()

    @patch.object(handler_module, "get_github_config")
    def test_generate_jwt_empty_app_id(self, mock_config):
        """Test raises ValueError for empty app_id."""
        mock_config.return_value = {
            "app_id": "",
            "private_key": "some-key",
        }

        with pytest.raises(ValueError, match="App ID not configured"):
            handler_module.generate_jwt()

    @patch.object(handler_module, "get_github_config")
    def test_generate_jwt_placeholder_private_key(self, mock_config):
        """Test raises ValueError for placeholder private_key."""
        mock_config.return_value = {
            "app_id": "123456",
            "private_key": "PLACEHOLDER_CONFIGURE_ME",
        }

        with pytest.raises(ValueError, match="private key not configured"):
            handler_module.generate_jwt()

    @patch.object(handler_module, "get_github_config")
    def test_generate_jwt_empty_private_key(self, mock_config):
        """Test raises ValueError for empty private_key."""
        mock_config.return_value = {
            "app_id": "123456",
            "private_key": "",
        }

        with pytest.raises(ValueError, match="private key not configured"):
            handler_module.generate_jwt()


# ============================================================================
# Repo removed mid-flight (ADR-052): start, before upload, before dispatch
# ============================================================================


class TestRepoRemovedGuards:
    """Each sync re-checks repo presence at three points and stops cleanly
    (non-retry status) once the repo row is gone."""

    _MSG = {
        "workspace_id": "ws_test",
        "repo_name": "acme/my-repo",
        "installation_id": "12345",
        "trigger": "manual",
    }
    _TREE = [
        {"path": "README.md", "type": "blob", "sha": "aaa", "size": 500},
        {"path": "src/main.py", "type": "blob", "sha": "bbb", "size": 1200},
    ]

    def _run(self, _presence, presence_side_effect, repo_type="application"):
        """Full process_sync_request with GitHub, S3 and SQS stubbed."""
        _presence.side_effect = presence_side_effect
        repo_row = {
            "repo_type": repo_type,
            "include_code_maps": True,
            "last_sync_status": "success",
        }
        mocks = {}
        names = {
            "get_repo": {"return_value": repo_row},
            "update_sync_status": {},
            "get_installation_token": {"return_value": "tok"},
            "resolve_sync_branch": {"return_value": "main"},
            "get_latest_commit": {"return_value": ("head123", 1700000000)},
            "get_repo_tree": {"return_value": self._TREE},
            "fetch_ingestion_config": {"return_value": []},
            "get_file_content": {"return_value": "# README"},
            "upload_file_to_s3": {"return_value": "key"},
            "_open_docs_pending_batch": {},
            "_advance_docs_sha_no_changes": {},
            "_queue_orphan_detection": {},
            "_open_code_map_dispatch_ledger": {},
            "queue_repo_for_code_maps": {"return_value": True},
            "log_source_sync": {},
        }
        patchers = [
            patch.object(handler_module, n, **kw) for n, kw in names.items()
        ]
        try:
            for n, p in zip(names, patchers):
                mocks[n] = p.start()
            result = process_sync_request(dict(self._MSG))
        finally:
            for p in patchers:
                p.stop()
        return result, mocks

    def test_removed_at_start_makes_no_github_calls(self, _presence, caplog):
        caplog.set_level("INFO")
        result, m = self._run(_presence, [False])
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "start"
        assert "repo_removed" in handler_module._NON_RETRY_STATUSES
        m["get_repo"].assert_not_called()
        m["update_sync_status"].assert_not_called()
        m["get_installation_token"].assert_not_called()
        m["get_latest_commit"].assert_not_called()
        m["upload_file_to_s3"].assert_not_called()
        m["queue_repo_for_code_maps"].assert_not_called()
        _presence.assert_called_once_with("ws_test", "github", "acme/my-repo")
        assert (
            "[github-sync] repo removed, dropping sync for ws_test/acme/my-repo"
            in caplog.text
        )

    def test_removed_before_upload_uploads_and_dispatches_nothing(self, _presence):
        result, m = self._run(_presence, [True, False])
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "before_upload"
        m["get_repo_tree"].assert_called_once()
        m["_open_docs_pending_batch"].assert_not_called()
        m["upload_file_to_s3"].assert_not_called()
        m["_queue_orphan_detection"].assert_not_called()
        m["_open_code_map_dispatch_ledger"].assert_not_called()
        m["queue_repo_for_code_maps"].assert_not_called()

    def test_standards_removed_before_upload_uploads_nothing(self, _presence):
        result, m = self._run(_presence, [True, False], repo_type="standards")
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "before_upload"
        m["_open_docs_pending_batch"].assert_not_called()
        m["upload_file_to_s3"].assert_not_called()
        m["_queue_orphan_detection"].assert_not_called()

    def test_removed_before_dispatch_keeps_uploads_opens_no_ledger(self, _presence):
        result, m = self._run(_presence, [True, True, False])
        assert result["status"] == "repo_removed"
        assert result["checkpoint"] == "before_dispatch"
        assert m["upload_file_to_s3"].call_count == 1
        m["_open_code_map_dispatch_ledger"].assert_not_called()
        m["queue_repo_for_code_maps"].assert_not_called()

    def test_connected_runs_all_three_checks_and_dispatches(self, _presence):
        result, m = self._run(_presence, [True, True, True])
        assert result["status"] == "success"
        assert _presence.call_count == 3
        m["_open_code_map_dispatch_ledger"].assert_called_once()
        m["queue_repo_for_code_maps"].assert_called_once()

    def test_lookup_error_at_start_raises(self, _presence):
        from shared.repo_presence import RepoPresenceError
        with pytest.raises(RepoPresenceError):
            self._run(_presence, RepoPresenceError("ddb down"))

    def test_lookup_error_mid_sync_is_a_retryable_error(self, _presence):
        from shared.repo_presence import RepoPresenceError
        result, m = self._run(_presence, [True, True, RepoPresenceError("ddb down")])
        assert result["status"] == "error"
        assert result["status"] not in handler_module._NON_RETRY_STATUSES
        m["queue_repo_for_code_maps"].assert_not_called()

    def test_lambda_handler_removed_is_not_redelivered(self, _presence):
        _presence.return_value = False
        event = {"Records": [{"messageId": "m1", "body": json.dumps(self._MSG)}]}
        assert lambda_handler(event, None) == {"batchItemFailures": []}

    def test_lambda_handler_lookup_error_is_redelivered(self, _presence):
        from shared.repo_presence import RepoPresenceError
        _presence.side_effect = RepoPresenceError("ddb down")
        event = {"Records": [{"messageId": "m1", "body": json.dumps(self._MSG)}]}
        assert lambda_handler(event, None) == {
            "batchItemFailures": [{"itemIdentifier": "m1"}],
        }
