"""Unit tests for github-scheduler Lambda handler."""

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
        if 'boto3.dynamodb' not in sys.modules:
            sys.modules['boto3.dynamodb'] = MagicMock()
        if 'boto3.dynamodb.conditions' not in sys.modules:
            mock_conditions = MagicMock()
            sys.modules['boto3.dynamodb.conditions'] = mock_conditions


def _load_handler_module():
    """Load or reload the github-scheduler handler module."""
    _ensure_boto3_submodules()

    if 'github_scheduler_handler' in sys.modules:
        del sys.modules['github_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../github-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("github_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['github_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace cached boto3 clients with mocks
    handler_module._dynamodb = mock_dynamodb_resource
    handler_module._sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

# Import functions
lambda_handler = handler_module.lambda_handler
scan_github_repos_for_sync = handler_module.scan_github_repos_for_sync
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks between tests."""
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    handler_module.GITHUB_SYNC_QUEUE = "https://sqs.example.com/github-sync"
    yield


@pytest.fixture
def sample_repos():
    """Sample GitHub repo items from DynamoDB scan."""
    return [
        {
            "PK": "WS#ws_001",
            "SK": "GITHUB_REPO#repo_001",
            "repo_name": "org/repo-alpha",
            "installation_id": "inst_111",
            "include_code_maps": True,
            "last_sync_at": "2026-02-20T10:00:00Z",
            "last_sync_status": "success",
        },
        {
            "PK": "WS#ws_002",
            "SK": "GITHUB_REPO#repo_002",
            "repo_name": "org/repo-beta",
            "installation_id": "inst_222",
            "include_code_maps": True,
            "last_sync_at": None,
            "last_sync_status": None,
        },
    ]


@pytest.fixture
def eventbridge_event():
    """Sample EventBridge scheduled event."""
    return {
        "source": "aws.events",
        "detail-type": "Scheduled Event",
        "detail": {},
    }


# ============================================================================
# Scan GitHub Repos For Sync
# ============================================================================

class TestScanGithubReposForSync:
    """Test scanning DynamoDB for GitHub repos that need syncing."""

    def test_scan_repos_success(self, sample_repos):
        """Test scanning returns repos with code maps enabled."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": sample_repos}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[0]["repo_name"] == "org/repo-alpha"
        assert result[0]["installation_id"] == "inst_111"
        assert result[0]["last_sync_at"] == "2026-02-20T10:00:00Z"
        assert result[0]["last_sync_status"] == "success"

    def test_scan_repos_second_item_fields(self, sample_repos):
        """Test second item is parsed correctly with null fields."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": sample_repos}
        ]

        result = scan_github_repos_for_sync()
        assert result[1]["workspace_id"] == "ws_002"
        assert result[1]["repo_name"] == "org/repo-beta"
        assert result[1]["installation_id"] == "inst_222"
        assert result[1]["last_sync_at"] is None
        assert result[1]["last_sync_status"] is None

    def test_scan_repos_extracts_workspace_id_from_pk(self):
        """Test workspace_id is correctly extracted from PK (WS# prefix)."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#my_long_workspace_id_123",
                    "SK": "GITHUB_REPO#repo_x",
                    "repo_name": "org/repo-x",
                    "installation_id": "inst_x",
                    "include_code_maps": True,
                },
            ]}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 1
        assert result[0]["workspace_id"] == "my_long_workspace_id_123"

    def test_scan_repos_skips_items_without_ws_prefix(self):
        """Test items without WS# PK prefix are skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "INVALID_PREFIX#ws_001",
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                    "include_code_maps": True,
                },
            ]}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 0

    def test_scan_repos_skips_items_with_empty_pk(self):
        """Test items with empty PK are skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "",
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                },
            ]}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 0

    def test_scan_repos_skips_items_missing_pk(self):
        """Test items with missing PK key are skipped."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                },
            ]}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 0

    def test_scan_repos_empty_results(self):
        """Test returns empty list when no repos match."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        result = scan_github_repos_for_sync()
        assert result == []

    def test_scan_repos_multiple_pages(self):
        """Test handles paginated results across multiple pages."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                    "include_code_maps": True,
                },
            ]},
            {"Items": [
                {
                    "PK": "WS#ws_002",
                    "SK": "GITHUB_REPO#repo_002",
                    "repo_name": "org/repo-beta",
                    "installation_id": "inst_222",
                    "include_code_maps": True,
                },
            ]},
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"
        assert result[1]["workspace_id"] == "ws_002"

    def test_scan_repos_page_with_no_items_key(self):
        """Test handles pages where Items key is missing."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {},  # Page without Items key
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                },
            ]},
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 1

    def test_scan_repos_dynamodb_client_error(self):
        """Test handles DynamoDB ClientError gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal Server Error"}}, "Scan"
        )

        result = scan_github_repos_for_sync()
        assert result == []

    def test_scan_repos_access_denied_error(self):
        """Test handles AccessDeniedException gracefully."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "Access denied"}},
            "Scan",
        )

        result = scan_github_repos_for_sync()
        assert result == []

    def test_scan_repos_workspaces_table_not_configured(self):
        """Test returns empty list when WORKSPACES_TABLE is not set."""
        handler_module.WORKSPACES_TABLE = None

        result = scan_github_repos_for_sync()
        assert result == []
        # Should not attempt DynamoDB calls
        mock_dynamodb_resource.Table.assert_not_called()

    def test_scan_repos_workspaces_table_empty_string(self):
        """Test returns empty list when WORKSPACES_TABLE is empty string."""
        handler_module.WORKSPACES_TABLE = ""

        result = scan_github_repos_for_sync()
        assert result == []
        mock_dynamodb_resource.Table.assert_not_called()

    def test_scan_repos_uses_correct_table_name(self):
        """Test scan uses the configured WORKSPACES_TABLE name."""
        handler_module.WORKSPACES_TABLE = "my-custom-table"

        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        scan_github_repos_for_sync()
        mock_dynamodb_resource.Table.assert_called_once_with("my-custom-table")

    def test_scan_repos_paginate_uses_correct_table_name(self):
        """Test paginator passes the correct TableName parameter."""
        handler_module.WORKSPACES_TABLE = "my-custom-table"

        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        scan_github_repos_for_sync()

        call_kwargs = mock_paginator.paginate.call_args[1]
        assert call_kwargs["TableName"] == "my-custom-table"

    def test_scan_repos_missing_optional_fields(self):
        """Test handles items missing optional fields like last_sync_at."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table

        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "GITHUB_REPO#repo_001",
                    "repo_name": "org/repo-alpha",
                    "installation_id": "inst_111",
                    # No last_sync_at or last_sync_status
                },
            ]}
        ]

        result = scan_github_repos_for_sync()
        assert len(result) == 1
        assert result[0]["last_sync_at"] is None
        assert result[0]["last_sync_status"] is None


# ============================================================================
# Queue Sync Job
# ============================================================================

class TestQueueSyncJob:
    """Test queuing sync jobs to the GitHub sync SQS queue."""

    def test_queue_sync_job_success(self):
        """Test queuing a sync job sends correct SQS message."""
        result = queue_sync_job("ws_001", "org/repo-alpha", "inst_111")
        assert result is True

        mock_sqs_client.send_message.assert_called_once()
        call_kwargs = mock_sqs_client.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.example.com/github-sync"

        msg = json.loads(call_kwargs["MessageBody"])
        assert msg["workspace_id"] == "ws_001"
        assert msg["repo_name"] == "org/repo-alpha"
        assert msg["installation_id"] == "inst_111"
        assert msg["trigger"] == "scheduled"

    def test_queue_sync_job_message_format(self):
        """Test message body contains exactly the expected keys."""
        queue_sync_job("ws_test", "org/test-repo", "inst_999")

        call_kwargs = mock_sqs_client.send_message.call_args[1]
        msg = json.loads(call_kwargs["MessageBody"])
        assert set(msg.keys()) == {
            "workspace_id", "repo_name", "installation_id", "trigger"
        }

    def test_queue_sync_job_queue_not_configured(self):
        """Test returns False when GITHUB_SYNC_QUEUE is not set."""
        handler_module.GITHUB_SYNC_QUEUE = None

        result = queue_sync_job("ws_001", "org/repo-alpha", "inst_111")
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_queue_empty_string(self):
        """Test returns False when GITHUB_SYNC_QUEUE is empty string."""
        handler_module.GITHUB_SYNC_QUEUE = ""

        result = queue_sync_job("ws_001", "org/repo-alpha", "inst_111")
        assert result is False
        mock_sqs_client.send_message.assert_not_called()

    def test_queue_sync_job_sqs_client_error(self):
        """Test returns False on SQS ClientError."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "AWS.SimpleQueueService.NonExistentQueue",
                       "Message": "Queue does not exist"}},
            "SendMessage",
        )

        result = queue_sync_job("ws_001", "org/repo-alpha", "inst_111")
        assert result is False

    def test_queue_sync_job_sqs_throttle_error(self):
        """Test returns False on SQS throttling error."""
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "ThrottlingException",
                       "Message": "Rate exceeded"}},
            "SendMessage",
        )

        result = queue_sync_job("ws_001", "org/repo-alpha", "inst_111")
        assert result is False

    def test_queue_sync_job_uses_correct_queue_url(self):
        """Test send_message uses the configured GITHUB_SYNC_QUEUE URL."""
        handler_module.GITHUB_SYNC_QUEUE = "https://sqs.us-east-1.amazonaws.com/123456/my-queue"

        queue_sync_job("ws_001", "org/repo-alpha", "inst_111")

        call_kwargs = mock_sqs_client.send_message.call_args[1]
        assert call_kwargs["QueueUrl"] == "https://sqs.us-east-1.amazonaws.com/123456/my-queue"


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:
    """Test main Lambda handler."""

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_queues_all_repos(self, mock_scan, mock_queue, sample_repos):
        """Test handler queues sync for all scanned repos."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_001",
                "repo_name": "org/repo-alpha",
                "installation_id": "inst_111",
                "last_sync_at": "2026-02-20T10:00:00Z",
                "last_sync_status": "success",
            },
            {
                "workspace_id": "ws_002",
                "repo_name": "org/repo-beta",
                "installation_id": "inst_222",
                "last_sync_at": None,
                "last_sync_status": None,
            },
        ]
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["repos_scanned"] == 2
        assert body["repos_queued"] == 2
        assert mock_queue.call_count == 2

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_passes_correct_args_to_queue(self, mock_scan, mock_queue):
        """Test handler passes workspace_id, repo_name, installation_id to queue."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_abc",
                "repo_name": "org/my-repo",
                "installation_id": "inst_xyz",
                "last_sync_at": None,
                "last_sync_status": None,
            },
        ]
        mock_queue.return_value = True

        lambda_handler({}, None)
        mock_queue.assert_called_once_with("ws_abc", "org/my-repo", "inst_xyz")

    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_no_repos_found(self, mock_scan):
        """Test handler with no repos to sync."""
        mock_scan.return_value = []

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["repos_scanned"] == 0
        assert body["repos_queued"] == 0

    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_no_repos_response_excludes_failed_key(self, mock_scan):
        """Test no-repos response body does not contain repos_failed."""
        mock_scan.return_value = []

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert "repos_failed" not in body

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_partial_queue_failure(self, mock_scan, mock_queue):
        """Test handler counts partial successes and failures."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_001",
                "repo_name": "org/repo-alpha",
                "installation_id": "inst_111",
                "last_sync_at": None,
                "last_sync_status": None,
            },
            {
                "workspace_id": "ws_002",
                "repo_name": "org/repo-beta",
                "installation_id": "inst_222",
                "last_sync_at": None,
                "last_sync_status": None,
            },
            {
                "workspace_id": "ws_003",
                "repo_name": "org/repo-gamma",
                "installation_id": "inst_333",
                "last_sync_at": None,
                "last_sync_status": None,
            },
        ]
        mock_queue.side_effect = [True, False, True]

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["repos_scanned"] == 3
        assert body["repos_queued"] == 2
        assert body["repos_failed"] == 1

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_all_queue_failures(self, mock_scan, mock_queue):
        """Test handler when all queue attempts fail."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_001",
                "repo_name": "org/repo-alpha",
                "installation_id": "inst_111",
                "last_sync_at": None,
                "last_sync_status": None,
            },
        ]
        mock_queue.return_value = False

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200

        body = json.loads(result["body"])
        assert body["repos_scanned"] == 1
        assert body["repos_queued"] == 0
        assert body["repos_failed"] == 1

    def test_handler_logs_event_source(self, eventbridge_event):
        """Test handler processes EventBridge event metadata."""
        with patch.object(handler_module, 'scan_github_repos_for_sync', return_value=[]):
            result = lambda_handler(eventbridge_event, None)
            assert result["statusCode"] == 200

    def test_handler_handles_missing_event_fields(self):
        """Test handler handles events without source or detail-type."""
        with patch.object(handler_module, 'scan_github_repos_for_sync', return_value=[]):
            result = lambda_handler({}, None)
            assert result["statusCode"] == 200

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_response_is_valid_json(self, mock_scan, mock_queue):
        """Test handler response body is valid JSON."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_001",
                "repo_name": "org/repo-alpha",
                "installation_id": "inst_111",
                "last_sync_at": None,
                "last_sync_status": None,
            },
        ]
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert "statusCode" in result
        assert "body" in result
        # Ensure body is valid JSON
        body = json.loads(result["body"])
        assert isinstance(body, dict)

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'scan_github_repos_for_sync')
    def test_handler_single_repo_success(self, mock_scan, mock_queue):
        """Test handler with exactly one repo that queues successfully."""
        mock_scan.return_value = [
            {
                "workspace_id": "ws_solo",
                "repo_name": "org/solo-repo",
                "installation_id": "inst_solo",
                "last_sync_at": "2026-02-25T00:00:00Z",
                "last_sync_status": "success",
            },
        ]
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["repos_scanned"] == 1
        assert body["repos_queued"] == 1
        assert body["repos_failed"] == 0


# ============================================================================
# Client Caching
# ============================================================================

class TestClientCaching:
    """Test that boto3 client caching works correctly."""

    def test_get_dynamodb_returns_cached_client(self):
        """Test _get_dynamodb returns the cached resource."""
        result = handler_module._get_dynamodb()
        assert result is mock_dynamodb_resource

    def test_get_sqs_returns_cached_client(self):
        """Test _get_sqs returns the cached client."""
        result = handler_module._get_sqs()
        assert result is mock_sqs_client

    def test_get_dynamodb_creates_new_when_none(self):
        """Test _get_dynamodb creates new resource when cache is None."""
        original = handler_module._dynamodb
        handler_module._dynamodb = None

        with patch("github_scheduler_handler.boto3") as mock_boto3:
            mock_resource = MagicMock()
            mock_boto3.resource.return_value = mock_resource

            result = handler_module._get_dynamodb()
            mock_boto3.resource.assert_called_once_with("dynamodb")
            assert result is mock_resource

        # Restore
        handler_module._dynamodb = original

    def test_get_sqs_creates_new_when_none(self):
        """Test _get_sqs creates new client when cache is None."""
        original = handler_module._sqs
        handler_module._sqs = None

        with patch("github_scheduler_handler.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client

            result = handler_module._get_sqs()
            mock_boto3.client.assert_called_once_with("sqs")
            assert result is mock_client

        # Restore
        handler_module._sqs = original
