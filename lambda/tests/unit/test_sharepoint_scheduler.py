# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for sharepoint-scheduler Lambda handler."""

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
    if 'sharepoint_scheduler_handler' in sys.modules:
        del sys.modules['sharepoint_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../sharepoint-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("sharepoint_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['sharepoint_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

lambda_handler = handler_module.lambda_handler
get_all_sharepoint_connections = handler_module.get_all_sharepoint_connections
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    handler_module.SHAREPOINT_SYNC_QUEUE = "https://sqs.example.com/sp-sync"
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    yield


@pytest.fixture
def sample_connections():
    return [
        {
            "PK": "WS#ws_001",
            "SK": "SHAREPOINT_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "selected_sites": [{"site_id": "s1"}],
        },
        {
            "PK": "WS#ws_002",
            "SK": "SHAREPOINT_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "selected_sites": [{"site_id": "s2"}],
        },
    ]


# ============================================================================
# Get All SharePoint Connections
# ============================================================================

class TestGetAllSharePointConnections:

    def test_get_connections_success(self, sample_connections):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": sample_connections}]

        result = get_all_sharepoint_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"

    def test_get_connections_filters_no_sites(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "SHAREPOINT_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    "selected_sites": [{"site_id": "s1"}],
                },
                {
                    "PK": "WS#ws_002",
                    "SK": "SHAREPOINT_CONNECTION#conn_002",
                    "connection_id": "conn_002",
                    "selected_sites": [],
                },
            ]}
        ]

        result = get_all_sharepoint_connections()
        assert len(result) == 1

    def test_get_connections_empty(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        result = get_all_sharepoint_connections()
        assert result == []

    def test_get_connections_dynamodb_error(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Scan"
        )

        result = get_all_sharepoint_connections()
        assert result == []

    def test_extracts_workspace_id(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [{
                "PK": "WS#my_ws",
                "SK": "SHAREPOINT_CONNECTION#cx",
                "connection_id": "cx",
                "selected_sites": [{"site_id": "s1"}],
            }]}
        ]

        result = get_all_sharepoint_connections()
        assert result[0]["workspace_id"] == "my_ws"


# ============================================================================
# Queue Sync Job
# ============================================================================

class TestQueueSyncJob:

    def test_success(self):
        result = queue_sync_job({"workspace_id": "ws1", "connection_id": "c1"})
        assert result is True
        msg = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert msg["triggered_by"] == "scheduler"

    def test_missing_workspace(self):
        assert queue_sync_job({"connection_id": "c1"}) is False

    def test_missing_connection_id(self):
        assert queue_sync_job({"workspace_id": "ws1"}) is False

    def test_sqs_error(self):
        mock_sqs_client.send_message.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "SendMessage"
        )
        assert queue_sync_job({"workspace_id": "ws1", "connection_id": "c1"}) is False


# ============================================================================
# Lambda Handler
# ============================================================================

class TestLambdaHandler:

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_sharepoint_connections')
    def test_handler_queues_all(self, mock_get, mock_queue, sample_connections):
        mock_get.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 2

    @patch.object(handler_module, 'get_all_sharepoint_connections')
    def test_handler_no_connections(self, mock_get):
        mock_get.return_value = []
        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["connections_found"] == 0

    def test_handler_no_queue(self):
        handler_module.SHAREPOINT_SYNC_QUEUE = None
        result = lambda_handler({}, None)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_sharepoint_connections')
    def test_handler_partial_failure(self, mock_get, mock_queue, sample_connections):
        mock_get.return_value = sample_connections
        mock_queue.side_effect = [True, False]

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["jobs_queued"] == 1
