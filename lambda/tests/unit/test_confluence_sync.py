# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for confluence-sync Lambda handler."""

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
    """Load or reload the confluence-sync handler module."""
    if 'confluence_sync_handler' in sys.modules:
        del sys.modules['confluence_sync_handler']

    # Add confluence-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), '../../confluence-sync')
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)

    handler_path = os.path.join(sync_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("confluence_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['confluence_sync_handler'] = handler_module
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
queue_file_ingestion = handler_module.queue_file_ingestion
queue_attachment_ingestion = handler_module.queue_attachment_ingestion
queue_orphan_detection = handler_module.queue_orphan_detection


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    mock_ssm_client.reset_mock()
    mock_ssm_client.get_parameter.side_effect = None
    handler_module._oauth_config = None
    handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
    handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
    yield


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    """Sample Confluence connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "CONFLUENCE_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "cloud_id": "cloud_abc123",
        "selected_spaces": [
            {"id": "space_1", "key": "ENG", "name": "Engineering"},
        ],
        "include_attachments": False,
        "last_sync_status": "queued",
    }


@pytest.fixture
def sample_connection_with_attachments(sample_connection):
    """Sample Confluence connection with attachments enabled."""
    return {**sample_connection, "include_attachments": True}


@pytest.fixture
def sample_connection_delta(sample_connection):
    """Sample Confluence connection with previous sync (delta mode)."""
    return {**sample_connection, "last_sync_at": "2025-01-15T00:00:00+00:00"}


@pytest.fixture
def sample_page():
    """Sample Confluence page object."""
    return {
        "id": "page_001",
        "title": "Architecture Overview",
        "status": "current",
        "version": {
            "number": 3,
            "createdAt": "2025-02-01T10:00:00.000Z",
        },
    }


@pytest.fixture
def sample_page_old():
    """Sample Confluence page not modified since last sync."""
    return {
        "id": "page_002",
        "title": "Old Page",
        "status": "current",
        "version": {
            "number": 1,
            "createdAt": "2024-12-01T10:00:00.000Z",
        },
    }


@pytest.fixture
def sample_attachment():
    """Sample Confluence attachment object."""
    return {
        "id": "att_100",
        "title": "diagram.png",
        "mediaType": "image/png",
        "fileSize": 2048,
    }


# ============================================================================
# Get Connection
# ============================================================================

class TestGetConnection:
    """Test fetching a connection from DynamoDB."""

    def test_get_connection_success(self, mock_table):
        """Test successful connection lookup."""
        mock_table.get_item.return_value = {
            "Item": {
                "PK": "WS#ws_test",
                "SK": "CONFLUENCE_CONNECTION#conn_test",
                "cloud_id": "cloud_abc123",
            }
        }

        result = get_connection("ws_test", "conn_test")

        assert result is not None
        assert result["cloud_id"] == "cloud_abc123"
        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "WS#ws_test",
                "SK": "CONFLUENCE_CONNECTION#conn_test",
            }
        )

    def test_get_connection_not_found(self, mock_table):
        """Test connection not found returns None."""
        mock_table.get_item.return_value = {}

        result = get_connection("ws_test", "conn_missing")

        assert result is None

    def test_get_connection_dynamodb_error(self, mock_table):
        """Test DynamoDB error returns None gracefully."""
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "GetItem",
        )

        result = get_connection("ws_test", "conn_test")

        assert result is None


# ============================================================================
# Update Connection Status
# ============================================================================

class TestUpdateConnectionStatus:
    """Test updating connection sync status in DynamoDB."""

    def test_update_status_syncing(self, mock_table):
        """Test updating to syncing status (no last_sync_at set)."""
        update_connection_status("ws_test", "conn_test", "syncing")

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "syncing"
        # syncing should not set last_sync_at
        assert ":sync_at" not in call_args["ExpressionAttributeValues"]

    def test_update_status_completed_sets_sync_at(self, mock_table):
        """Test completed status sets last_sync_at."""
        update_connection_status("ws_test", "conn_test", "completed", page_count=25)

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "completed"
        assert ":sync_at" in call_args["ExpressionAttributeValues"]
        assert call_args["ExpressionAttributeValues"][":count"] == 25

    def test_update_status_error_sets_sync_at(self, mock_table):
        """Test error status sets last_sync_at."""
        update_connection_status("ws_test", "conn_test", "error", error="API timeout")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "error"
        assert ":sync_at" in call_args["ExpressionAttributeValues"]
        assert call_args["ExpressionAttributeValues"][":error"] == "API timeout"

    def test_update_status_auth_error_sets_sync_at(self, mock_table):
        """Test auth_error status sets last_sync_at."""
        update_connection_status("ws_test", "conn_test", "auth_error", error="Token expired")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["ExpressionAttributeValues"][":status"] == "auth_error"
        assert ":sync_at" in call_args["ExpressionAttributeValues"]
        assert ":error" in call_args["ExpressionAttributeValues"]

    def test_update_status_without_error_removes_last_sync_error(self, mock_table):
        """Test status update without error REMOVEs last_sync_error."""
        update_connection_status("ws_test", "conn_test", "completed")

        call_args = mock_table.update_item.call_args[1]
        assert "REMOVE last_sync_error" in call_args["UpdateExpression"]
        assert ":error" not in call_args["ExpressionAttributeValues"]

    def test_update_status_zero_page_count_not_set(self, mock_table):
        """Test page_count=0 does not add :count to expression."""
        update_connection_status("ws_test", "conn_test", "completed", page_count=0)

        call_args = mock_table.update_item.call_args[1]
        assert ":count" not in call_args["ExpressionAttributeValues"]

    def test_update_status_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_connection_status("ws_test", "conn_test", "error")

    def test_update_status_correct_key(self, mock_table):
        """Test the DynamoDB key is correctly formed."""
        update_connection_status("ws_abc", "conn_xyz", "syncing")

        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"] == {
            "PK": "WS#ws_abc",
            "SK": "CONFLUENCE_CONNECTION#conn_xyz",
        }


# ============================================================================
# Queue File Ingestion
# ============================================================================

class TestQueueFileIngestion:
    """Test queuing pages to file ingestion."""

    def test_queue_file_sends_sqs_message(self, sample_page):
        """Test page message is sent to SQS with correct structure."""
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=sample_page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_001",
            file_index=0,
            total_files=10,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])

        assert msg["source"] == "confluence"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["cloud_id"] == "cloud_abc"
        assert msg["file"]["page_id"] == "page_001"
        assert msg["file"]["title"] == "Architecture Overview"
        assert msg["file"]["space_key"] == "ENG"
        assert msg["file"]["space_name"] == "Engineering"
        assert msg["sync_id"] == "sync_001"
        assert msg["file_index"] == 0
        assert msg["total_files"] == 10

    def test_queue_file_uses_correct_queue_url(self, sample_page):
        """Test SQS message is sent to FILE_INGESTION_QUEUE."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/my-queue"

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=sample_page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["QueueUrl"] == "https://sqs.example.com/my-queue"

    def test_queue_file_message_group_id(self, sample_page):
        """Test MessageGroupId uses workspace-connection combo."""
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=sample_page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageGroupId"] == "ws_test-conn_test"

    def test_queue_file_deduplication_id(self, sample_page):
        """Test MessageDeduplicationId uses sync_id and page_id."""
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=sample_page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_abc",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageDeduplicationId"] == "sync_abc-page-page_001"

    def test_queue_file_handles_sqs_error(self, sample_page):
        """Test handles SQS error gracefully."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=sample_page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

    def test_queue_file_handles_missing_page_fields(self):
        """Test handles page dict with missing optional fields."""
        page = {"id": "page_minimal"}

        queue_file_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            page=page,
            space_key="ENG",
            space_name="Engineering",
            sync_id="sync_001",
            file_index=0,
            total_files=1,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["page_id"] == "page_minimal"
        assert msg["file"]["title"] == ""


# ============================================================================
# Queue Attachment Ingestion
# ============================================================================

class TestQueueAttachmentIngestion:
    """Test queuing attachments to file ingestion."""

    def test_queue_attachment_sends_sqs_message(self, sample_attachment):
        """Test attachment message is sent to SQS with correct structure."""
        queue_attachment_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            attachment=sample_attachment,
            page_id="page_001",
            space_name="Engineering",
            sync_id="sync_001",
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])

        assert msg["source"] == "confluence_attachment"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["cloud_id"] == "cloud_abc"
        assert msg["file"]["attachment_id"] == "att_100"
        assert msg["file"]["page_id"] == "page_001"
        assert msg["file"]["space_name"] == "Engineering"
        assert msg["file"]["filename"] == "diagram.png"
        assert msg["sync_id"] == "sync_001"

    def test_queue_attachment_deduplication_id(self, sample_attachment):
        """Test MessageDeduplicationId uses sync_id and attachment_id."""
        queue_attachment_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            attachment=sample_attachment,
            page_id="page_001",
            space_name="Engineering",
            sync_id="sync_xyz",
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["MessageDeduplicationId"] == "sync_xyz-attachment-att_100"

    def test_queue_attachment_handles_sqs_error(self, sample_attachment):
        """Test handles SQS error gracefully."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_attachment_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            attachment=sample_attachment,
            page_id="page_001",
            space_name="Engineering",
            sync_id="sync_001",
        )

    def test_queue_attachment_handles_missing_title(self):
        """Test handles attachment with missing title."""
        attachment = {"id": "att_200"}

        queue_attachment_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc",
            attachment=attachment,
            page_id="page_001",
            space_name="Engineering",
            sync_id="sync_001",
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["filename"] == "attachment"


# ============================================================================
# Queue Orphan Detection
# ============================================================================

class TestQueueOrphanDetection:
    """Test queuing orphan detection jobs."""

    def test_queue_orphan_detection_sends_message(self):
        """Test orphan detection message is sent to DELETE_QUEUE."""
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"

        queue_orphan_detection(
            workspace_id="ws_test",
            source="confluence",
            prefix="Engineering/",
            current_ids=["page_001", "page_002", "page_003"],
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["QueueUrl"] == "https://sqs.example.com/delete"

        msg = json.loads(call_args["MessageBody"])
        assert msg["action"] == "delete_orphans"
        assert msg["workspace_id"] == "ws_test"
        assert msg["source"] == "confluence"
        assert msg["prefix"] == "Engineering/"
        assert msg["current_ids"] == ["page_001", "page_002", "page_003"]

    def test_queue_orphan_detection_no_queue_configured(self):
        """Test no-op when DELETE_QUEUE not configured."""
        handler_module.DELETE_QUEUE = None

        # Should not raise, should not call SQS
        queue_orphan_detection(
            workspace_id="ws_test",
            source="confluence",
            prefix="Engineering/",
            current_ids=["page_001"],
        )

        mock_sqs_client.send_message.assert_not_called()

    def test_queue_orphan_detection_empty_string_queue(self):
        """Test no-op when DELETE_QUEUE is empty string (falsy)."""
        handler_module.DELETE_QUEUE = ""

        queue_orphan_detection(
            workspace_id="ws_test",
            source="confluence",
            prefix="Engineering/",
            current_ids=["page_001"],
        )

        mock_sqs_client.send_message.assert_not_called()

    def test_queue_orphan_detection_sqs_error(self):
        """Test handles SQS error gracefully."""
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_orphan_detection(
            workspace_id="ws_test",
            source="confluence",
            prefix="Engineering/",
            current_ids=["page_001"],
        )


# ============================================================================
# Load OAuth Config
# ============================================================================

class TestLoadOAuthConfig:
    """Test loading OAuth configuration from SSM."""

    def test_load_oauth_config_success(self):
        """Test successful OAuth config load from SSM."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "my-client-id"}},
            {"Parameter": {"Value": "my-client-secret"}},
        ]

        result = handler_module._load_oauth_config()

        assert result["client_id"] == "my-client-id"
        assert result["client_secret"] == "my-client-secret"
        assert mock_ssm_client.get_parameter.call_count == 2

    def test_load_oauth_config_caches(self):
        """Test OAuth config is cached after first load."""
        handler_module._oauth_config = {"client_id": "cached", "client_secret": "cached"}

        result = handler_module._load_oauth_config()

        assert result["client_id"] == "cached"
        mock_ssm_client.get_parameter.assert_not_called()

    def test_load_oauth_config_ssm_error(self):
        """Test SSM error raises ClientError."""
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )

        with pytest.raises(ClientError):
            handler_module._load_oauth_config()

    def test_load_oauth_config_decrypts_secret(self):
        """Test client secret is fetched with WithDecryption=True."""
        mock_ssm_client.get_parameter.side_effect = [
            {"Parameter": {"Value": "cid"}},
            {"Parameter": {"Value": "csec"}},
        ]

        handler_module._load_oauth_config()

        # Second call should have WithDecryption=True (client secret)
        second_call = mock_ssm_client.get_parameter.call_args_list[1]
        assert second_call[1]["WithDecryption"] is True


# ============================================================================
# Process Sync Request
# ============================================================================

class TestProcessSyncRequest:
    """Test the main sync processing function."""

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_full_success(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test successful full sync request."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [
                {"id": "space_1", "key": "ENG", "name": "Engineering"},
            ],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "Page 1"},
            {"id": "p2", "title": "Page 2"},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 2
        assert result["sync_type"] == "full"
        assert "Engineering" in result["spaces_processed"]
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'get_connection')
    def test_sync_connection_not_found(self, mock_get_conn):
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
    def test_sync_no_spaces_selected(self, mock_get_conn, mock_update):
        """Test sync skips when no spaces selected."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [],
            "include_attachments": False,
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"
        assert "No spaces selected" in result["reason"]
        mock_update.assert_called_once_with("ws_test", "conn_test", "no_spaces_selected")

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_auth_failure(self, mock_get_conn, mock_update, mock_config, mock_refresh):
        """Test sync handles authentication failure."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
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
            error="Failed to refresh OAuth token",
        )

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_no_pages_found(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue,
    ):
        """Test sync completes with zero pages when spaces are empty."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Empty sync returns "success" so the SQS envelope doesn't DLQ it
        # (830f4a3 fix). DDB write still uses "completed" as the persisted
        # last_sync_status vocabulary.
        assert result["status"] == "success"
        assert result["pages_queued"] == 0
        mock_queue.assert_not_called()
        mock_update.assert_any_call("ws_test", "conn_test", "completed", page_count=0)

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_multiple_spaces(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test sync processes multiple spaces."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [
                {"id": "s1", "key": "ENG", "name": "Engineering"},
                {"id": "s2", "key": "OPS", "name": "Operations"},
            ],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.side_effect = [
            [{"id": "p1", "title": "Page 1"}],
            [{"id": "p2", "title": "Page 2"}, {"id": "p3", "title": "Page 3"}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 3
        assert "Engineering" in result["spaces_processed"]
        assert "Operations" in result["spaces_processed"]
        assert mock_queue.call_count == 3

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_space_api_error_continues_other_spaces(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test sync continues when one space fails (ConfluenceAPIError)."""
        from confluence_api import ConfluenceAPIError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [
                {"id": "s1", "key": "ENG", "name": "Engineering"},
                {"id": "s2", "key": "OPS", "name": "Operations"},
            ],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.side_effect = [
            ConfluenceAPIError("Space not accessible", status_code=403),
            [{"id": "p2", "title": "Page 2"}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 1
        # Only Operations should be in spaces_processed
        assert "Engineering" not in result["spaces_processed"]
        assert "Operations" in result["spaces_processed"]

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_confluence_auth_error(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue,
    ):
        """Test sync handles ConfluenceAuthError at top level."""
        from confluence_api import ConfluenceAuthError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        # ConfluenceAuthError is a subclass of ConfluenceAPIError, but the handler
        # has a separate except block for it at the top level
        mock_list_pages.side_effect = ConfluenceAuthError("401 Unauthorized", status_code=401)

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # The ConfluenceAuthError is caught inside the for loop by the
        # ConfluenceAPIError handler (since it's a subclass), so it continues.
        # With only one space, all_pages will be empty -> empty-sync path
        # returns "success" (830f4a3 fix).
        assert result["status"] == "success"
        assert result["pages_queued"] == 0

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_delta_filters_pages(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test delta sync filters pages by last_sync_at."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
            "last_sync_at": "2025-01-15T00:00:00+00:00",
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            # Modified after last_sync_at -> included
            {"id": "p1", "title": "New Page", "version": {"createdAt": "2025-02-01T10:00:00.000Z"}},
            # Modified before last_sync_at -> excluded
            {"id": "p2", "title": "Old Page", "version": {"createdAt": "2024-12-01T10:00:00.000Z"}},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["pages_queued"] == 1
        assert result["sync_type"] == "delta"
        assert mock_queue.call_count == 1
        # Verify the correct page was queued
        queued_page = mock_queue.call_args[1]["page"]
        assert queued_page["id"] == "p1"

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_delta_skips_orphan_detection(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test delta sync does NOT queue orphan detection."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
            "last_sync_at": "2025-01-15T00:00:00+00:00",
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "New Page", "version": {"createdAt": "2025-02-01T10:00:00.000Z"}},
        ]

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        mock_orphan.assert_not_called()

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_full_queues_orphan_detection_per_space(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test full sync queues orphan detection for each space."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [
                {"id": "s1", "key": "ENG", "name": "Engineering"},
                {"id": "s2", "key": "OPS", "name": "Operations"},
            ],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.side_effect = [
            [{"id": "p1", "title": "Page 1"}],
            [{"id": "p2", "title": "Page 2"}],
        ]

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert mock_orphan.call_count == 2
        # Verify orphan detection calls
        orphan_calls = mock_orphan.call_args_list
        assert orphan_calls[0][1]["prefix"] == "Engineering/"
        assert orphan_calls[0][1]["current_ids"] == ["p1"]
        assert orphan_calls[1][1]["prefix"] == "Operations/"
        assert orphan_calls[1][1]["current_ids"] == ["p2"]

    @patch.object(handler_module, 'list_page_attachments')
    @patch.object(handler_module, 'queue_attachment_ingestion')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_with_attachments(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue_page, mock_queue_att, mock_list_att,
    ):
        """Test sync queues attachments when include_attachments is True."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": True,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "Page 1"},
        ]
        mock_list_att.return_value = [
            {"id": "att_1", "title": "file.pdf"},
            {"id": "att_2", "title": "image.png"},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["attachments_queued"] == 2
        assert mock_queue_att.call_count == 2

    @patch.object(handler_module, 'list_page_attachments')
    @patch.object(handler_module, 'queue_attachment_ingestion')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_attachments_not_included_when_disabled(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue_page, mock_queue_att, mock_list_att,
    ):
        """Test attachments are not queued when include_attachments is False."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [{"id": "p1", "title": "Page 1"}]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert "attachments_queued" not in result
        mock_list_att.assert_not_called()
        mock_queue_att.assert_not_called()

    @patch.object(handler_module, 'list_page_attachments')
    @patch.object(handler_module, 'queue_attachment_ingestion')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_attachment_api_error_continues(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue_page, mock_queue_att, mock_list_att,
    ):
        """Test sync continues when listing attachments fails for a page."""
        from confluence_api import ConfluenceAPIError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": True,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "Page 1"},
            {"id": "p2", "title": "Page 2"},
        ]
        # First page attachments fail, second succeeds
        mock_list_att.side_effect = [
            ConfluenceAPIError("Attachment error", status_code=500),
            [{"id": "att_1", "title": "file.pdf"}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["attachments_queued"] == 1
        assert mock_queue_att.call_count == 1

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_unexpected_exception(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue,
    ):
        """Test sync handles unexpected exceptions."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.side_effect = RuntimeError("Something unexpected")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Something unexpected" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "error",
            error="Something unexpected",
        )

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_sets_status_syncing_before_processing(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test that status is set to 'syncing' before listing pages."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [{"id": "p1", "title": "Page 1"}]

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # First call should be 'syncing', last should be 'completed'
        update_calls = mock_update.call_args_list
        assert update_calls[0] == call("ws_test", "conn_test", "syncing")
        assert update_calls[-1] == call(
            "ws_test", "conn_test", "completed", page_count=1
        )

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_space_key_fallback_for_space_name(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue,
    ):
        """Test space_name falls back to space_key when name is missing."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [{"id": "p1", "title": "Page 1"}]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        # space_name should fall back to space_key
        queued_call = mock_queue.call_args
        assert queued_call[1]["space_name"] == "ENG"

    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_file_index_increments(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue,
    ):
        """Test file_index increments correctly across pages."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "Page 1"},
            {"id": "p2", "title": "Page 2"},
            {"id": "p3", "title": "Page 3"},
        ]

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert mock_queue.call_count == 3
        # Verify file_index values: 0, 1, 2
        for i, call_args in enumerate(mock_queue.call_args_list):
            assert call_args[1]["file_index"] == i
            assert call_args[1]["total_files"] == 3

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_file_ingestion')
    @patch.object(handler_module, 'list_pages_in_space')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, '_load_oauth_config')
    @patch.object(handler_module, 'update_connection_status')
    @patch.object(handler_module, 'get_connection')
    def test_sync_delta_all_pages_filtered_out(
        self, mock_get_conn, mock_update, mock_config, mock_refresh,
        mock_list_pages, mock_queue, mock_orphan,
    ):
        """Test delta sync with all pages older than last_sync_at."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_spaces": [{"id": "s1", "key": "ENG", "name": "Engineering"}],
            "include_attachments": False,
            "last_sync_at": "2025-03-01T00:00:00+00:00",
        }
        mock_config.return_value = {"client_id": "cid", "client_secret": "csec"}
        mock_refresh.return_value = "access_token"
        mock_list_pages.return_value = [
            {"id": "p1", "title": "Old Page", "version": {"createdAt": "2025-01-01T10:00:00.000Z"}},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Empty delta (all filtered out) returns "success" per 830f4a3.
        assert result["status"] == "success"
        assert result["pages_queued"] == 0
        mock_queue.assert_not_called()


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler -- Chunk 02: SQS partial-batch-failure
    contract. Handler must return {"batchItemFailures": [...]} so SQS
    redelivers only the failed records instead of silently deleting on 200."""

    @patch.object(handler_module, 'process_sync_request')
    def test_processes_sqs_records(self, mock_process):
        """Success record -> empty batchItemFailures (ack all)."""
        mock_process.return_value = {"status": "success", "pages_queued": 5}

        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, 'process_sync_request')
    def test_handles_multiple_records(self, mock_process):
        """All-success batch -> empty batchItemFailures."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"messageId": "msg-2", "body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}
        assert mock_process.call_count == 2

    def test_handles_invalid_json(self):
        """Poison-pill message -> batchItemFailures so SQS routes to DLQ."""
        event = {"Records": [{"messageId": "msg-bad", "body": "not valid json at all"}]}

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-bad"}]}

    def test_handles_missing_records(self):
        """Missing Records key: still statusCode 400 (early-return guard)."""
        result = lambda_handler({}, None)

        assert result["statusCode"] == 400
        assert "Invalid event" in result["body"]

    def test_handles_empty_records(self):
        """Empty Records -> empty batchItemFailures."""
        event = {"Records": []}

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": []}

    @patch.object(handler_module, 'process_sync_request')
    def test_handles_processing_exception(self, mock_process):
        """Exception during processing -> batchItemFailures for that record."""
        mock_process.side_effect = Exception("Unexpected error")

        event = {
            "Records": [
                {"messageId": "msg-boom", "body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-boom"}]}

    @patch.object(handler_module, 'process_sync_request')
    def test_mixed_success_and_failure(self, mock_process):
        """Only failed record itemIdentifier lands in batchItemFailures --
        the two success records are ack'd (SQS deletes them)."""
        mock_process.side_effect = [
            {"status": "success", "pages_queued": 5},
            {"status": "error", "error": "Connection not found"},
            {"status": "success", "pages_queued": 3},
        ]

        event = {
            "Records": [
                {"messageId": "msg-1", "body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"messageId": "msg-2", "body": json.dumps({"workspace_id": "ws2", "connection_id": "c2"})},
                {"messageId": "msg-3", "body": json.dumps({"workspace_id": "ws3", "connection_id": "c3"})},
            ]
        }

        result = lambda_handler(event, None)
        assert result == {"batchItemFailures": [{"itemIdentifier": "msg-2"}]}
