"""Unit tests for outlook-scheduler Lambda handler."""

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
    """Load or reload the outlook-scheduler handler module."""
    if 'outlook_scheduler_handler' in sys.modules:
        del sys.modules['outlook_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../outlook-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("outlook_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['outlook_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
get_all_outlook_connections = handler_module.get_all_outlook_connections
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    handler_module.OUTLOOK_SYNC_QUEUE = "https://sqs.example.com/outlook-sync"
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    yield


@pytest.fixture
def sample_connections():
    """Sample Outlook connections from DynamoDB scan."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "OUTLOOK_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_folders": [{"id": "f1", "displayName": "Inbox"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "OUTLOOK_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_folders": [{"id": "f2", "displayName": "Archive"}],
        },
    ]


# ============================================================================
# Get All Outlook Connections
# ============================================================================

class TestGetAllOutlookConnections:
    """Test scanning for active Outlook connections."""

    def test_get_connections_success(self, sample_connections):
        """Test scanning returns active connections."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        # Mock paginator
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": sample_connections}
        ]

        result = get_all_outlook_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[1]["workspace_id"] == "ws_002"

    def test_get_connections_filters_no_folders(self):
        """Test filters out connections without selected_folders."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "OUTLOOK_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    "selected_folders": [{"id": "f1"}],
                },
                {
                    "PK": "WS#ws_002",
                    "SK": "OUTLOOK_CONNECTION#conn_002",
                    "connection_id": "conn_002",
                    "selected_folders": [],  # No folders - should be excluded
                },
            ]}
        ]

        result = get_all_outlook_connections()
        assert len(result) == 1

    def test_get_connections_empty(self):
        """Test returns empty list when no connections."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        result = get_all_outlook_connections()
        assert result == []

    def test_get_connections_dynamodb_error(self):
        """Test handles DynamoDB error gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Scan"
        )

        result = get_all_outlook_connections()
        assert result == []

    def test_get_connections_extracts_workspace_id(self):
        """Test workspace_id is extracted from PK."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#my_workspace_123",
                    "SK": "OUTLOOK_CONNECTION#conn_x",
                    "connection_id": "conn_x",
                    "selected_folders": [{"id": "f1"}],
                },
            ]}
        ]

        result = get_all_outlook_connections()
        assert result[0]["workspace_id"] == "my_workspace_123"


# ============================================================================
# Queue Sync Job
# ============================================================================

class TestQueueSyncJob:
    """Test queuing sync jobs."""

    def test_queue_sync_job_success(self):
        """Test queuing a sync job sends SQS message."""
        connection = {
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        }

        result = queue_sync_job(connection)
        assert result is True

        mock_sqs_client.send_message.assert_called_once()
        call_args = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_args["MessageBody"])
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["triggered_by"] == "scheduler"

    def test_queue_sync_job_missing_workspace(self):
        """Test returns False for invalid connection."""
        result = queue_sync_job({"connection_id": "conn_test"})
        assert result is False

    def test_queue_sync_job_missing_connection_id(self):
        """Test returns False when missing connection_id."""
        result = queue_sync_job({"workspace_id": "ws_test"})
        assert result is False

    def test_queue_sync_job_sqs_error(self):
        """Test returns False on SQS error."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )

        result = queue_sync_job({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })
        assert result is False


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_outlook_connections')
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

    @patch.object(handler_module, 'get_all_outlook_connections')
    def test_handler_no_connections(self, mock_get_conns):
        """Test handler with no active connections."""
        mock_get_conns.return_value = []

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 0

    def test_handler_no_queue_configured(self):
        """Test handler returns 500 when queue not configured."""
        handler_module.OUTLOOK_SYNC_QUEUE = None

        result = lambda_handler({}, None)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_outlook_connections')
    def test_handler_partial_queue_failure(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler counts partial successes."""
        mock_get_conns.return_value = sample_connections
        mock_queue.side_effect = [True, False]  # First succeeds, second fails

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 1
