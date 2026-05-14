"""Unit tests for file-ingestion Lambda - Outlook email and attachment handlers."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

mock_dynamodb_resource = MagicMock()
mock_s3_client = MagicMock()
mock_ssm_client = MagicMock()

# Pre-create mock modules for local imports in process_outlook_attachment
mock_outlook_api_module = MagicMock()
mock_outlook_token_module = MagicMock()


def _load_handler_module():
    """Load or reload the file-ingestion handler module."""
    if 'file_ingestion_handler' in sys.modules:
        del sys.modules['file_ingestion_handler']

    # Add file-ingestion dir to path for sibling imports
    fi_dir = os.path.join(os.path.dirname(__file__), '../../file-ingestion')
    if fi_dir not in sys.path:
        sys.path.insert(0, fi_dir)

    handler_path = os.path.join(fi_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("file_ingestion_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['file_ingestion_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.s3_client = mock_s3_client
    handler_module.ssm_client = mock_ssm_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
process_outlook_email = handler_module.process_outlook_email
process_outlook_attachment = handler_module.process_outlook_attachment
_load_microsoft_oauth_config = handler_module._load_microsoft_oauth_config
sanitize_filename = handler_module.sanitize_filename


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_s3_client.reset_mock()
    mock_s3_client.put_object.side_effect = None
    mock_ssm_client.reset_mock()
    mock_ssm_client.get_parameter.side_effect = None
    handler_module._microsoft_oauth_config = None
    handler_module._oauth_config = None
    handler_module.KB_BUCKET = "test-kb-bucket"
    yield


@pytest.fixture(autouse=True)
def stub_outcomeops_license():
    """Inject a permissive outcomeops_license stub for every test (ADR-019).

    Mirrors the stub in test_file_ingestion_confluence.py - the handler
    imports validate_license / check_repo_limit inside lambda_handler so
    the license module must exist in sys.modules before each invocation.
    """
    stub = MagicMock()
    stub.validate_license.return_value = {"sub": "test-customer"}
    allowed = MagicMock()
    allowed.allowed = True
    allowed.reason = None
    allowed.current = 0
    allowed.limit = 100
    stub.check_repo_limit.return_value = allowed
    saved = sys.modules.get("outcomeops_license")
    sys.modules["outcomeops_license"] = stub
    yield stub
    if saved is not None:
        sys.modules["outcomeops_license"] = saved
    else:
        sys.modules.pop("outcomeops_license", None)


@pytest.fixture
def email_file_info():
    """Sample email file info from outlook-sync queue."""
    return {
        "message_id": "AAMkAGE1MDFmNTg4LWE0YzgtNDZmYy1iYTRjLTM5YmI2NmVkMWQxYQ==",
        "subject": "Q4 Planning - Action Items",
        "folder_name": "Inbox",
        "from": "manager@example.com",
        "received_at": "2024-02-24T10:00:00Z",
        "body_content": "<p>Here are the action items from our Q4 planning meeting.</p>",
        "body_content_type": "html",
        "has_attachments": True,
    }


@pytest.fixture
def attachment_file_info():
    """Sample attachment file info from outlook-sync queue."""
    return {
        "attachment_id": "AAMkAGE1MDFmNTg4att001",
        "message_id": "AAMkAGE1MDFmNTg4LWE0YzgtNDZmYy1iYTRjLTM5YmI2NmVkMWQxYQ==",
        "folder_name": "Inbox",
        "filename": "Q4_Planning_Slides.pdf",
        "content_type": "application/pdf",
        "size": 2048000,
    }


# ============================================================================
# Load Microsoft OAuth Config
# ============================================================================

class TestLoadMicrosoftOAuthConfig:
    """Test loading Microsoft OAuth config from SSM."""

    def test_load_config_success(self):
        """Test loading config from SSM."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "ms-client-id"}},
            {"Parameter": {"Value": "ms-client-secret"}},
        ]

        config = _load_microsoft_oauth_config()
        assert config["client_id"] == "ms-client-id"
        assert config["client_secret"] == "ms-client-secret"

    def test_load_config_ssm_error(self):
        """Test raises on SSM error."""
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

        with pytest.raises(ClientError):
            _load_microsoft_oauth_config()

    def test_load_config_caches_result(self):
        """Test config is cached after first load."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "cid"}},
            {"Parameter": {"Value": "csec"}},
        ]

        _load_microsoft_oauth_config()
        _load_microsoft_oauth_config()
        # SSM should only be called twice (two params), not four
        assert mock_ssm_client.get_parameter.call_count == 2


# ============================================================================
# Process Outlook Email
# ============================================================================

class TestProcessOutlookEmail:
    """Test Outlook email processing and S3 upload."""

    def test_process_email_success(self, email_file_info):
        """Test successful email upload to S3."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_outlook_email("ws_test", "conn_test", email_file_info, sync_id="sync_001")

        assert result is not None
        assert "workspaces/ws_test/outlook/" in result
        assert result.endswith(".html")
        mock_s3_client.put_object.assert_called_once()

        # Verify S3 call
        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["Bucket"] == "test-kb-bucket"
        assert call_args["ContentType"] == "text/html"
        assert call_args["Metadata"]["source"] == "outlook"
        assert call_args["Metadata"]["workspace_id"] == "ws_test"
        assert call_args["Metadata"]["connection_id"] == "conn_test"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_email_html_body(self, email_file_info):
        """Test HTML body is included in uploaded content."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_outlook_email("ws_test", "conn_test", email_file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "Q4 Planning - Action Items" in body
        assert "manager@example.com" in body
        assert "action items from our Q4 planning" in body

    def test_process_email_text_body(self):
        """Test plain text body is wrapped in pre tags."""
        file_info = {
            "message_id": "msg_001",
            "subject": "Plain Text Email",
            "folder_name": "Inbox",
            "from": "sender@example.com",
            "received_at": "2024-01-01T00:00:00Z",
            "body_content": "Hello, this is plain text.",
            "body_content_type": "text",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_outlook_email("ws_test", "conn_test", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "<pre>" in body
        assert "Hello, this is plain text." in body

    def test_process_email_empty_body(self):
        """Test email with empty body still uploads placeholder."""
        file_info = {
            "message_id": "msg_empty",
            "subject": "Empty Email",
            "folder_name": "Inbox",
            "from": "sender@example.com",
            "received_at": "2024-01-01T00:00:00Z",
            "body_content": "",
            "body_content_type": "html",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_outlook_email("ws_test", "conn_test", file_info)
        assert result is not None
        mock_s3_client.put_object.assert_called_once()

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        assert "No content" in body

    def test_process_email_s3_path_structure(self, email_file_info):
        """Test S3 key follows expected path structure."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        result = process_outlook_email("ws_test", "conn_test", email_file_info)

        assert result.startswith("workspaces/ws_test/outlook/")
        assert "Inbox/" in result

    def test_process_email_s3_error(self, email_file_info):
        """Test returns None on S3 error."""
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutObject"
        )

        result = process_outlook_email("ws_test", "conn_test", email_file_info)
        assert result is None

    def test_process_email_no_sync_id(self, email_file_info):
        """Test metadata excludes sync_id when not provided."""
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_outlook_email("ws_test", "conn_test", email_file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        assert "sync_id" not in call_args["Metadata"]

    def test_process_email_escapes_html_in_metadata(self):
        """Test subject and from are HTML-escaped in the email document."""
        file_info = {
            "message_id": "msg_xss",
            "subject": '<script>alert("xss")</script>',
            "folder_name": "Inbox",
            "from": "<b>hacker@evil.com</b>",
            "received_at": "2024-01-01T00:00:00Z",
            "body_content": "Normal body",
            "body_content_type": "html",
        }
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        process_outlook_email("ws_test", "conn_test", file_info)

        call_args = mock_s3_client.put_object.call_args[1]
        body = call_args["Body"].decode("utf-8")
        # Script tags should be escaped in the title and metadata
        assert "<script>" not in body.split("<div class=\"email-body\">")[0]


# ============================================================================
# Process Outlook Attachment
# ============================================================================

class TestProcessOutlookAttachment:
    """Test Outlook attachment processing.

    process_outlook_attachment uses local imports:
        from outlook_api import download_attachment, OutlookAPIError
        from outlook_token_manager import refresh_access_token
    We mock these via sys.modules patches.
    """

    def _setup_oauth_ssm(self):
        """Set up SSM mock for Microsoft OAuth config."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "ms-client-id"}},
            {"Parameter": {"Value": "ms-client-secret"}},
        ]

    def _make_api_mock(self):
        """Create outlook_api mock with real exception classes for except clauses."""
        from outlook_api import OutlookAPIError
        mock_api = MagicMock()
        mock_api.OutlookAPIError = OutlookAPIError
        return mock_api

    def test_process_attachment_success(self, attachment_file_info):
        """Test successful attachment download and upload."""
        self._setup_oauth_ssm()

        # Mock the modules that process_outlook_attachment locally imports
        mock_api = self._make_api_mock()
        mock_token = MagicMock()
        mock_api.download_attachment.return_value = b"PDF binary content"
        mock_token.refresh_access_token.return_value = "access_token"
        mock_s3_client.put_object.return_value = {"ResponseMetadata": {"HTTPStatusCode": 200}}

        with patch.dict(sys.modules, {'outlook_api': mock_api, 'outlook_token_manager': mock_token}):
            result = process_outlook_attachment("ws_test", "conn_test", attachment_file_info, sync_id="sync_001")

        assert result is not None
        assert "workspaces/ws_test/outlook/" in result
        assert "attachments/" in result

        call_args = mock_s3_client.put_object.call_args[1]
        assert call_args["ContentType"] == "application/pdf"
        assert call_args["Metadata"]["source"] == "outlook_attachment"
        assert call_args["Metadata"]["sync_id"] == "sync_001"

    def test_process_attachment_token_refresh_fails(self, attachment_file_info):
        """Test returns None when token refresh fails."""
        self._setup_oauth_ssm()

        mock_api = MagicMock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = None

        with patch.dict(sys.modules, {'outlook_api': mock_api, 'outlook_token_manager': mock_token}):
            result = process_outlook_attachment("ws_test", "conn_test", attachment_file_info)
        assert result is None

    def test_process_attachment_download_error(self, attachment_file_info):
        """Test returns None on download API error."""
        self._setup_oauth_ssm()

        # Load real OutlookAPIError class for exception raising
        from outlook_api import OutlookAPIError

        mock_api = MagicMock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.download_attachment.side_effect = OutlookAPIError("Download failed")
        mock_api.OutlookAPIError = OutlookAPIError

        with patch.dict(sys.modules, {'outlook_api': mock_api, 'outlook_token_manager': mock_token}):
            result = process_outlook_attachment("ws_test", "conn_test", attachment_file_info)
        assert result is None

    def test_process_attachment_s3_error(self, attachment_file_info):
        """Test returns None on S3 upload error."""
        self._setup_oauth_ssm()

        mock_api = self._make_api_mock()
        mock_token = MagicMock()
        mock_token.refresh_access_token.return_value = "access_token"
        mock_api.download_attachment.return_value = b"content"
        mock_s3_client.put_object.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutObject"
        )

        with patch.dict(sys.modules, {'outlook_api': mock_api, 'outlook_token_manager': mock_token}):
            result = process_outlook_attachment("ws_test", "conn_test", attachment_file_info)
        assert result is None


# ============================================================================
# Lambda Handler Routing
# ============================================================================

class TestLambdaHandlerOutlookRouting:
    """Test lambda_handler routes Outlook sources correctly."""

    @patch.object(handler_module, 'process_outlook_email')
    def test_routes_outlook_source(self, mock_process):
        """Test routes source='outlook' to process_outlook_email."""
        mock_process.return_value = "workspaces/ws_test/outlook/Inbox/email.html"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "outlook",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "file": {"message_id": "msg_001", "subject": "Test"},
                    "sync_id": "sync_001",
                })
            }]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        assert body["results"][0]["source"] == "outlook"
        mock_process.assert_called_once()

    @patch.object(handler_module, 'process_outlook_attachment')
    def test_routes_outlook_attachment_source(self, mock_process):
        """Test routes source='outlook_attachment' to process_outlook_attachment."""
        mock_process.return_value = "workspaces/ws_test/outlook/Inbox/att/file.pdf"

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "outlook_attachment",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "file": {"attachment_id": "att_001", "message_id": "msg_001"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "success"
        mock_process.assert_called_once()

    @patch.object(handler_module, 'process_outlook_email')
    def test_outlook_processing_failure(self, mock_process):
        """Test reports error when Outlook processing returns None."""
        mock_process.return_value = None

        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "outlook",
                    "workspace_id": "ws_test",
                    "connection_id": "conn_test",
                    "file": {"message_id": "msg_001"},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    def test_missing_source_returns_error(self):
        """Test missing source field returns error."""
        event = {
            "Records": [{
                "body": json.dumps({
                    "workspace_id": "ws_test",
                    "file": {},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    def test_unknown_source_returns_skipped(self):
        """Test unknown source returns skipped."""
        event = {
            "Records": [{
                "body": json.dumps({
                    "source": "unknown_source",
                    "workspace_id": "ws_test",
                    "file": {},
                })
            }]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "skipped"

    def test_invalid_json_record(self):
        """Test handles invalid JSON in SQS record."""
        event = {"Records": [{"body": "not json"}]}

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    def test_no_records_returns_400(self):
        """Test returns 400 when no Records key."""
        result = lambda_handler({}, None)
        assert result["statusCode"] == 400
