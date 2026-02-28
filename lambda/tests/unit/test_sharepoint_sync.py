"""Unit tests for sharepoint-sync Lambda handler."""

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


def _load_handler_module():
    """Load or reload the sharepoint-sync handler module."""
    if 'sharepoint_sync_handler' in sys.modules:
        del sys.modules['sharepoint_sync_handler']

    # Clear cached sibling modules that may conflict with other integration dirs
    for mod_name in ['token_manager', 'sharepoint_api']:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Add sharepoint-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), '../../sharepoint-sync')
    abs_sync_dir = os.path.abspath(sync_dir)
    # Remove any existing entry and re-insert at front
    sys.path = [p for p in sys.path if os.path.abspath(p) != abs_sync_dir]
    sys.path.insert(0, abs_sync_dir)

    handler_path = os.path.join(sync_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("sharepoint_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['sharepoint_sync_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs_client = mock_sqs_client
    handler_module.ssm_client = mock_ssm_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
process_sync_request = handler_module.process_sync_request
get_connection = handler_module.get_connection
update_connection_status = handler_module.update_connection_status
sanitize_filename = handler_module.sanitize_filename
queue_file_ingestion = handler_module.queue_file_ingestion
queue_page_ingestion = handler_module.queue_page_ingestion


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_ssm_client.reset_mock()
    handler_module._oauth_config = None
    yield


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    """Sample SharePoint connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "SHAREPOINT_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "selected_sites": [
            {
                "site_id": "site_001",
                "site_name": "Engineering",
                "drives": [
                    {"id": "drive_001", "name": "Documents"},
                ],
            },
        ],
        "include_pages": True,
        "delta_tokens": {},
        "last_sync_status": "queued",
    }


@pytest.fixture
def sample_drive_item():
    """Sample SharePoint drive item (file) from delta API."""
    return {
        "id": "item_abc123",
        "name": "architecture.pdf",
        "size": 204800,
        "file": {
            "mimeType": "application/pdf",
        },
        "parentReference": {
            "path": "/drives/drive_001/root:/Projects",
        },
        "@microsoft.graph.downloadUrl": "https://sharepoint.com/download/architecture.pdf",
    }


@pytest.fixture
def sample_page():
    """Sample SharePoint site page."""
    return {
        "id": "page_xyz789",
        "title": "Team Onboarding Guide",
        "webUrl": "https://contoso.sharepoint.com/sites/Engineering/SitePages/Onboarding.aspx",
        "lastModifiedDateTime": "2024-03-15T14:30:00Z",
        "canvasLayout": {
            "horizontalSections": [
                {
                    "columns": [
                        {
                            "webparts": [
                                {"innerHtml": "<p>Welcome to the team onboarding guide.</p>"},
                            ],
                        },
                    ],
                },
            ],
        },
    }


# ============================================================================
# Sanitize Filename
# ============================================================================

class TestSanitizeFilename:
    """Test filename sanitization."""

    def test_sanitize_normal_filename(self):
        """Test normal filename passes through."""
        assert sanitize_filename("Documents") == "Documents"

    def test_sanitize_special_characters(self):
        """Test special characters are replaced."""
        result = sanitize_filename('Report: Q4 <2024> "Final"')
        assert "<" not in result
        assert ">" not in result
        assert '"' not in result
        assert ":" not in result

    def test_sanitize_whitespace(self):
        """Test whitespace is collapsed."""
        result = sanitize_filename("hello   world")
        assert result == "hello_world"

    def test_sanitize_truncates_long_names(self):
        """Test names are truncated to 200 chars."""
        long_name = "a" * 300
        result = sanitize_filename(long_name)
        assert len(result) <= 200

    def test_sanitize_strips_leading_trailing(self):
        """Test strips leading/trailing dots and underscores."""
        result = sanitize_filename("._test._")
        assert not result.startswith(".")
        assert not result.startswith("_")


# ============================================================================
# Get Connection
# ============================================================================

class TestGetConnection:
    """Test fetching connection from DynamoDB."""

    def test_get_connection_success(self, mock_table):
        """Test successful connection retrieval."""
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "WS#ws_test",
                "SK": "SHAREPOINT_CONNECTION#conn_test",
                "connection_id": "conn_test",
            }
        }

        result = get_connection("ws_test", "conn_test")

        assert result is not None
        assert result["connection_id"] == "conn_test"
        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "WS#ws_test",
                "SK": "SHAREPOINT_CONNECTION#conn_test",
            }
        )

    def test_get_connection_not_found(self, mock_table):
        """Test returns None when connection not found."""
        mock_table.get_item.return_value = {}

        result = get_connection("ws_test", "bad_id")

        assert result is None

    def test_get_connection_dynamodb_error(self, mock_table):
        """Test returns None on DynamoDB error."""
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "GetItem"
        )

        result = get_connection("ws_test", "conn_test")

        assert result is None


# ============================================================================
# Update Connection Status
# ============================================================================

class TestUpdateConnectionStatus:
    """Test updating connection sync status."""

    def test_update_status_completed(self, mock_table):
        """Test updating to completed status."""
        update_connection_status("ws_test", "conn_test", "completed", file_count=10)

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "completed"
        assert call_args["ExpressionAttributeValues"][":file_count"] == 10

    def test_update_status_with_error(self, mock_table):
        """Test updating status with error message."""
        update_connection_status("ws_test", "conn_test", "error", error="Token expired")

        call_args = mock_table.update_item.call_args[1]
        assert ":error" in call_args["ExpressionAttributeValues"]
        assert call_args["ExpressionAttributeValues"][":error"] == "Token expired"

    def test_update_status_with_delta_tokens(self, mock_table):
        """Test updating status with delta tokens."""
        delta_tokens = {"drive_001": "https://delta-link"}
        update_connection_status("ws_test", "conn_test", "completed", delta_tokens=delta_tokens)

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":delta"] == delta_tokens

    def test_update_status_with_page_count(self, mock_table):
        """Test updating status with page count."""
        update_connection_status("ws_test", "conn_test", "completed", page_count=5)

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":page_count"] == 5

    def test_update_status_uses_sharepoint_connection_sk(self, mock_table):
        """Test update uses SHAREPOINT_CONNECTION# SK prefix."""
        update_connection_status("ws_test", "conn_test", "syncing")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"]["SK"] == "SHAREPOINT_CONNECTION#conn_test"

    def test_update_status_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_connection_status("ws_test", "conn_test", "error")


# ============================================================================
# Queue File Ingestion
# ============================================================================

class TestQueueFileIngestion:
    """Test queuing files to file ingestion."""

    def test_queue_file_sends_sqs_message(self, sample_drive_item):
        """Test file message is sent to SQS."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            item=sample_drive_item,
            site_name="Engineering",
            drive_name="Documents",
            download_url="https://sharepoint.com/download/architecture.pdf",
            sync_id="sync_001",
            file_index=0,
            total_files=10,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "sharepoint"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["file"]["filename"] == "architecture.pdf"
        assert msg["file"]["site_name"] == "Engineering"
        assert msg["file"]["drive_name"] == "Documents"
        assert msg["file"]["download_url"] == "https://sharepoint.com/download/architecture.pdf"
        assert msg["file"]["mime_type"] == "application/pdf"

    def test_queue_file_includes_message_group_id(self, sample_drive_item):
        """Test SQS message includes correct MessageGroupId."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            item=sample_drive_item,
            site_name="Engineering",
            drive_name="Documents",
            download_url="https://sharepoint.com/download/file.pdf",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_file_handles_sqs_error(self, sample_drive_item):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            item=sample_drive_item,
            site_name="Engineering",
            drive_name="Documents",
            download_url="https://sharepoint.com/download/file.pdf",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

    def test_queue_file_default_mime_type(self):
        """Test default mime type when file info is missing."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        item = {"id": "item_1", "name": "data.bin", "file": {}}

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            item=item,
            site_name="Engineering",
            drive_name="Documents",
            download_url="https://sharepoint.com/download/data.bin",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["mime_type"] == "application/octet-stream"


# ============================================================================
# Queue Page Ingestion
# ============================================================================

class TestQueuePageIngestion:
    """Test queuing pages to file ingestion."""

    def test_queue_page_sends_sqs_message(self, sample_page):
        """Test page message is sent to SQS."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_page_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            page=sample_page,
            site_name="Engineering",
            page_text="# Team Onboarding Guide\n\nWelcome to the team.",
            sync_id="sync_001",
            file_index=0,
            total_files=5,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "sharepoint_page"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["file"]["page_id"] == "page_xyz789"
        assert msg["file"]["title"] == "Team Onboarding Guide"
        assert msg["file"]["site_name"] == "Engineering"
        assert msg["file"]["content"] == "# Team Onboarding Guide\n\nWelcome to the team."
        assert msg["file"]["web_url"] == "https://contoso.sharepoint.com/sites/Engineering/SitePages/Onboarding.aspx"

    def test_queue_page_includes_message_group_id(self, sample_page):
        """Test SQS message includes correct MessageGroupId."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_page_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            page=sample_page,
            site_name="Engineering",
            page_text="Some content here.",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_page_handles_sqs_error(self, sample_page):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_page_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            page=sample_page,
            site_name="Engineering",
            page_text="Page content.",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

    def test_queue_page_untitled_default(self):
        """Test default title for page without title."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        page = {"id": "page_notitle"}

        queue_page_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            page=page,
            site_name="Engineering",
            page_text="Some text content.",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["title"] == "Untitled"


# ============================================================================
# Process Sync Request
# ============================================================================

class TestProcessSyncRequest:
    """Test the main sync processing function."""

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_success_files(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta, mock_queue_file,
    ):
        """Test successful sync request with files."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.return_value = (
            [
                {
                    "id": "item_1",
                    "name": "doc.pdf",
                    "file": {"mimeType": "application/pdf"},
                    "@microsoft.graph.downloadUrl": "https://download/doc.pdf",
                },
            ],
            "new_delta_token",
        )

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["files_queued"] == 1
        assert result["pages_queued"] == 0
        mock_queue_file.assert_called_once()

    @patch.object(handler_module, 'queue_page_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_success_with_pages(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta, mock_queue_page,
    ):
        """Test successful sync request with pages enabled."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [],
                },
            ],
            "include_pages": True,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "page_1", "title": "Page One", "webUrl": "https://page1"},
        ]
        mock_extract.return_value = "# Page One\n\nThis is the page content with enough text."

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 1
        mock_queue_page.assert_called_once()

    @patch.object(handler_module, 'get_connection')
    def test_sync_request_connection_not_found(self, mock_get_conn):
        """Test sync fails when connection not found."""
        mock_get_conn.return_value = None

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "bad_id",
        })

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_no_sites(self, mock_get_conn, mock_update):
        """Test sync skips when no sites selected."""
        mock_get_conn.return_value = {
            "selected_sites": [],
            "delta_tokens": {},
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"
        mock_update.assert_called_with("ws_test", "conn_test", "no_sites_selected")

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_auth_failure(self, mock_get_conn, mock_update, mock_config, mock_refresh):
        """Test sync handles auth failure."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": True,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = None

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Authentication" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "auth_error",
            error="Failed to refresh OAuth token"
        )

    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_skips_deleted_items(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta,
    ):
        """Test sync skips deleted and removed items."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.return_value = (
            [
                {"id": "item_del", "deleted": True, "file": {"mimeType": "text/plain"}},
                {"id": "item_removed", "@removed": {"reason": "deleted"}, "file": {"mimeType": "text/plain"}},
                {"id": "item_folder", "name": "FolderOnly"},  # No "file" key = folder
            ],
            None,
        )
        mock_list_pages.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["files_queued"] == 0

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'get_item_download_url')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_fetches_download_url_when_missing(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_get_download, mock_get_delta, mock_queue_file,
    ):
        """Test fetches download URL via API when not in delta response."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        # Item without @microsoft.graph.downloadUrl
        mock_get_delta.return_value = (
            [
                {"id": "item_no_url", "name": "report.docx", "file": {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"}},
            ],
            None,
        )
        mock_get_download.return_value = "https://sharepoint.com/download/report.docx"
        mock_list_pages.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["files_queued"] == 1
        mock_get_download.assert_called_once_with("access_token", "drive_001", "item_no_url")

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'get_item_download_url')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_skips_file_when_no_download_url(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_get_download, mock_get_delta, mock_queue_file,
    ):
        """Test skips file when download URL cannot be obtained."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.return_value = (
            [
                {"id": "item_no_url", "name": "secret.bin", "file": {"mimeType": "application/octet-stream"}},
            ],
            None,
        )
        mock_get_download.return_value = None
        mock_list_pages.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["files_queued"] == 0
        mock_queue_file.assert_not_called()

    @patch.object(handler_module, 'queue_page_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_skips_short_page_content(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta, mock_queue_page,
    ):
        """Test skips pages with very short content."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [],
                },
            ],
            "include_pages": True,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "page_short", "title": "Empty Page"},
        ]
        # Content under 10 chars after strip
        mock_extract.return_value = "Hi"

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 0
        mock_queue_page.assert_not_called()

    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_updates_delta_tokens(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_get_delta,
    ):
        """Test delta tokens are persisted on completion."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.return_value = ([], "https://graph.microsoft.com/delta?token=abc123")
        mock_list_pages.return_value = []

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Verify update_connection_status was called with delta_tokens
        completed_call = [
            c for c in mock_update.call_args_list
            if c[0][2] == "completed"
        ]
        assert len(completed_call) == 1
        assert completed_call[0][1]["delta_tokens"] == {
            "drive_001": "https://graph.microsoft.com/delta?token=abc123"
        }

    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_auth_error_during_sync(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_get_delta,
    ):
        """Test SharePointAuthError during sync updates status."""
        from sharepoint_api import SharePointAuthError

        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.side_effect = SharePointAuthError("Token expired")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Authentication" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "auth_error",
            error="Authentication failed during sync"
        )

    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_generic_exception(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_get_delta,
    ):
        """Test generic exception during sync updates status."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [{"id": "drive_001", "name": "Documents"}],
                },
            ],
            "include_pages": False,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_get_delta.side_effect = RuntimeError("Unexpected network failure")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Unexpected network failure" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "error",
            error="Unexpected network failure"
        )

    @patch.object(handler_module, 'queue_page_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_page_api_error_continues(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta, mock_queue_page,
    ):
        """Test SharePointAPIError during page sync does not fail entire sync."""
        from sharepoint_api import SharePointAPIError

        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [],
                },
            ],
            "include_pages": True,
            "delta_tokens": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.side_effect = SharePointAPIError("Pages not accessible")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Should still complete successfully, just with 0 pages
        assert result["status"] == "success"
        assert result["pages_queued"] == 0
        mock_queue_page.assert_not_called()

    @patch.object(handler_module, 'queue_page_ingestion')
    @patch.object(handler_module, 'get_drive_delta')
    @patch.object(handler_module, 'list_site_pages')
    @patch.object(handler_module, 'extract_page_text')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_incremental_page_filter(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_extract, mock_list_pages, mock_get_delta, mock_queue_page,
    ):
        """Test incremental page sync filters by last_sync_at."""
        mock_get_conn.return_value = {
            "selected_sites": [
                {
                    "site_id": "site_001",
                    "site_name": "Engineering",
                    "drives": [],
                },
            ],
            "include_pages": True,
            "delta_tokens": {},
            "last_sync_at": "2024-03-01T00:00:00Z",
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            # Modified after last_sync_at - should be included
            {"id": "page_new", "title": "New Page", "lastModifiedDateTime": "2024-03-15T10:00:00Z"},
            # Modified before last_sync_at - should be filtered out
            {"id": "page_old", "title": "Old Page", "lastModifiedDateTime": "2024-02-01T10:00:00Z"},
        ]
        mock_extract.return_value = "# Page\n\nContent that is long enough to pass the threshold."

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 1
        mock_queue_page.assert_called_once()


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'process_sync_request')
    def test_processes_sqs_records(self, mock_process):
        """Test handler processes SQS records."""
        mock_process.return_value = {"status": "success", "files_queued": 5, "pages_queued": 2}

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 1

    @patch.object(handler_module, 'process_sync_request')
    def test_handles_multiple_records(self, mock_process):
        """Test handler processes multiple SQS records."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert len(body["results"]) == 2

    def test_handles_invalid_json(self):
        """Test handler handles invalid JSON gracefully."""
        event = {"Records": [{"body": "not json"}]}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    def test_handles_missing_records(self):
        """Test handler handles missing Records key."""
        result = lambda_handler({}, None)
        assert result["statusCode"] == 400

    @patch.object(handler_module, 'process_sync_request')
    def test_handles_processing_exception(self, mock_process):
        """Test handler catches processing exceptions."""
        mock_process.side_effect = Exception("Unexpected error")

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"

    @patch.object(handler_module, 'process_sync_request')
    def test_result_body_contains_message(self, mock_process):
        """Test response body contains SharePoint sync message."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert "SharePoint sync completed" in body["message"]
