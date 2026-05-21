"""Unit tests for github-sync Lambda handler."""

import importlib.util
import json
import os
import sys
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


def _make_license_module_stub():
    """Stand-in for the outcomeops_license layer in unit tests.

    The handler imports validate_license / check_repo_limit at module
    scope. The layer is mounted at /opt/python in the Lambda runtime;
    in unit tests we inject a MagicMock module so the imports succeed
    and individual tests can monkeypatch behaviour.
    """
    stub = MagicMock()
    stub.validate_license.return_value = {"sub": "test-customer"}

    class _Result:
        def __init__(self, allowed=True, reason=None, current=0, limit=100):
            self.allowed = allowed
            self.reason = reason
            self.current = current
            self.limit = limit

    stub.check_repo_limit.side_effect = lambda **_: _Result()
    return stub


def _load_handler_module():
    """Load or reload the github-sync handler module."""
    if "github_sync_handler" in sys.modules:
        del sys.modules["github_sync_handler"]

    # Inject a stub outcomeops_license layer so the handler's top-level
    # `from outcomeops_license import ...` succeeds in the test environment.
    sys.modules["outcomeops_license"] = _make_license_module_stub()

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
        # plan-git-truth chunk 02 added diagnostic kwargs; assert by
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
        # plan-git-truth chunk 02 added diagnostic kwargs.
        mock_update_status.assert_called_once()
        args, kwargs = mock_update_status.call_args
        assert args == ("ws_test", "acme/my-repo", "success")
        assert kwargs.get("commit_sha") == "commit123"

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
        # plan-git-truth chunk 02 added diagnostic kwargs
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

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed"] == 1
        assert body["successes"] == 1
        assert body["failures"] == 0
        assert len(body["results"]) == 1
        assert body["results"][0]["message_id"] == "msg-001"

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
        body = json.loads(result["body"])
        assert body["processed"] == 2
        assert body["successes"] == 2
        assert body["failures"] == 0

    def test_handles_invalid_json_in_record(self):
        """Test handler handles invalid JSON in SQS message body."""
        event = {
            "Records": [
                {"messageId": "msg-bad", "body": "not-json!!!"}
            ]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["failures"] == 1
        assert body["results"][0]["result"]["status"] == "error"
        assert "Invalid JSON" in body["results"][0]["result"]["error"]

    def test_handles_empty_records(self):
        """Test handler handles empty Records list."""
        event = {"Records": []}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed"] == 0
        assert body["successes"] == 0
        assert body["failures"] == 0

    def test_handles_missing_records_key(self):
        """Test handler handles missing Records key gracefully."""
        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["processed"] == 0

    @patch.object(handler_module, "process_sync_request")
    def test_handles_processing_exception(self, mock_process):
        """Test handler catches unexpected processing exceptions."""
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
        body = json.loads(result["body"])
        assert body["failures"] == 1
        assert body["results"][0]["result"]["status"] == "error"
        assert "Unexpected boom" in body["results"][0]["result"]["error"]

    @patch.object(handler_module, "process_sync_request")
    def test_counts_no_changes_as_success(self, mock_process):
        """Test no_changes status is counted as success."""
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
        body = json.loads(result["body"])
        assert body["successes"] == 1
        assert body["failures"] == 0

    @patch.object(handler_module, "process_sync_request")
    def test_counts_skipped_as_success(self, mock_process):
        """Test skipped status is counted as success."""
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
        body = json.loads(result["body"])
        assert body["successes"] == 1
        assert body["failures"] == 0

    @patch.object(handler_module, "process_sync_request")
    def test_mixed_success_and_failure(self, mock_process):
        """Test correct counting with mixed results."""
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
        body = json.loads(result["body"])
        assert body["processed"] == 3
        assert body["successes"] == 2
        assert body["failures"] == 1

    @patch.object(handler_module, "process_sync_request")
    def test_message_id_preserved_in_results(self, mock_process):
        """Test message IDs are preserved in results."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {
                    "messageId": "unique-msg-id-abc",
                    "body": json.dumps({
                        "workspace_id": "ws_test",
                        "repo_name": "acme/repo",
                        "installation_id": "123",
                    }),
                }
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["message_id"] == "unique-msg-id-abc"


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
# License layer integration (ADR-019)
# ============================================================================


class _LimitResult:
    """Lightweight stand-in for outcomeops_license.EnforcementResult."""

    def __init__(self, allowed=True, reason=None, current=0, limit=100):
        self.allowed = allowed
        self.reason = reason
        self.current = current
        self.limit = limit


class TestLicenseValidation:
    """Test license validation in lambda_handler entry path (ADR-019)."""

    def test_lambda_handler_license_valid(self, monkeypatch):
        """Valid license claims allow processing to continue."""
        monkeypatch.setattr(
            handler_module,
            "validate_license",
            lambda: {"sub": "acme-customer", "license": {"tier": "team"}},
        )
        monkeypatch.setattr(
            handler_module, "process_sync_request", lambda msg: {"status": "success"}
        )

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
        result = handler_module.lambda_handler(event, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["successes"] == 1

    def test_lambda_handler_license_import_error(self, monkeypatch):
        """Missing layer (ImportError) emits LICENSE_FAIL_OPEN and re-raises."""

        def raise_import(*args, **kwargs):
            raise ImportError("No module named 'outcomeops_license'")

        monkeypatch.setattr(handler_module, "validate_license", raise_import)
        mock_log = MagicMock()
        monkeypatch.setattr(handler_module, "log_license_fail_open", mock_log)

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
        with pytest.raises(ImportError):
            handler_module.lambda_handler(event, None)

        mock_log.assert_called_once()
        # First positional arg is the audit table name; second is the lambda
        # short name used for the regression-detector audit row.
        args, _ = mock_log.call_args
        assert args[1] == "github-sync"

    def test_lambda_handler_license_invalid(self, monkeypatch):
        """Other license errors (expired/invalid) re-raise without fail-open log."""

        def raise_invalid(*args, **kwargs):
            raise RuntimeError("license expired")

        monkeypatch.setattr(handler_module, "validate_license", raise_invalid)
        mock_log = MagicMock()
        monkeypatch.setattr(handler_module, "log_license_fail_open", mock_log)

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
        with pytest.raises(RuntimeError, match="license expired"):
            handler_module.lambda_handler(event, None)

        # log_license_fail_open is for the import-error / unattached-layer
        # case only; a runtime validation failure should not emit it.
        mock_log.assert_not_called()


class TestRepoLimitEnforcement:
    """Test per-source check_repo_limit (ADR-019).

    Liveness tracking (license layer v2.0.0 desired-state snapshot via
    report_repos_snapshot) is owned by the github-integration connect/
    disconnect handlers and tested in those test modules.
    """

    @patch.object(handler_module, "get_repo")
    def test_check_repo_limit_blocked_skips_source(self, mock_get_repo, monkeypatch):
        """When check_repo_limit returns allowed=False, sync is blocked and
        update_sync_status is called with status='blocked_license_limit'."""
        mock_get_repo.return_value = {
            "repo_type": "application",
            "include_code_maps": True,
        }
        monkeypatch.setattr(
            handler_module,
            "check_repo_limit",
            lambda **_: _LimitResult(
                allowed=False, reason="repo limit reached", current=10, limit=10
            ),
        )
        mock_update = MagicMock()
        monkeypatch.setattr(handler_module, "update_sync_status", mock_update)
        # Ensure we never even attempt to fetch upstream once blocked.
        monkeypatch.setattr(
            handler_module,
            "get_installation_token",
            MagicMock(side_effect=AssertionError("must not call upstream")),
        )

        result = handler_module.process_sync_request({
            "workspace_id": "ws_test",
            "repo_name": "acme/my-repo",
            "installation_id": "12345",
        })

        assert result["status"] == "blocked"
        assert result["reason"] == "repo limit reached"
        # Verify the blocked_license_limit status write happened.
        statuses = [c.args[2] for c in mock_update.call_args_list]
        assert "blocked_license_limit" in statuses

