"""Unit tests for confluence-scheduler Lambda handler."""

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


def _ensure_boto3_submodules():
    """Ensure boto3.dynamodb.conditions is importable (may be mocked by another test)."""
    boto3_mod = sys.modules.get('boto3')
    if boto3_mod is not None and not hasattr(boto3_mod, '__file__'):
        # boto3 is a MagicMock - register submodules so `from boto3.dynamodb.conditions import Key` works
        if 'boto3.dynamodb' not in sys.modules:
            sys.modules['boto3.dynamodb'] = MagicMock()
        if 'boto3.dynamodb.conditions' not in sys.modules:
            mock_conditions = MagicMock()
            sys.modules['boto3.dynamodb.conditions'] = mock_conditions


def _load_handler_module():
    """Load or reload the confluence-scheduler handler module."""
    _ensure_boto3_submodules()

    if 'confluence_scheduler_handler' in sys.modules:
        del sys.modules['confluence_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../confluence-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("confluence_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['confluence_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
get_all_confluence_connections = handler_module.get_all_confluence_connections
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    handler_module.CONFLUENCE_SYNC_QUEUE = "https://sqs.example.com/confluence-sync"
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    yield


@pytest.fixture
def sample_connections():
    """Sample Confluence connections from DynamoDB scan."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "CONFLUENCE_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "CONFLUENCE_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_spaces": [{"key": "OPS", "name": "Operations"}],
        },
    ]


@pytest.fixture
def three_connections():
    """Three Confluence connections for multi-connection scenarios."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "CONFLUENCE_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "CONFLUENCE_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_spaces": [{"key": "OPS", "name": "Operations"}],
        },
        {
            "PK": "WS#ws_003",
            "SK": "CONFLUENCE_CONNECTION#conn_003",
            "connection_id": "conn_003",
            "workspace_id": "ws_003",
            "selected_spaces": [
                {"key": "DEV", "name": "DevOps"},
                {"key": "SRE", "name": "SRE"},
            ],
        },
    ]


def _setup_paginator(items_pages):
    """Helper to set up DynamoDB table mock with paginator returning given pages.

    Args:
        items_pages: List of lists, where each inner list is the Items for one page.
    """
    mock_table = MagicMock()
    mock_dynamodb_resource.Table.return_value = mock_table

    mock_paginator = MagicMock()
    mock_table.meta.client.get_paginator.return_value = mock_paginator
    mock_paginator.paginate.return_value = [
        {"Items": page} for page in items_pages
    ]
    return mock_table, mock_paginator


# ============================================================================
# Get All Confluence Connections
# ============================================================================

class TestGetAllConfluenceConnections:
    """Test scanning for active Confluence connections."""

    def test_get_connections_success(self, sample_connections):
        """Test scanning returns active connections with selected spaces."""
        _setup_paginator([sample_connections])

        result = get_all_confluence_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[0]["connection_id"] == "conn_001"
        assert result[1]["workspace_id"] == "ws_002"
        assert result[1]["connection_id"] == "conn_002"

    def test_get_connections_filters_no_spaces(self):
        """Test filters out connections without selected_spaces."""
        items = [
            {
                "PK": "WS#ws_001",
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
            },
            {
                "PK": "WS#ws_002",
                "SK": "CONFLUENCE_CONNECTION#conn_002",
                "connection_id": "conn_002",
                "selected_spaces": [],  # No spaces - should be excluded
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert len(result) == 1
        assert result[0]["connection_id"] == "conn_001"

    def test_get_connections_filters_missing_spaces_key(self):
        """Test filters out connections where selected_spaces key is absent."""
        items = [
            {
                "PK": "WS#ws_001",
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                # No selected_spaces key at all
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_filters_none_spaces(self):
        """Test filters out connections where selected_spaces is None."""
        items = [
            {
                "PK": "WS#ws_001",
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                "selected_spaces": None,
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_empty(self):
        """Test returns empty list when no connections exist."""
        _setup_paginator([[]])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_multiple_pages(self, sample_connections):
        """Test pagination across multiple DynamoDB pages."""
        page1 = [sample_connections[0]]
        page2 = [sample_connections[1]]
        _setup_paginator([page1, page2])

        result = get_all_confluence_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[1]["workspace_id"] == "ws_002"

    def test_get_connections_dynamodb_error(self):
        """Test handles DynamoDB ClientError gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal Server Error"}}, "Scan"
        )

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_extracts_workspace_id(self):
        """Test workspace_id is correctly extracted from PK field."""
        items = [
            {
                "PK": "WS#my_workspace_abc_123",
                "SK": "CONFLUENCE_CONNECTION#conn_x",
                "connection_id": "conn_x",
                "selected_spaces": [{"key": "DOCS", "name": "Documentation"}],
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert len(result) == 1
        assert result[0]["workspace_id"] == "my_workspace_abc_123"

    def test_get_connections_skips_invalid_pk_prefix(self):
        """Test items without WS# prefix in PK are excluded."""
        items = [
            {
                "PK": "INVALID#ws_001",
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_skips_empty_pk(self):
        """Test items with empty PK are excluded."""
        items = [
            {
                "PK": "",
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_skips_missing_pk(self):
        """Test items without PK key are excluded."""
        items = [
            {
                "SK": "CONFLUENCE_CONNECTION#conn_001",
                "connection_id": "conn_001",
                "selected_spaces": [{"key": "ENG", "name": "Engineering"}],
            },
        ]
        _setup_paginator([items])

        result = get_all_confluence_connections()
        assert result == []

    def test_get_connections_uses_correct_table(self):
        """Test the function queries the correct DynamoDB table."""
        mock_table, mock_paginator = _setup_paginator([[]])

        get_all_confluence_connections()
        mock_dynamodb_resource.Table.assert_called_once_with("test-workspaces")

    def test_get_connections_uses_correct_filter(self):
        """Test the scan uses the correct filter expression for Confluence connections."""
        mock_table, mock_paginator = _setup_paginator([[]])

        get_all_confluence_connections()
        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs["FilterExpression"] == "begins_with(SK, :sk_prefix)"
        assert call_kwargs["ExpressionAttributeValues"] == {
            ":sk_prefix": "CONFLUENCE_CONNECTION#"
        }

    def test_get_connections_page_with_no_items_key(self):
        """Test handles pages that have no Items key gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {},  # Page with no Items key
        ]

        result = get_all_confluence_connections()
        assert result == []


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
        call_kwargs = mock_sqs_client.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/confluence-sync"
        msg = json.loads(call_kwargs["MessageBody"])
        assert msg["workspace_id"] == "ws_test"
        assert msg["connection_id"] == "conn_test"
        assert msg["triggered_by"] == "scheduler"

    def test_queue_sync_job_missing_workspace_id(self):
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

    def test_queue_sync_job_none_workspace_id(self):
        """Test returns False when workspace_id is None."""
        result = queue_sync_job({"workspace_id": None, "connection_id": "conn_test"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_none_connection_id(self):
        """Test returns False when connection_id is None."""
        result = queue_sync_job({"workspace_id": "ws_test", "connection_id": None})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_empty_string_workspace_id(self):
        """Test returns False when workspace_id is empty string."""
        result = queue_sync_job({"workspace_id": "", "connection_id": "conn_test"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_empty_string_connection_id(self):
        """Test returns False when connection_id is empty string."""
        result = queue_sync_job({"workspace_id": "ws_test", "connection_id": ""})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_sqs_error(self):
        """Test returns False on SQS ClientError."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue", "Message": "Queue does not exist"}},
            "SendMessage",
        )

        result = queue_sync_job({
            "workspace_id": "ws_test",
            "connection_id": "conn_test",
        })
        assert result is False

    def test_queue_sync_job_message_format(self):
        """Test the SQS message body is valid JSON with expected structure."""
        connection = {
            "workspace_id": "ws_abc",
            "connection_id": "conn_xyz",
            "extra_field": "should_not_appear",
        }

        queue_sync_job(connection)

        call_kwargs = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_kwargs["MessageBody"])
        assert set(msg.keys()) == {"workspace_id", "connection_id", "triggered_by"}
        assert msg["workspace_id"] == "ws_abc"
        assert msg["connection_id"] == "conn_xyz"
        assert msg["triggered_by"] == "scheduler"


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
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
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_passes_each_connection_to_queue(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler passes each connection individually to queue_sync_job."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        lambda_handler({}, None)

        calls = mock_queue.call_args_list
        assert calls[0][0][0] == sample_connections[0]
        assert calls[1][0][0] == sample_connections[1]

    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_no_connections(self, mock_get_conns):
        """Test handler with no active connections returns 200."""
        mock_get_conns.return_value = []

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 0
        assert "No active connections" in body["message"]

    def test_handler_no_queue_configured(self):
        """Test handler returns 500 when CONFLUENCE_SYNC_QUEUE is not set."""
        handler_module.CONFLUENCE_SYNC_QUEUE = None

        result = lambda_handler({}, None)
        assert result["statusCode"] == 500
        assert "Queue not configured" in result["body"]

    def test_handler_empty_string_queue(self):
        """Test handler returns 500 when CONFLUENCE_SYNC_QUEUE is empty string."""
        handler_module.CONFLUENCE_SYNC_QUEUE = ""

        result = lambda_handler({}, None)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_partial_queue_failure(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler correctly counts partial successes."""
        mock_get_conns.return_value = sample_connections
        mock_queue.side_effect = [True, False]  # First succeeds, second fails

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 1

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_all_queue_failures(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler when all queue jobs fail."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = False

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 0

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_single_connection(self, mock_get_conns, mock_queue):
        """Test handler with a single connection."""
        mock_get_conns.return_value = [
            {
                "workspace_id": "ws_only",
                "connection_id": "conn_only",
                "selected_spaces": [{"key": "MAIN"}],
            }
        ]
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 1
        assert body["jobs_queued"] == 1
        assert mock_queue.call_count == 1

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_many_connections(self, mock_get_conns, mock_queue, three_connections):
        """Test handler with multiple connections from different workspaces."""
        mock_get_conns.return_value = three_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 3
        assert body["jobs_queued"] == 3

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_response_body_is_valid_json(self, mock_get_conns, mock_queue, sample_connections):
        """Test handler response body is always valid JSON."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert "message" in body
        assert "connections_found" in body
        assert "jobs_queued" in body

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_eventbridge_event(self, mock_get_conns, mock_queue):
        """Test handler works with EventBridge scheduled event payload."""
        mock_get_conns.return_value = []

        eventbridge_event = {
            "version": "0",
            "id": "12345678-1234-1234-1234-123456789012",
            "detail-type": "Scheduled Event",
            "source": "aws.events",
            "time": "2026-02-25T12:00:00Z",
            "region": "us-east-1",
            "resources": ["arn:aws:events:us-east-1:123456789012:rule/confluence-sync"],
            "detail": {},
        }

        result = lambda_handler(eventbridge_event, None)
        assert result["statusCode"] == 200
        mock_get_conns.assert_called_once()

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_no_connections_message(self, mock_get_conns, mock_queue):
        """Test the specific message when no connections are found."""
        mock_get_conns.return_value = []

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["message"] == "No active connections to sync"
        mock_queue.assert_not_called()

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_confluence_connections')
    def test_handler_success_message(self, mock_get_conns, mock_queue, sample_connections):
        """Test the specific message when sync scheduling completes."""
        mock_get_conns.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["message"] == "Sync scheduling completed"
