"""Unit tests for jira-scheduler Lambda handler."""

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
mock_sqs_client = MagicMock()


def _load_handler_module():
    """Load or reload the jira-scheduler handler module."""
    if 'jira_scheduler_handler' in sys.modules:
        del sys.modules['jira_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../jira-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("jira_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['jira_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
get_all_jira_connections = handler_module.get_all_jira_connections
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    handler_module.JIRA_SYNC_QUEUE = "https://sqs.example.com/jira-sync"
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    yield


@pytest.fixture
def sample_connections():
    """Sample Jira connections from DynamoDB scan."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "JIRA_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_projects": [{"key": "PROJ", "name": "My Project"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "JIRA_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_projects": [{"key": "ENG", "name": "Engineering"}],
        },
    ]


@pytest.fixture
def three_connections():
    """Three Jira connections for multi-connection tests."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "JIRA_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_projects": [{"key": "PROJ1", "name": "Project One"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "JIRA_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_projects": [{"key": "PROJ2", "name": "Project Two"}],
        },
        {
            "PK": "WS#ws_003",
            "SK": "JIRA_CONNECTION#conn_003",
            "connection_id": "conn_003",
            "workspace_id": "ws_003",
            "selected_projects": [
                {"key": "PROJ3A", "name": "Project Three A"},
                {"key": "PROJ3B", "name": "Project Three B"},
            ],
        },
    ]


# ============================================================================
# Get All Jira Connections
# ============================================================================

class TestGetAllJiraConnections:
    """Test scanning for active Jira connections."""

    def test_get_connections_success(self, sample_connections):
        """Test scanning returns active connections with selected projects."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": sample_connections}
        ]

        result = get_all_jira_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[1]["workspace_id"] == "ws_002"

    def test_get_connections_filters_no_projects(self):
        """Test filters out connections without selected_projects."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "JIRA_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    "selected_projects": [{"key": "PROJ", "name": "My Project"}],
                },
                {
                    "PK": "WS#ws_002",
                    "SK": "JIRA_CONNECTION#conn_002",
                    "connection_id": "conn_002",
                    "selected_projects": [],  # Empty - should be excluded
                },
            ]}
        ]

        result = get_all_jira_connections()
        assert len(result) == 1
        assert result[0]["connection_id"] == "conn_001"

    def test_get_connections_filters_missing_projects_key(self):
        """Test filters out connections where selected_projects key is absent."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "JIRA_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    # No selected_projects key at all
                },
            ]}
        ]

        result = get_all_jira_connections()
        assert result == []

    def test_get_connections_empty(self):
        """Test returns empty list when no connections exist."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        result = get_all_jira_connections()
        assert result == []

    def test_get_connections_dynamodb_error(self):
        """Test handles DynamoDB ClientError gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal Server Error"}}, "Scan"
        )

        result = get_all_jira_connections()
        assert result == []

    def test_get_connections_extracts_workspace_id(self):
        """Test workspace_id is correctly extracted from PK prefix."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#my_workspace_123",
                    "SK": "JIRA_CONNECTION#conn_x",
                    "connection_id": "conn_x",
                    "selected_projects": [{"key": "TST"}],
                },
            ]}
        ]

        result = get_all_jira_connections()
        assert result[0]["workspace_id"] == "my_workspace_123"

    def test_get_connections_skips_non_ws_pk(self):
        """Test items with PK not starting with WS# are excluded."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "OTHER#something",
                    "SK": "JIRA_CONNECTION#conn_x",
                    "connection_id": "conn_x",
                    "selected_projects": [{"key": "TST"}],
                },
            ]}
        ]

        result = get_all_jira_connections()
        # Item has selected_projects but no workspace_id was set because
        # PK doesn't start with WS#, so selected_projects check passes
        # but workspace_id is not injected
        assert len(result) == 0

    def test_get_connections_multiple_pages(self):
        """Test pagination across multiple DynamoDB pages."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_page1",
                    "SK": "JIRA_CONNECTION#conn_p1",
                    "connection_id": "conn_p1",
                    "selected_projects": [{"key": "P1"}],
                },
            ]},
            {"Items": [
                {
                    "PK": "WS#ws_page2",
                    "SK": "JIRA_CONNECTION#conn_p2",
                    "connection_id": "conn_p2",
                    "selected_projects": [{"key": "P2"}],
                },
            ]},
        ]

        result = get_all_jira_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_page1"
        assert result[1]["workspace_id"] == "ws_page2"

    def test_get_connections_page_with_no_items_key(self):
        """Test handles pages that have no Items key gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {},  # No Items key
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "JIRA_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    "selected_projects": [{"key": "TST"}],
                },
            ]},
        ]

        result = get_all_jira_connections()
        assert len(result) == 1
        assert result[0]["workspace_id"] == "ws_001"

    def test_get_connections_uses_correct_table(self):
        """Test that the scan uses the configured WORKSPACES_TABLE."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        get_all_jira_connections()

        mock_dynamodb_resource.Table.assert_called_once_with("test-workspaces")

    def test_get_connections_uses_correct_filter(self):
        """Test that the scan filters on JIRA_CONNECTION# SK prefix."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        get_all_jira_connections()

        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs["FilterExpression"] == "begins_with(SK, :sk_prefix)"
        assert call_kwargs["ExpressionAttributeValues"] == {":sk_prefix": "JIRA_CONNECTION#"}

    def test_get_connections_empty_pk(self):
        """Test item with empty PK string is excluded."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "",
                    "SK": "JIRA_CONNECTION#conn_x",
                    "connection_id": "conn_x",
                    "selected_projects": [{"key": "TST"}],
                },
            ]}
        ]

        result = get_all_jira_connections()
        assert len(result) == 0


# ============================================================================
# Queue Sync Job
# ============================================================================

class TestQueueSyncJob:
    """Test queuing sync jobs to SQS."""

    def test_queue_sync_job_success(self):
        """Test queuing a sync job sends correct SQS message."""
        connection = {
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        }

        result = queue_sync_job(connection)
        assert result is True

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["QueueUrl"] == "https://sqs.example.com/jira-sync"
        msg = json.loads(call_args["MessageBody"])
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["triggered_by"] == "scheduler"

    def test_queue_sync_job_message_body_format(self):
        """Test the SQS message body contains exactly the expected keys."""
        connection = {
            "workspace_id": "ws_abc",
            "connection_id": "conn_xyz",
            "extra_field": "should_not_appear",
        }

        queue_sync_job(connection)

        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert set(msg.keys()) == {"workspace_id", "connection_id", "triggered_by"}

    def test_queue_sync_job_missing_workspace(self):
        """Test returns False when workspace_id is missing."""
        result = queue_sync_job({"connection_id": "conn_test"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_missing_connection_id(self):
        """Test returns False when connection_id is missing."""
        result = queue_sync_job({"workspace_id": "ws_test"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_empty_dict(self):
        """Test returns False for empty connection dict."""
        result = queue_sync_job({})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_none_workspace(self):
        """Test returns False when workspace_id is None."""
        result = queue_sync_job({"workspace_id": None, "connection_id": "conn_test"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_none_connection_id(self):
        """Test returns False when connection_id is None."""
        result = queue_sync_job({"workspace_id": "ws_test", "connection_id": None})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_sqs_error(self):
        """Test returns False on SQS ClientError."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal Error"}}, "SendMessage"
        )

        result = queue_sync_job({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })
        assert result is False

    def test_queue_sync_job_sqs_access_denied(self):
        """Test returns False on SQS AccessDeniedException."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access Denied"}},
            "SendMessage",
        )

        result = queue_sync_job({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })
        assert result is False

    def test_queue_sync_job_uses_configured_queue_url(self):
        """Test that the message is sent to the configured queue URL."""
        handler_module.JIRA_SYNC_QUEUE = "https://sqs.us-west-2.amazonaws.com/123456/my-queue"

        queue_sync_job({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })

        call_args = mock_sqs_client.send_message.call_args[1]
        assert call_args["QueueUrl"] == "https://sqs.us-west-2.amazonaws.com/123456/my-queue"


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_queues_all_connections(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler queues sync for all active connections."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 2
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_passes_each_connection_to_queue(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler passes each individual connection to queue_sync_job."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        lambda_handler({}, None)

        calls = mock_queue.call_args_list
        assert calls[0][0][0] == sample_connections[0]
        assert calls[1][0][0] == sample_connections[1]

    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_no_connections(self, mock_get_conns):
        """Test handler returns 200 with zero connections found."""
        mock_get_conns.return_value = []

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 0
        assert body["message"] == "No active connections to sync"

    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_no_connections_body_has_no_jobs_queued(self, mock_get_conns):
        """Test handler response for no connections does not include jobs_queued."""
        mock_get_conns.return_value = []

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert "jobs_queued" not in body

    def test_handler_no_queue_configured(self):
        """Test handler returns 500 when JIRA_SYNC_QUEUE is not set."""
        handler_module.JIRA_SYNC_QUEUE = None

        result = lambda_handler({}, None)
        assert result["statusCode"] == 500
        assert "not configured" in result["body"]

    def test_handler_empty_string_queue_configured(self):
        """Test handler returns 500 when JIRA_SYNC_QUEUE is empty string."""
        handler_module.JIRA_SYNC_QUEUE = ""

        result = lambda_handler({}, None)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_partial_queue_failure(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler counts partial successes correctly."""
        mock_get_conns.return_value = sample_connections
        mock_queue.side_effect = [True, False]  # First succeeds, second fails

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 1

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_all_queue_failures(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler returns 200 with zero jobs queued when all fail."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = False

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 0

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_three_connections_mixed_results(self, mock_get_conns, mock_queue, three_connections):
        """Test handler with three connections and mixed queue results."""
        mock_get_conns.return_value = three_connections
        mock_queue.side_effect = [True, False, True]

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["connections_found"] == 3
        assert body["jobs_queued"] == 2

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_response_message(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler returns correct completion message."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["message"] == "Jira sync scheduling completed"

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_single_connection(self, mock_get_conns, mock_queue):
        """Test handler with a single active connection."""
        mock_get_conns.return_value = [{
            "workspace_id": "ws_only",
            "connection_id": "conn_only",
            "selected_projects": [{"key": "SOLO"}],
        }]
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 1
        assert body["jobs_queued"] == 1

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_jira_connections')
    def test_handler_ignores_event_content(self, mock_get_conns, mock_queue):
        """Test handler works regardless of event payload (EventBridge triggered)."""
        mock_get_conns.return_value = []

        # EventBridge event has specific structure but handler ignores it
        eventbridge_event = {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "detail": {},
        }

        result = lambda_handler(eventbridge_event, None)
        assert result["statusCode"] == 200
