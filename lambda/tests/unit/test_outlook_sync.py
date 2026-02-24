"""Unit tests for outlook-sync Lambda handler."""

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
    """Load or reload the outlook-sync handler module."""
    if 'outlook_sync_handler' in sys.modules:
        del sys.modules['outlook_sync_handler']

    # Add outlook-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), '../../outlook-sync')
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)

    handler_path = os.path.join(sync_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("outlook_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['outlook_sync_handler'] = handler_module
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
queue_email_ingestion = handler_module.queue_email_ingestion
queue_attachment_ingestion = handler_module.queue_attachment_ingestion
queue_continuation = handler_module.queue_continuation
_process_folder_page = handler_module._process_folder_page


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
    """Sample Outlook connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "OUTLOOK_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "selected_folders": [
            {"id": "folder_inbox", "displayName": "Inbox"},
        ],
        "delta_links": {},
        "last_sync_status": "queued",
    }


@pytest.fixture
def sample_message():
    """Sample MS Graph message object."""
    return {
        "id": "msg_abc123",
        "subject": "Test Email Subject",
        "from": {
            "emailAddress": {"address": "sender@example.com", "name": "Sender"}
        },
        "receivedDateTime": "2024-02-24T10:00:00Z",
        "body": {"content": "<p>Hello</p>", "contentType": "html"},
        "hasAttachments": False,
    }


# ============================================================================
# Sanitize Filename
# ============================================================================

class TestSanitizeFilename:
    """Test filename sanitization."""

    def test_sanitize_normal_filename(self):
        """Test normal filename passes through."""
        assert sanitize_filename("Inbox") == "Inbox"

    def test_sanitize_special_characters(self):
        """Test special characters are replaced."""
        result = sanitize_filename('Re: Test <script>"hack"')
        assert "<" not in result
        assert ">" not in result
        assert '"' not in result

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
# Queue Functions
# ============================================================================

class TestQueueEmailIngestion:
    """Test queuing emails to file ingestion."""

    def test_queue_email_sends_sqs_message(self, sample_message):
        """Test email message is sent to SQS."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_email_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            message=sample_message,
            folder_name="Inbox",
            sync_id="sync_001",
            file_index=0,
            total_files=10,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "outlook"
        assert msg["workspace_id"] == "ws_test"
        assert msg["file"]["subject"] == "Test Email Subject"
        assert msg["file"]["folder_name"] == "Inbox"

    def test_queue_email_handles_sqs_error(self, sample_message):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_email_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            message=sample_message,
            folder_name="Inbox",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )


class TestQueueAttachmentIngestion:
    """Test queuing attachments to file ingestion."""

    def test_queue_attachment_sends_sqs_message(self):
        """Test attachment message is sent to SQS."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        attachment = {
            "id": "att_001",
            "name": "document.pdf",
            "contentType": "application/pdf",
            "size": 1024,
        }

        queue_attachment_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            attachment=attachment,
            message_id="msg_abc",
            folder_name="Inbox",
            sync_id="sync_001",
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "outlook_attachment"
        assert msg["file"]["filename"] == "document.pdf"
        assert msg["file"]["attachment_id"] == "att_001"


class TestQueueContinuation:
    """Test SQS continuation for large mailboxes."""

    def test_queue_continuation_sends_message(self):
        """Test continuation message is queued."""
        handler_module.OUTLOOK_SYNC_QUEUE = "https://sqs.example.com/sync"

        queue_continuation(
            workspace_id="ws_test",
            connection_id="conn_test",
            folder_id="folder_inbox",
            folder_name="Inbox",
            next_link="https://graph.microsoft.com/next",
            sync_id="sync_001",
            messages_so_far=500,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["continuation"] is True
        assert msg["next_link"] == "https://graph.microsoft.com/next"
        assert msg["messages_so_far"] == 500

    def test_queue_continuation_no_queue_configured(self):
        """Test no-op when queue not configured."""
        handler_module.OUTLOOK_SYNC_QUEUE = None
        # Should not raise
        queue_continuation("ws_test", "conn_test", "f1", "Inbox", "link", "sync", 100)


# ============================================================================
# Update Connection Status
# ============================================================================

class TestUpdateConnectionStatus:
    """Test updating connection sync status."""

    def test_update_status_completed(self, mock_table):
        """Test updating to completed status."""
        update_connection_status("ws_test", "conn_test", "completed", email_count=42)

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert ":status" in call_args["ExpressionAttributeValues"]
        assert call_args["ExpressionAttributeValues"][":status"] == "completed"
        assert call_args["ExpressionAttributeValues"][":count"] == 42

    def test_update_status_with_error(self, mock_table):
        """Test updating status with error message."""
        update_connection_status("ws_test", "conn_test", "error", error="Token expired")

        call_args = mock_table.update_item.call_args[1]
        assert ":error" in call_args["ExpressionAttributeValues"]
        assert call_args["ExpressionAttributeValues"][":error"] == "Token expired"

    def test_update_status_with_delta_links(self, mock_table):
        """Test updating status with delta links."""
        delta_links = {"folder1": "https://delta-link"}
        update_connection_status("ws_test", "conn_test", "completed", delta_links=delta_links)

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":delta"] == delta_links

    def test_update_status_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_connection_status("ws_test", "conn_test", "error")


# ============================================================================
# Process Sync Request
# ============================================================================

class TestProcessSyncRequest:
    """Test the main sync processing function."""

    @patch.object(handler_module, '_process_folder_page')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_success(self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_process):
        """Test successful sync request."""
        mock_get_conn.return_value = {
            "selected_folders": [{"id": "f1", "displayName": "Inbox"}],
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_process.return_value = (10, 2)

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["emails_queued"] == 10
        assert result["attachments_queued"] == 2

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
    def test_sync_request_no_folders(self, mock_get_conn, mock_update):
        """Test sync skips when no folders selected."""
        mock_get_conn.return_value = {
            "selected_folders": [],
            "delta_links": {},
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_auth_failure(self, mock_get_conn, mock_update, mock_config, mock_refresh):
        """Test sync handles auth failure."""
        mock_get_conn.return_value = {
            "selected_folders": [{"id": "f1", "displayName": "Inbox"}],
            "delta_links": {},
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

    @patch.object(handler_module, '_process_folder_page')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_continuation(self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_process):
        """Test continuation sync request."""
        mock_get_conn.return_value = {
            "selected_folders": [],
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_process.return_value = (25, 0)

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
            "continuation": True,
            "folder_id": "f1",
            "folder_name": "Inbox",
            "next_link": "https://graph.microsoft.com/next",
            "sync_id": "sync_001",
            "messages_so_far": 100,
        })

        assert result["status"] == "success"
        assert result["emails_queued"] == 125  # 100 + 25


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'process_sync_request')
    def test_processes_sqs_records(self, mock_process):
        """Test handler processes SQS records."""
        mock_process.return_value = {"status": "success", "emails_queued": 5}

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
