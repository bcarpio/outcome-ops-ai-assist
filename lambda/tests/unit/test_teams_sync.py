"""Unit tests for teams-sync Lambda handler."""

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
    """Load or reload the teams-sync handler module."""
    if 'teams_sync_handler' in sys.modules:
        del sys.modules['teams_sync_handler']

    # Clear cached sibling modules that may conflict with other integration dirs
    for mod_name in ['token_manager', 'teams_api']:
        if mod_name in sys.modules:
            del sys.modules[mod_name]

    # Add teams-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), '../../teams-sync')
    abs_sync_dir = os.path.abspath(sync_dir)
    # Remove any existing entry and re-insert at front
    sys.path = [p for p in sys.path if os.path.abspath(p) != abs_sync_dir]
    sys.path.insert(0, abs_sync_dir)

    handler_path = os.path.join(sync_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("teams_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['teams_sync_handler'] = handler_module
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
build_message_html = handler_module.build_message_html
queue_message_ingestion = handler_module.queue_message_ingestion
queue_file_ingestion = handler_module.queue_file_ingestion
_sync_channel_delta = handler_module._sync_channel_delta
_sync_channel_full = handler_module._sync_channel_full
_sync_channel_files = handler_module._sync_channel_files


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
    """Sample Teams connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "TEAMS_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "config": {
            "include_channels": [
                {
                    "team_id": "team_001",
                    "channel_id": "chan_001",
                    "channel_name": "General",
                },
            ],
            "days_back": 30,
            "sync_files": True,
        },
        "delta_links": {},
        "last_sync_status": "queued",
    }


@pytest.fixture
def sample_message():
    """Sample MS Graph Teams message object."""
    return {
        "id": "msg_abc123",
        "subject": "Test Discussion",
        "from": {
            "user": {"displayName": "Alice Smith", "id": "user_001"},
        },
        "createdDateTime": "2024-02-24T10:00:00Z",
        "body": {"content": "<p>Hello team!</p>", "contentType": "html"},
    }


@pytest.fixture
def sample_file():
    """Sample MS Graph file item from a channel."""
    return {
        "id": "file_001",
        "name": "architecture.pdf",
        "@microsoft.graph.downloadUrl": "https://sharepoint.example.com/download/arch.pdf",
        "file": {"mimeType": "application/pdf"},
        "size": 204800,
    }


# ============================================================================
# Sanitize Filename
# ============================================================================

class TestSanitizeFilename:
    """Test filename sanitization."""

    def test_sanitize_normal_filename(self):
        """Test normal filename passes through."""
        assert sanitize_filename("General") == "General"

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
# Build Message HTML
# ============================================================================

class TestBuildMessageHtml:
    """Test HTML formatting of Teams messages and replies."""

    def test_basic_html_message(self, sample_message):
        """Test basic message renders to HTML."""
        result = build_message_html(sample_message)
        assert "<!DOCTYPE html>" in result
        assert "Alice Smith" in result
        assert "<p>Hello team!</p>" in result
        assert "2024-02-24T10:00:00Z" in result

    def test_message_with_subject(self, sample_message):
        """Test message subject appears as h1."""
        result = build_message_html(sample_message)
        assert "<h1>Test Discussion</h1>" in result

    def test_message_without_subject(self, sample_message):
        """Test message without subject uses default title."""
        sample_message["subject"] = ""
        result = build_message_html(sample_message)
        assert "<title>Teams Message</title>" in result
        assert "<h1>" not in result

    def test_text_content_type_escaped(self):
        """Test plain text body is wrapped in pre and escaped."""
        msg = {
            "id": "msg_text",
            "from": {"user": {"displayName": "Bob"}},
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "<script>alert('xss')</script>", "contentType": "text"},
        }
        result = build_message_html(msg)
        assert "<pre>" in result
        assert "&lt;script&gt;" in result
        assert "<script>alert" not in result

    def test_message_with_replies(self, sample_message):
        """Test message with replies includes replies section."""
        replies = [
            {
                "id": "reply_001",
                "from": {"user": {"displayName": "Bob Jones"}},
                "createdDateTime": "2024-02-24T11:00:00Z",
                "body": {"content": "<p>Thanks!</p>", "contentType": "html"},
            },
            {
                "id": "reply_002",
                "from": {"user": {"displayName": "Carol White"}},
                "createdDateTime": "2024-02-24T12:00:00Z",
                "body": {"content": "Plain text reply", "contentType": "text"},
            },
        ]
        result = build_message_html(sample_message, replies)
        assert "<h2>Replies</h2>" in result
        assert "Bob Jones" in result
        assert "<p>Thanks!</p>" in result
        assert "Carol White" in result
        assert "<pre>" in result  # text reply wrapped in pre

    def test_message_with_empty_replies(self, sample_message):
        """Test message with empty replies list has no replies section."""
        result = build_message_html(sample_message, [])
        assert "Replies" not in result

    def test_message_with_no_from(self):
        """Test message with missing from field shows Unknown."""
        msg = {
            "id": "msg_no_from",
            "from": None,
            "createdDateTime": "2024-01-01T00:00:00Z",
            "body": {"content": "Test", "contentType": "html"},
        }
        result = build_message_html(msg)
        assert "Unknown" in result

    def test_reply_with_no_from(self, sample_message):
        """Test reply with missing from field shows Unknown."""
        replies = [
            {
                "id": "reply_no_from",
                "from": None,
                "createdDateTime": "2024-02-24T11:00:00Z",
                "body": {"content": "Reply", "contentType": "html"},
            },
        ]
        result = build_message_html(sample_message, replies)
        assert result.count("Unknown") >= 1


# ============================================================================
# Get Connection
# ============================================================================

class TestGetConnection:
    """Test reading connection from DynamoDB."""

    def test_get_connection_found(self, mock_table):
        """Test retrieving an existing connection."""
        mock_table.get_item.return_value = {
            "Item": {"PK": "WS#ws_test", "SK": "TEAMS_CONNECTION#conn_test"}
        }

        result = get_connection("ws_test", "conn_test")

        assert result is not None
        assert result["PK"] == "WS#ws_test"
        assert result["SK"] == "TEAMS_CONNECTION#conn_test"
        mock_table.get_item.assert_called_once_with(
            Key={"PK": "WS#ws_test", "SK": "TEAMS_CONNECTION#conn_test"}
        )

    def test_get_connection_not_found(self, mock_table):
        """Test returns None when connection not found."""
        mock_table.get_item.return_value = {}

        result = get_connection("ws_test", "bad_conn")
        assert result is None

    def test_get_connection_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
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
        update_connection_status("ws_test", "conn_test", "completed", message_count=42, file_count=5)

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "completed"
        assert call_args["ExpressionAttributeValues"][":msg_count"] == 42
        assert call_args["ExpressionAttributeValues"][":file_count"] == 5

    def test_update_status_with_error(self, mock_table):
        """Test updating status with error message."""
        update_connection_status("ws_test", "conn_test", "error", error="Token expired")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":error"] == "Token expired"

    def test_update_status_with_delta_links(self, mock_table):
        """Test updating status with delta links."""
        delta_links = {"team1_chan1": "https://delta-link"}
        update_connection_status("ws_test", "conn_test", "completed", delta_links=delta_links)

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":delta"] == delta_links

    def test_update_status_syncing(self, mock_table):
        """Test updating to syncing status does not set sync_at."""
        update_connection_status("ws_test", "conn_test", "syncing")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "syncing"
        assert ":sync_at" not in call_args["ExpressionAttributeValues"]

    def test_update_status_auth_error_sets_sync_at(self, mock_table):
        """Test auth_error status sets last_sync_at."""
        update_connection_status("ws_test", "conn_test", "auth_error", error="Auth failed")

        call_args = mock_table.update_item.call_args[1]
        assert ":sync_at" in call_args["ExpressionAttributeValues"]

    def test_update_status_removes_error_on_success(self, mock_table):
        """Test completed status without error removes last_sync_error."""
        update_connection_status("ws_test", "conn_test", "completed")

        call_args = mock_table.update_item.call_args[1]
        assert "REMOVE last_sync_error" in call_args["UpdateExpression"]

    def test_update_status_uses_teams_connection_sk(self, mock_table):
        """Test update uses TEAMS_CONNECTION# SK prefix."""
        update_connection_status("ws_test", "conn_test", "completed")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"]["SK"] == "TEAMS_CONNECTION#conn_test"

    def test_update_status_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_connection_status("ws_test", "conn_test", "error")


# ============================================================================
# Queue Message Ingestion
# ============================================================================

class TestQueueMessageIngestion:
    """Test queuing Teams messages to file ingestion."""

    def test_queue_message_sends_sqs_message(self, sample_message):
        """Test message is sent to SQS with correct format."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_message_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            message=sample_message,
            channel_name="General",
            message_html="<html>content</html>",
            sync_id="sync_001",
            file_index=0,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "teams"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["file"]["message_id"] == "msg_abc123"
        assert msg["file"]["channel_name"] == "General"
        assert msg["file"]["body_content"] == "<html>content</html>"
        assert msg["sync_id"] == "sync_001"

    def test_queue_message_uses_fifo_attributes(self, sample_message):
        """Test SQS message includes MessageGroupId and DeduplicationId."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_message_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            message=sample_message,
            channel_name="General",
            message_html="<html>test</html>",
            sync_id="sync_001",
            file_index=0,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageGroupId"] == "ws_test-conn_test"
        assert call_args["MessageDeduplicationId"].startswith("sync_001-msg-")

    def test_queue_message_handles_sqs_error(self, sample_message):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_message_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            message=sample_message,
            channel_name="General",
            message_html="<html>test</html>",
            sync_id="sync_001",
            file_index=0,
        )


# ============================================================================
# Queue File Ingestion
# ============================================================================

class TestQueueFileIngestion:
    """Test queuing Teams channel files to file ingestion."""

    def test_queue_file_sends_sqs_message(self, sample_file):
        """Test file message is sent to SQS with correct format."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            file_item=sample_file,
            channel_name="General",
            sync_id="sync_001",
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "teams_file"
        assert msg["workspace_id"] == "ws_test"
        assert msg["file"]["item_id"] == "file_001"
        assert msg["file"]["filename"] == "architecture.pdf"
        assert msg["file"]["download_url"] == "https://sharepoint.example.com/download/arch.pdf"
        assert msg["file"]["mime_type"] == "application/pdf"
        assert msg["file"]["size"] == 204800

    def test_queue_file_skips_without_download_url(self):
        """Test file without download URL is skipped."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        file_item = {
            "id": "file_no_url",
            "name": "missing.pdf",
            "size": 100,
        }

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            file_item=file_item,
            channel_name="General",
            sync_id="sync_001",
        )

        mock_sqs_client.send_message.assert_not_called()

    def test_queue_file_uses_fifo_attributes(self, sample_file):
        """Test SQS message includes MessageGroupId and DeduplicationId."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            file_item=sample_file,
            channel_name="General",
            sync_id="sync_001",
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageGroupId"] == "ws_test-conn_test"
        assert call_args["MessageDeduplicationId"].startswith("sync_001-file-")

    def test_queue_file_handles_sqs_error(self, sample_file):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            file_item=sample_file,
            channel_name="General",
            sync_id="sync_001",
        )

    def test_queue_file_default_mime_type(self):
        """Test default mime type when file info is missing."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        file_item = {
            "id": "file_002",
            "name": "data.bin",
            "@microsoft.graph.downloadUrl": "https://example.com/download",
            "size": 500,
        }

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            file_item=file_item,
            channel_name="General",
            sync_id="sync_001",
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["mime_type"] == "application/octet-stream"


# ============================================================================
# Sync Channel Delta
# ============================================================================

class TestSyncChannelDelta:
    """Test delta sync of channel messages."""

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.get_messages_delta')
    def test_delta_sync_queues_messages(self, mock_delta, mock_replies, mock_build, mock_queue):
        """Test delta sync fetches and queues messages."""
        mock_delta.return_value = {
            "messages": [
                {"id": "msg_1", "body": {"content": "Hello"}},
                {"id": "msg_2", "body": {"content": "World"}},
            ],
            "next_link": None,
            "delta_link": "https://delta.new",
        }
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, new_delta = _sync_channel_delta(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            delta_link="https://delta.old",
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 2
        assert new_delta == "https://delta.new"
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.get_messages_delta')
    def test_delta_sync_skips_empty_body(self, mock_delta, mock_replies, mock_build, mock_queue):
        """Test delta sync skips messages with no body content."""
        mock_delta.return_value = {
            "messages": [
                {"id": "msg_1", "body": {"content": ""}},
                {"id": "msg_2", "body": {"content": "Valid"}},
            ],
            "next_link": None,
            "delta_link": "https://delta.new",
        }
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_delta(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            delta_link="https://delta.old",
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 1
        assert mock_queue.call_count == 1

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.get_messages_delta')
    def test_delta_sync_handles_reply_error(self, mock_delta, mock_replies, mock_build, mock_queue):
        """Test delta sync continues when reply fetch fails."""
        from teams_api import TeamsAPIError

        mock_delta.return_value = {
            "messages": [{"id": "msg_1", "body": {"content": "Hello"}}],
            "next_link": None,
            "delta_link": "https://delta.new",
        }
        mock_replies.side_effect = TeamsAPIError("Reply fetch failed")
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_delta(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            delta_link="https://delta.old",
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 1
        # build_message_html called with empty replies
        mock_build.assert_called_once_with(
            {"id": "msg_1", "body": {"content": "Hello"}}, []
        )

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.get_messages_delta')
    def test_delta_sync_follows_next_link(self, mock_delta, mock_replies, mock_build, mock_queue):
        """Test delta sync follows pagination via next_link."""
        mock_delta.side_effect = [
            {
                "messages": [{"id": "msg_1", "body": {"content": "Page 1"}}],
                "next_link": "https://graph/next",
                "delta_link": None,
            },
            {
                "messages": [{"id": "msg_2", "body": {"content": "Page 2"}}],
                "next_link": None,
                "delta_link": "https://delta.final",
            },
        ]
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, new_delta = _sync_channel_delta(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            delta_link="https://delta.old",
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 2
        assert new_delta == "https://delta.final"
        assert mock_delta.call_count == 2

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.get_messages_delta')
    def test_delta_sync_stops_at_max_messages(self, mock_delta, mock_replies, mock_build, mock_queue):
        """Test delta sync breaks when max messages reached."""
        mock_delta.return_value = {
            "messages": [{"id": f"msg_{i}", "body": {"content": f"Msg {i}"}} for i in range(10)],
            "next_link": "https://graph/next",
            "delta_link": None,
        }
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_delta(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            delta_link="https://delta.old",
            sync_id="sync_001",
            messages_so_far=495,
        )

        # Queued 10 messages (495 + 10 = 505 >= 500), then broke out of loop
        assert count == 10
        # Should NOT follow the next_link since we exceeded MAX_MESSAGES
        assert mock_delta.call_count == 1


# ============================================================================
# Sync Channel Full
# ============================================================================

class TestSyncChannelFull:
    """Test full sync of channel messages with date filter."""

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.filter_messages_by_date')
    @patch('teams_sync_handler.get_channel_messages')
    @patch('teams_sync_handler.get_messages_delta')
    def test_full_sync_establishes_delta_link(
        self, mock_delta, mock_get_msgs, mock_filter, mock_replies, mock_build, mock_queue
    ):
        """Test full sync establishes delta link for future incremental syncs."""
        mock_delta.return_value = {"delta_link": "https://delta.initial"}
        mock_get_msgs.return_value = ([], None)
        mock_filter.return_value = []

        _, delta_link = _sync_channel_full(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            days_back=30,
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert delta_link == "https://delta.initial"
        mock_delta.assert_called_once()

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.filter_messages_by_date')
    @patch('teams_sync_handler.get_channel_messages')
    @patch('teams_sync_handler.get_messages_delta')
    def test_full_sync_fetches_and_filters_messages(
        self, mock_delta, mock_get_msgs, mock_filter, mock_replies, mock_build, mock_queue
    ):
        """Test full sync fetches messages and filters by date."""
        mock_delta.return_value = {"delta_link": "https://delta.initial"}
        messages = [
            {"id": "msg_1", "body": {"content": "Recent"}, "createdDateTime": "2024-02-20T10:00:00Z"},
            {"id": "msg_2", "body": {"content": "Old"}, "createdDateTime": "2023-01-01T10:00:00Z"},
        ]
        mock_get_msgs.return_value = (messages, None)
        mock_filter.return_value = [messages[0]]
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_full(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            days_back=30,
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 1
        mock_filter.assert_called_once_with(messages, 30)

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.filter_messages_by_date')
    @patch('teams_sync_handler.get_channel_messages')
    @patch('teams_sync_handler.get_messages_delta')
    def test_full_sync_stops_when_all_messages_older(
        self, mock_delta, mock_get_msgs, mock_filter, mock_replies, mock_build, mock_queue
    ):
        """Test full sync stops when filter returns fewer than fetched (all older)."""
        mock_delta.return_value = {"delta_link": "https://delta.initial"}
        all_messages = [
            {"id": "msg_1", "body": {"content": "Recent"}, "createdDateTime": "2024-02-20T10:00:00Z"},
            {"id": "msg_2", "body": {"content": "Old"}, "createdDateTime": "2023-01-01T10:00:00Z"},
        ]
        # First call returns 2 messages but filter keeps only 1 -> stops
        mock_get_msgs.return_value = (all_messages, "next_token")
        mock_filter.return_value = [all_messages[0]]
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_full(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            days_back=30,
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 1
        # Should NOT continue pagination since filtered < fetched
        assert mock_get_msgs.call_count == 1

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.filter_messages_by_date')
    @patch('teams_sync_handler.get_channel_messages')
    @patch('teams_sync_handler.get_messages_delta')
    def test_full_sync_skips_empty_body(
        self, mock_delta, mock_get_msgs, mock_filter, mock_replies, mock_build, mock_queue
    ):
        """Test full sync skips messages with no body content."""
        mock_delta.return_value = {"delta_link": "https://delta.initial"}
        messages = [
            {"id": "msg_1", "body": {"content": ""}},
            {"id": "msg_2", "body": {"content": "Valid"}},
        ]
        mock_get_msgs.return_value = (messages, None)
        mock_filter.return_value = messages
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_full(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            days_back=30,
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 1
        assert mock_queue.call_count == 1

    @patch.object(handler_module, 'queue_message_ingestion')
    @patch.object(handler_module, 'build_message_html')
    @patch('teams_sync_handler.get_message_replies')
    @patch('teams_sync_handler.filter_messages_by_date')
    @patch('teams_sync_handler.get_channel_messages')
    @patch('teams_sync_handler.get_messages_delta')
    def test_full_sync_paginates(
        self, mock_delta, mock_get_msgs, mock_filter, mock_replies, mock_build, mock_queue
    ):
        """Test full sync follows pagination tokens."""
        mock_delta.return_value = {"delta_link": "https://delta.initial"}
        page1 = [{"id": "msg_1", "body": {"content": "Page 1"}}]
        page2 = [{"id": "msg_2", "body": {"content": "Page 2"}}]
        mock_get_msgs.side_effect = [
            (page1, "skip_token_2"),
            (page2, None),
        ]
        mock_filter.side_effect = [page1, page2]
        mock_replies.return_value = []
        mock_build.return_value = "<html>msg</html>"

        count, _ = _sync_channel_full(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            days_back=30,
            sync_id="sync_001",
            messages_so_far=0,
        )

        assert count == 2
        assert mock_get_msgs.call_count == 2


# ============================================================================
# Sync Channel Files
# ============================================================================

class TestSyncChannelFiles:
    """Test syncing files from a channel's SharePoint folder."""

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch('teams_sync_handler.list_folder_files')
    @patch('teams_sync_handler.get_channel_files_folder')
    def test_sync_files_queues_files(self, mock_folder, mock_list, mock_queue):
        """Test files are fetched and queued."""
        mock_folder.return_value = {
            "id": "folder_001",
            "parentReference": {"driveId": "drive_001"},
        }
        mock_list.return_value = [
            {
                "id": "file_1",
                "name": "doc.pdf",
                "@microsoft.graph.downloadUrl": "https://download/1",
                "size": 1024,
            },
            {
                "id": "file_2",
                "name": "sheet.xlsx",
                "@microsoft.graph.downloadUrl": "https://download/2",
                "size": 2048,
            },
        ]

        count = _sync_channel_files(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            sync_id="sync_001",
        )

        assert count == 2
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch('teams_sync_handler.list_folder_files')
    @patch('teams_sync_handler.get_channel_files_folder')
    def test_sync_files_skips_folders(self, mock_folder, mock_list, mock_queue):
        """Test folder items are skipped."""
        mock_folder.return_value = {
            "id": "folder_001",
            "parentReference": {"driveId": "drive_001"},
        }
        mock_list.return_value = [
            {
                "id": "file_1",
                "name": "doc.pdf",
                "@microsoft.graph.downloadUrl": "https://download/1",
                "size": 1024,
            },
            {
                "id": "subfolder_1",
                "name": "Subfolder",
                "folder": {"childCount": 3},
            },
        ]

        count = _sync_channel_files(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            sync_id="sync_001",
        )

        assert count == 1
        assert mock_queue.call_count == 1

    @patch('teams_sync_handler.get_channel_files_folder')
    def test_sync_files_no_files_folder(self, mock_folder):
        """Test returns 0 when no files folder found."""
        mock_folder.return_value = None

        count = _sync_channel_files(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            sync_id="sync_001",
        )

        assert count == 0

    @patch('teams_sync_handler.get_channel_files_folder')
    def test_sync_files_missing_drive_id(self, mock_folder):
        """Test returns 0 when drive ID is missing."""
        mock_folder.return_value = {
            "id": "folder_001",
            "parentReference": {},
        }

        count = _sync_channel_files(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            sync_id="sync_001",
        )

        assert count == 0

    @patch('teams_sync_handler.list_folder_files')
    @patch('teams_sync_handler.get_channel_files_folder')
    def test_sync_files_api_error_returns_zero(self, mock_folder, mock_list):
        """Test returns 0 when list files API call fails."""
        from teams_api import TeamsAPIError

        mock_folder.return_value = {
            "id": "folder_001",
            "parentReference": {"driveId": "drive_001"},
        }
        mock_list.side_effect = TeamsAPIError("API failed")

        count = _sync_channel_files(
            access_token="token",
            workspace_id="ws_test",
            connection_id="conn_test",
            team_id="team_001",
            channel_id="chan_001",
            channel_name="General",
            sync_id="sync_001",
        )

        assert count == 0


# ============================================================================
# Process Sync Request
# ============================================================================

class TestProcessSyncRequest:
    """Test the main sync processing function."""

    @patch.object(handler_module, '_sync_channel_files')
    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_success(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full, mock_files
    ):
        """Test successful sync request."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": True,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.return_value = (10, "https://delta.new")
        mock_files.return_value = 3

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["messages_queued"] == 10
        assert result["files_queued"] == 3

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
    def test_sync_request_no_channels(self, mock_get_conn, mock_update):
        """Test sync skips when no channels configured."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [],
            },
            "delta_links": {},
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"
        mock_update.assert_called_with(
            "ws_test", "conn_test", "no_channels_configured"
        )

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_auth_failure(self, mock_get_conn, mock_update, mock_config, mock_refresh):
        """Test sync handles auth failure."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
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

    @patch.object(handler_module, '_sync_channel_files')
    @patch.object(handler_module, '_sync_channel_delta')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_uses_delta_when_available(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_delta, mock_files
    ):
        """Test sync uses delta query when delta link exists."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {"t1_c1": "https://delta.existing"},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_delta.return_value = (5, "https://delta.updated")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["messages_queued"] == 5
        mock_delta.assert_called_once()

    @patch.object(handler_module, '_sync_channel_files')
    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_skips_channel_without_ids(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full, mock_files
    ):
        """Test sync skips channels missing team_id or channel_id."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"channel_name": "Missing IDs"},
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "Valid"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.return_value = (7, "https://delta.new")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["messages_queued"] == 7
        # Only called once for the valid channel
        assert mock_full.call_count == 1

    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_teams_auth_error(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full
    ):
        """Test sync handles TeamsAuthError during channel processing."""
        from teams_api import TeamsAuthError

        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.side_effect = TeamsAuthError("401 Unauthorized")

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

    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_unexpected_error(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full
    ):
        """Test sync handles unexpected exceptions."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.side_effect = RuntimeError("Something broke")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Something broke" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "error",
            error="Something broke"
        )

    @patch.object(handler_module, '_sync_channel_files')
    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_updates_delta_links(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full, mock_files
    ):
        """Test sync updates delta links on completion."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.return_value = (5, "https://delta.new")

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Verify update_connection_status was called with the new delta links
        completed_call = [
            c for c in mock_update.call_args_list
            if c[0][2] == "completed"
        ]
        assert len(completed_call) == 1
        assert completed_call[0][1]["delta_links"] == {"t1_c1": "https://delta.new"}

    @patch.object(handler_module, '_sync_channel_files')
    @patch.object(handler_module, '_sync_channel_full')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_request_disables_file_sync(
        self, mock_get_conn, mock_update, mock_config, mock_refresh, mock_full, mock_files
    ):
        """Test sync does not sync files when sync_files is False."""
        mock_get_conn.return_value = {
            "config": {
                "include_channels": [
                    {"team_id": "t1", "channel_id": "c1", "channel_name": "General"},
                ],
                "days_back": 30,
                "sync_files": False,
            },
            "delta_links": {},
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_full.return_value = (5, "https://delta.new")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["files_queued"] == 0
        mock_files.assert_not_called()


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'process_sync_request')
    def test_processes_sqs_records(self, mock_process):
        """Test handler processes SQS records."""
        mock_process.return_value = {"status": "success", "messages_queued": 5}

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
    def test_returns_success_count(self, mock_process):
        """Test handler logs correct success count in response."""
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "fail"},
            {"status": "success"},
        ]

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
                {"body": json.dumps({"workspace_id": "ws3", "connection_id": "c3"})},
            ]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 3
        success_count = sum(1 for r in body["results"] if r.get("status") == "success")
        assert success_count == 2
