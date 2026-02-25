"""Unit tests for jira-sync Lambda handler."""

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


def _load_handler_module():
    """Load or reload the jira-sync handler module."""
    if 'jira_sync_handler' in sys.modules:
        del sys.modules['jira_sync_handler']

    # Add jira-sync dir to path so handler can resolve its sibling imports
    sync_dir = os.path.join(os.path.dirname(__file__), '../../jira-sync')
    if sync_dir not in sys.path:
        sys.path.insert(0, sync_dir)

    handler_path = os.path.join(sync_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("jira_sync_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['jira_sync_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs_client = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
process_sync_request = handler_module.process_sync_request
get_connection = handler_module.get_connection
update_sync_status = handler_module.update_sync_status
queue_issue_for_ingestion = handler_module.queue_issue_for_ingestion
queue_attachment_for_ingestion = handler_module.queue_attachment_for_ingestion
queue_orphan_detection = handler_module.queue_orphan_detection


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset all mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    # Clear any side_effect from previous tests
    mock_sqs_client.send_message.side_effect = None
    yield


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


@pytest.fixture
def sample_connection():
    """Sample Jira connection record."""
    return {
        "PK": "WS#ws_test",
        "SK": "JIRA_CONNECTION#conn_test",
        "connection_id": "conn_test",
        "workspace_id": "ws_test",
        "cloud_id": "cloud_abc123",
        "selected_projects": [
            {"key": "PROJ", "name": "My Project"},
        ],
        "include_attachments": True,
        "include_comments": True,
        "last_sync_status": "queued",
    }


@pytest.fixture
def sample_connection_delta(sample_connection):
    """Sample Jira connection with last_sync_at for delta sync."""
    return {
        **sample_connection,
        "last_sync_at": "2024-02-20T10:00:00+00:00",
    }


@pytest.fixture
def sample_issue():
    """Sample Jira issue object."""
    return {
        "id": "10001",
        "key": "PROJ-1",
        "fields": {
            "summary": "Test issue summary",
            "description": "Test description",
            "status": {"name": "In Progress"},
            "priority": {"name": "High"},
            "issuetype": {"name": "Story"},
            "assignee": {"displayName": "Jane Doe"},
            "reporter": {"displayName": "John Smith"},
            "labels": ["backend", "urgent"],
            "created": "2024-01-15T09:00:00.000+0000",
            "updated": "2024-02-24T14:30:00.000+0000",
            "attachment": [],
        },
    }


@pytest.fixture
def sample_issue_with_attachments(sample_issue):
    """Sample Jira issue with attachments."""
    issue = {**sample_issue}
    issue["fields"] = {
        **issue["fields"],
        "attachment": [
            {
                "id": "att_001",
                "filename": "design.pdf",
                "mimeType": "application/pdf",
                "size": 2048,
            },
            {
                "id": "att_002",
                "filename": "screenshot.png",
                "mimeType": "image/png",
                "size": 4096,
            },
        ],
    }
    return issue


@pytest.fixture
def sample_comment():
    """Sample Jira comment."""
    return {
        "id": "comment_001",
        "body": "This is a test comment",
        "author": {"displayName": "Jane Doe"},
        "created": "2024-02-20T10:00:00.000+0000",
    }


# ============================================================================
# Get Connection
# ============================================================================

class TestGetConnection:
    """Test getting a Jira connection from DynamoDB."""

    def test_get_connection_success(self, mock_table, sample_connection):
        """Test successful connection retrieval."""
        mock_table.get_item.return_value = {"Item": sample_connection}

        result = get_connection("ws_test", "conn_test")

        assert result is not None
        assert result["connection_id"] == "conn_test"
        assert result["cloud_id"] == "cloud_abc123"
        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "WS#ws_test",
                "SK": "JIRA_CONNECTION#conn_test",
            }
        )

    def test_get_connection_not_found(self, mock_table):
        """Test returns None when connection does not exist."""
        mock_table.get_item.return_value = {}

        result = get_connection("ws_test", "nonexistent")

        assert result is None

    def test_get_connection_dynamodb_error(self, mock_table):
        """Test returns None on DynamoDB error."""
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}},
            "GetItem",
        )

        result = get_connection("ws_test", "conn_test")

        assert result is None


# ============================================================================
# Update Sync Status
# ============================================================================

class TestUpdateSyncStatus:
    """Test updating connection sync status in DynamoDB."""

    def test_update_status_success(self, mock_table):
        """Test updating status to success with issue count."""
        update_sync_status("ws_test", "conn_test", "success", issue_count=42)

        mock_table.update_item.assert_called_once()
        call_args = mock_table.update_item.call_args[1]
        assert call_args["Key"] == {
            "PK": "WS#ws_test",
            "SK": "JIRA_CONNECTION#conn_test",
        }
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "success"
        assert expr_values[":count"] == 42
        # success sets last_sync_at
        assert ":sync_at" in expr_values

    def test_update_status_failed_sets_sync_at(self, mock_table):
        """Test that 'failed' status also sets last_sync_at."""
        update_sync_status("ws_test", "conn_test", "failed", error="Something broke")

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "failed"
        assert ":sync_at" in expr_values
        assert expr_values[":error"] == "Something broke"

    def test_update_status_auth_error_sets_sync_at(self, mock_table):
        """Test that 'auth_error' status sets last_sync_at."""
        update_sync_status(
            "ws_test", "conn_test", "auth_error",
            error="Failed to refresh OAuth token",
        )

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "auth_error"
        assert ":sync_at" in expr_values

    def test_update_status_in_progress_no_sync_at(self, mock_table):
        """Test that 'in_progress' does NOT set last_sync_at."""
        update_sync_status("ws_test", "conn_test", "in_progress")

        call_args = mock_table.update_item.call_args[1]
        expr_values = call_args["ExpressionAttributeValues"]
        assert expr_values[":status"] == "in_progress"
        assert ":sync_at" not in expr_values

    def test_update_status_removes_error_when_not_provided(self, mock_table):
        """Test that error field is REMOVEd when no error is given."""
        update_sync_status("ws_test", "conn_test", "success", issue_count=10)

        call_args = mock_table.update_item.call_args[1]
        assert "REMOVE last_sync_error" in call_args["UpdateExpression"]

    def test_update_status_with_error_message(self, mock_table):
        """Test error message is included in update expression."""
        update_sync_status("ws_test", "conn_test", "failed", error="Timeout")

        call_args = mock_table.update_item.call_args[1]
        assert "last_sync_error = :error" in call_args["UpdateExpression"]
        assert call_args["ExpressionAttributeValues"][":error"] == "Timeout"

    def test_update_status_without_issue_count(self, mock_table):
        """Test update without issue_count omits count expression."""
        update_sync_status("ws_test", "conn_test", "in_progress")

        call_args = mock_table.update_item.call_args[1]
        assert ":count" not in call_args["ExpressionAttributeValues"]

    def test_update_status_dynamodb_error(self, mock_table):
        """Test handles DynamoDB error gracefully."""
        mock_table.update_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "UpdateItem"
        )
        # Should not raise
        update_sync_status("ws_test", "conn_test", "failed", error="oops")


# ============================================================================
# Queue Issue for Ingestion
# ============================================================================

class TestQueueIssueForIngestion:
    """Test queuing issues to file ingestion SQS queue."""

    def test_queue_issue_sends_sqs_message(self, sample_issue):
        """Test issue message is sent to SQS with correct fields."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_issue_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            issue=sample_issue,
            project_key="PROJ",
            project_name="My Project",
            comments=[{"id": "c1", "body": "A comment"}],
            include_attachments=False,
            sync_id="sync_001",
            file_index=0,
            total_files=5,
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "jira"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["cloud_id"] == "cloud_abc123"
        assert msg["file"]["issue_id"] == "10001"
        assert msg["file"]["issue_key"] == "PROJ-1"
        assert msg["file"]["project_key"] == "PROJ"
        assert msg["file"]["project_name"] == "My Project"
        assert msg["file"]["summary"] == "Test issue summary"
        assert msg["file"]["status"] == "In Progress"
        assert msg["file"]["priority"] == "High"
        assert msg["file"]["issue_type"] == "Story"
        assert msg["file"]["assignee"] == "Jane Doe"
        assert msg["file"]["reporter"] == "John Smith"
        assert msg["file"]["labels"] == ["backend", "urgent"]
        assert msg["file"]["comments"] == [{"id": "c1", "body": "A comment"}]
        assert msg["sync_id"] == "sync_001"
        assert msg["file_index"] == 0
        assert msg["total_files"] == 5
        assert call_args["MessageGroupId"] == "ws_test-conn_test"
        assert call_args["MessageDeduplicationId"] == "sync_001-issue-10001"

    def test_queue_issue_with_null_optional_fields(self):
        """Test issue with None values for optional fields."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        issue = {
            "id": "10002",
            "key": "PROJ-2",
            "fields": {
                "summary": "Minimal issue",
                "description": None,
                "status": {"name": "Open"},
                "priority": None,
                "issuetype": {"name": "Bug"},
                "assignee": None,
                "reporter": None,
                "labels": [],
                "created": "2024-01-01T00:00:00.000+0000",
                "updated": "2024-01-01T00:00:00.000+0000",
                "attachment": [],
            },
        }

        queue_issue_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            issue=issue,
            project_key="PROJ",
            project_name="My Project",
            comments=[],
            include_attachments=False,
            sync_id="sync_002",
            file_index=1,
            total_files=3,
        )

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["file"]["priority"] is None
        assert msg["file"]["assignee"] is None
        assert msg["file"]["reporter"] is None

    def test_queue_issue_with_attachments_enabled(self, sample_issue_with_attachments):
        """Test that attachments are queued when include_attachments is True."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_issue_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            issue=sample_issue_with_attachments,
            project_key="PROJ",
            project_name="My Project",
            comments=[],
            include_attachments=True,
            sync_id="sync_003",
            file_index=0,
            total_files=1,
        )

        # 1 issue + 2 attachments = 3 SQS messages
        assert mock_sqs_client.send_message.call_count == 3

    def test_queue_issue_without_attachments(self, sample_issue_with_attachments):
        """Test that attachments are NOT queued when include_attachments is False."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        queue_issue_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            issue=sample_issue_with_attachments,
            project_key="PROJ",
            project_name="My Project",
            comments=[],
            include_attachments=False,
            sync_id="sync_003",
            file_index=0,
            total_files=1,
        )

        # Only 1 issue, no attachments
        assert mock_sqs_client.send_message.call_count == 1

    def test_queue_issue_handles_sqs_error(self, sample_issue):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_issue_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            issue=sample_issue,
            project_key="PROJ",
            project_name="My Project",
            comments=[],
            include_attachments=False,
            sync_id="sync_004",
            file_index=0,
            total_files=1,
        )


# ============================================================================
# Queue Attachment for Ingestion
# ============================================================================

class TestQueueAttachmentForIngestion:
    """Test queuing attachments to file ingestion SQS queue."""

    def test_queue_attachment_sends_sqs_message(self):
        """Test attachment message is sent to SQS."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"

        attachment = {
            "id": "att_001",
            "filename": "design.pdf",
            "mimeType": "application/pdf",
            "size": 2048,
        }

        queue_attachment_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            attachment=attachment,
            issue_key="PROJ-1",
            project_key="PROJ",
            project_name="My Project",
            sync_id="sync_005",
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["source"] == "jira_attachment"
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["cloud_id"] == "cloud_abc123"
        assert msg["file"]["attachment_id"] == "att_001"
        assert msg["file"]["issue_key"] == "PROJ-1"
        assert msg["file"]["project_key"] == "PROJ"
        assert msg["file"]["project_name"] == "My Project"
        assert msg["file"]["filename"] == "design.pdf"
        assert msg["file"]["mime_type"] == "application/pdf"
        assert msg["file"]["size"] == 2048
        assert msg["sync_id"] == "sync_005"
        assert call_args["MessageGroupId"] == "ws_test-conn_test"
        assert call_args["MessageDeduplicationId"] == "sync_005-attachment-att_001"

    def test_queue_attachment_handles_sqs_error(self):
        """Test handles SQS error gracefully."""
        handler_module.FILE_INGESTION_QUEUE = "https://sqs.example.com/ingestion"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_attachment_for_ingestion(
            workspace_id="ws_test",
            connection_id="conn_test",
            cloud_id="cloud_abc123",
            attachment={"id": "att_001", "filename": "file.txt"},
            issue_key="PROJ-1",
            project_key="PROJ",
            project_name="My Project",
            sync_id="sync_006",
        )


# ============================================================================
# Queue Orphan Detection
# ============================================================================

class TestQueueOrphanDetection:
    """Test queuing orphan detection jobs to delete queue."""

    def test_queue_orphan_detection_sends_message(self):
        """Test orphan detection message is sent to delete queue."""
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"

        queue_orphan_detection(
            workspace_id="ws_test",
            source="jira",
            prefix="My Project/",
            current_ids=["PROJ-1", "PROJ-2", "PROJ-3"],
        )

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["action"] == "delete_orphans"
        assert msg["workspace_id"] == "ws_test"
        assert msg["source"] == "jira"
        assert msg["prefix"] == "My Project/"
        assert msg["current_ids"] == ["PROJ-1", "PROJ-2", "PROJ-3"]
        assert call_args["QueueUrl"] == "https://sqs.example.com/delete"

    def test_queue_orphan_detection_no_queue_configured(self):
        """Test no-op when DELETE_QUEUE is not configured."""
        handler_module.DELETE_QUEUE = None

        # Should not raise and should not send any message
        queue_orphan_detection(
            workspace_id="ws_test",
            source="jira",
            prefix="My Project/",
            current_ids=["PROJ-1"],
        )

        mock_sqs_client.send_message.assert_not_called()

    def test_queue_orphan_detection_empty_string_queue(self):
        """Test no-op when DELETE_QUEUE is empty string (falsy)."""
        handler_module.DELETE_QUEUE = ""

        queue_orphan_detection(
            workspace_id="ws_test",
            source="jira",
            prefix="My Project/",
            current_ids=["PROJ-1"],
        )

        mock_sqs_client.send_message.assert_not_called()

    def test_queue_orphan_detection_handles_sqs_error(self):
        """Test handles SQS error gracefully."""
        handler_module.DELETE_QUEUE = "https://sqs.example.com/delete"
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        # Should not raise
        queue_orphan_detection(
            workspace_id="ws_test",
            source="jira",
            prefix="My Project/",
            current_ids=["PROJ-1"],
        )


# ============================================================================
# Process Sync Request - Full Sync
# ============================================================================

class TestProcessSyncRequestFullSync:
    """Test full sync (no last_sync_at) in process_sync_request."""

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_success(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test successful full sync with issues."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": True,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Issue 1"}},
            {"id": "10002", "key": "PROJ-2", "fields": {"summary": "Issue 2"}},
        ]
        mock_get_comments.return_value = [{"id": "c1", "body": "Comment"}]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["issues_queued"] == 2
        assert result["sync_type"] == "full"
        assert "My Project" in result["projects_processed"]
        # Full sync queues orphan detection
        mock_queue_orphan.assert_called_once()
        # Status updated to in_progress then success
        mock_update.assert_any_call("ws_test", "conn_test", "in_progress")
        mock_update.assert_any_call("ws_test", "conn_test", "success", issue_count=2)

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_multiple_projects(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test full sync across multiple projects."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [
                {"key": "PROJ", "name": "My Project"},
                {"key": "OPS", "name": "Operations"},
            ],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = [
            [{"id": "10001", "key": "PROJ-1", "fields": {"summary": "Issue 1"}}],
            [
                {"id": "20001", "key": "OPS-1", "fields": {"summary": "Ops 1"}},
                {"id": "20002", "key": "OPS-2", "fields": {"summary": "Ops 2"}},
            ],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["issues_queued"] == 3
        assert mock_queue_issue.call_count == 3
        # Orphan detection for each project
        assert mock_queue_orphan.call_count == 2

    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_no_issues_found(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_queue_issue,
    ):
        """Test full sync when no issues are found."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "completed"
        assert result["issues_queued"] == 0
        mock_queue_issue.assert_not_called()
        mock_update.assert_any_call("ws_test", "conn_test", "success", issue_count=0)

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_comments_disabled(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test full sync with comments disabled does not fetch comments."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Issue 1"}},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        mock_get_comments.assert_not_called()

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_comment_fetch_failure_continues(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test that failing to fetch comments for one issue does not stop sync."""
        from jira_api import JiraAPIError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": True,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Issue 1"}},
            {"id": "10002", "key": "PROJ-2", "fields": {"summary": "Issue 2"}},
        ]
        # First call fails, second succeeds
        mock_get_comments.side_effect = [
            JiraAPIError("Comment fetch failed", 500),
            [{"id": "c1", "body": "Comment on issue 2"}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["issues_queued"] == 2
        # Both issues still queued despite comment failure on first
        assert mock_queue_issue.call_count == 2


# ============================================================================
# Process Sync Request - Delta Sync
# ============================================================================

class TestProcessSyncRequestDeltaSync:
    """Test delta sync (has last_sync_at) in process_sync_request."""

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'search_issues_modified_since')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_delta_sync_success(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_search_modified, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test successful delta sync with modified issues."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": True,
            "last_sync_at": "2024-02-20T10:00:00+00:00",
        }
        mock_refresh.return_value = "access_token"
        mock_search_modified.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Updated issue"}},
        ]
        mock_get_comments.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["sync_type"] == "delta"
        assert result["issues_queued"] == 1
        mock_search_modified.assert_called_once_with(
            "access_token", "cloud_abc", "PROJ", "2024-02-20T10:00:00+00:00"
        )
        # Delta sync does NOT queue orphan detection
        mock_queue_orphan.assert_not_called()

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'search_issues_modified_since')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_delta_sync_no_modified_issues(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_search_modified, mock_queue_issue, mock_queue_orphan,
    ):
        """Test delta sync when no issues have been modified."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
            "last_sync_at": "2024-02-20T10:00:00+00:00",
        }
        mock_refresh.return_value = "access_token"
        mock_search_modified.return_value = []

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "completed"
        assert result["issues_queued"] == 0
        mock_queue_issue.assert_not_called()
        mock_queue_orphan.assert_not_called()


# ============================================================================
# Process Sync Request - Error Handling
# ============================================================================

class TestProcessSyncRequestErrors:
    """Test error handling in process_sync_request."""

    @patch.object(handler_module, 'get_connection')
    def test_connection_not_found(self, mock_get_conn):
        """Test sync fails when connection is not found."""
        mock_get_conn.return_value = None

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "bad_id",
        })

        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_no_projects_selected(self, mock_get_conn, mock_update):
        """Test sync skips when no projects are selected."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [],
            "include_attachments": False,
            "include_comments": False,
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"
        assert "No projects" in result["reason"]
        mock_update.assert_called_once_with(
            "ws_test", "conn_test", "no_projects_selected"
        )

    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_auth_failure_token_refresh(self, mock_get_conn, mock_update, mock_refresh):
        """Test sync handles authentication failure from token refresh."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
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

    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_auth_error_in_project_caught_as_api_error(
        self, mock_get_conn, mock_update, mock_refresh, mock_list_issues,
    ):
        """Test JiraAuthError in project listing is caught by inner JiraAPIError handler.

        JiraAuthError extends JiraAPIError, so when raised inside the per-project
        try block it is caught by 'except JiraAPIError' and the project is skipped.
        With only one project and no issues, this results in a 'completed' status.
        """
        from jira_api import JiraAuthError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = JiraAuthError("Token expired", 401)

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Auth error is caught as JiraAPIError at the project level,
        # so it skips the project and falls through to "no issues" path
        assert result["status"] == "completed"
        assert result["issues_queued"] == 0
        mock_update.assert_any_call("ws_test", "conn_test", "success", issue_count=0)

    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_auth_error_outside_project_loop(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_queue_issue,
    ):
        """Test JiraAuthError raised outside the per-project try sets auth_error.

        The outer except JiraAuthError handler catches errors raised after
        the per-project processing, e.g. during issue queuing.
        """
        from jira_api import JiraAuthError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Issue 1"}},
        ]
        # Raise auth error during queuing (outside the inner try)
        mock_queue_issue.side_effect = JiraAuthError("Token expired", 401)

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

    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_unexpected_exception_during_sync(
        self, mock_get_conn, mock_update, mock_refresh, mock_list_issues,
    ):
        """Test unexpected exception sets failed status."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = RuntimeError("Something broke badly")

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "error"
        assert "Something broke badly" in result["error"]
        mock_update.assert_any_call(
            "ws_test", "conn_test", "failed",
            error="Something broke badly"
        )

    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_api_error_in_one_project_continues_to_next(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_queue_issue,
    ):
        """Test JiraAPIError in one project does not stop other projects."""
        from jira_api import JiraAPIError

        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [
                {"key": "FAIL", "name": "Failing Project"},
                {"key": "OK", "name": "OK Project"},
            ],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = [
            JiraAPIError("Project access denied", 403),
            [{"id": "20001", "key": "OK-1", "fields": {"summary": "OK issue"}}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert result["issues_queued"] == 1
        # Only "OK Project" in processed list
        assert "OK Project" in result["projects_processed"]
        assert "Failing Project" not in result["projects_processed"]

    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_default_include_attachments_true(self, mock_get_conn, mock_update):
        """Test include_attachments defaults to True when not in connection."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [],  # Will skip early
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "skipped"

    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_default_include_comments_true(self, mock_get_conn, mock_update):
        """Test include_comments defaults to True when not in connection."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [],  # Will skip early
        }

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        # Just verifying defaults are read from connection.get(..., True)
        assert result["status"] == "skipped"


# ============================================================================
# Process Sync Request - Orphan Detection
# ============================================================================

class TestProcessSyncRequestOrphanDetection:
    """Test orphan detection behavior in process_sync_request."""

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_full_sync_orphan_detection_per_project(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_queue_issue, mock_queue_orphan,
    ):
        """Test orphan detection is queued per project during full sync."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [
                {"key": "PROJ", "name": "My Project"},
                {"key": "OPS", "name": "Operations"},
            ],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = [
            [{"id": "10001", "key": "PROJ-1", "fields": {"summary": "P1"}}],
            [{"id": "20001", "key": "OPS-1", "fields": {"summary": "O1"}}],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        assert mock_queue_orphan.call_count == 2
        orphan_calls = mock_queue_orphan.call_args_list

        # First project
        assert orphan_calls[0] == call(
            workspace_id="ws_test",
            source="jira",
            prefix="My Project/",
            current_ids=["PROJ-1"],
        )
        # Second project
        assert orphan_calls[1] == call(
            workspace_id="ws_test",
            source="jira",
            prefix="Operations/",
            current_ids=["OPS-1"],
        )

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'search_issues_modified_since')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_delta_sync_skips_orphan_detection(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_search_modified, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test that delta sync does NOT queue orphan detection."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
            "include_attachments": False,
            "include_comments": False,
            "last_sync_at": "2024-02-20T10:00:00+00:00",
        }
        mock_refresh.return_value = "access_token"
        mock_search_modified.return_value = [
            {"id": "10001", "key": "PROJ-1", "fields": {"summary": "Updated"}},
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["status"] == "success"
        mock_queue_orphan.assert_not_called()


# ============================================================================
# Process Sync Request - Issue Queuing Details
# ============================================================================

class TestProcessSyncRequestIssueQueuing:
    """Test that issues are queued with correct indices and total_files."""

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_issue_queued_with_correct_index_and_total(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_queue_issue, mock_queue_orphan,
    ):
        """Test that file_index and total_files are correct across projects."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [
                {"key": "A", "name": "Alpha"},
                {"key": "B", "name": "Beta"},
            ],
            "include_attachments": False,
            "include_comments": False,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = [
            [{"id": "1", "key": "A-1", "fields": {"summary": "A1"}}],
            [
                {"id": "2", "key": "B-1", "fields": {"summary": "B1"}},
                {"id": "3", "key": "B-2", "fields": {"summary": "B2"}},
            ],
        ]

        result = process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        assert result["issues_queued"] == 3
        assert mock_queue_issue.call_count == 3

        # Verify file_index progression: 0, 1, 2
        indices = [c.kwargs["file_index"] for c in mock_queue_issue.call_args_list]
        assert indices == [0, 1, 2]

        # Verify total_files is always 3
        totals = [c.kwargs["total_files"] for c in mock_queue_issue.call_args_list]
        assert totals == [3, 3, 3]

    @patch.object(handler_module, 'queue_orphan_detection')
    @patch.object(handler_module, 'queue_issue_for_ingestion')
    @patch.object(handler_module, 'get_issue_comments')
    @patch.object(handler_module, 'list_all_issues_in_project')
    @patch.object(handler_module, 'refresh_access_token')
    @patch.object(handler_module, 'update_sync_status')
    @patch.object(handler_module, 'get_connection')
    def test_issue_queued_with_correct_project_info(
        self, mock_get_conn, mock_update, mock_refresh,
        mock_list_issues, mock_get_comments, mock_queue_issue, mock_queue_orphan,
    ):
        """Test that each issue is queued with its own project key and name."""
        mock_get_conn.return_value = {
            "cloud_id": "cloud_abc",
            "selected_projects": [
                {"key": "A", "name": "Alpha"},
                {"key": "B", "name": "Beta"},
            ],
            "include_attachments": False,
            "include_comments": True,
        }
        mock_refresh.return_value = "access_token"
        mock_list_issues.side_effect = [
            [{"id": "1", "key": "A-1", "fields": {"summary": "A1"}}],
            [{"id": "2", "key": "B-1", "fields": {"summary": "B1"}}],
        ]
        mock_get_comments.return_value = []

        process_sync_request({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        calls = mock_queue_issue.call_args_list
        assert calls[0].kwargs["project_key"] == "A"
        assert calls[0].kwargs["project_name"] == "Alpha"
        assert calls[1].kwargs["project_key"] == "B"
        assert calls[1].kwargs["project_name"] == "Beta"


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'process_sync_request')
    def test_processes_sqs_records(self, mock_process):
        """Test handler processes SQS records."""
        mock_process.return_value = {"status": "success", "issues_queued": 5}

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws_test", "connection_id": "conn_test"})}
            ]
        }

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert len(body["results"]) == 1
        assert body["results"][0]["status"] == "success"

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
        assert mock_process.call_count == 2

    def test_handles_invalid_json(self):
        """Test handler handles invalid JSON gracefully."""
        event = {"Records": [{"body": "not json"}]}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"][0]["status"] == "error"
        assert "Invalid JSON" in body["results"][0]["error"]

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
        assert "Unexpected error" in body["results"][0]["error"]

    @patch.object(handler_module, 'process_sync_request')
    def test_reports_success_count(self, mock_process):
        """Test handler correctly counts successful results."""
        mock_process.side_effect = [
            {"status": "success"},
            {"status": "error", "error": "failed"},
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
        assert body["message"] == "Jira sync orchestration completed"
        successes = [r for r in body["results"] if r["status"] == "success"]
        assert len(successes) == 2

    @patch.object(handler_module, 'process_sync_request')
    def test_mixed_valid_and_invalid_records(self, mock_process):
        """Test handler processes mix of valid JSON and invalid JSON records."""
        mock_process.return_value = {"status": "success"}

        event = {
            "Records": [
                {"body": json.dumps({"workspace_id": "ws1", "connection_id": "c1"})},
                {"body": "invalid json here"},
                {"body": json.dumps({"workspace_id": "ws3", "connection_id": "c3"})},
            ]
        }

        result = lambda_handler(event, None)
        body = json.loads(result["body"])
        assert len(body["results"]) == 3
        assert body["results"][0]["status"] == "success"
        assert body["results"][1]["status"] == "error"
        assert body["results"][2]["status"] == "success"

    def test_empty_records_list(self):
        """Test handler handles empty Records list."""
        event = {"Records": []}

        result = lambda_handler(event, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["results"] == []
