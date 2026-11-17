# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for database-scheduler Lambda handler."""

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
    if 'database_scheduler_handler' in sys.modules:
        del sys.modules['database_scheduler_handler']

    scheduler_dir = os.path.join(os.path.dirname(__file__), '../../database-scheduler')
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)

    handler_path = os.path.join(scheduler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("database_scheduler_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['database_scheduler_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    handler_module.dynamodb = mock_dynamodb_resource
    handler_module.sqs = mock_sqs_client

    return handler_module


handler_module = _load_handler_module()

lambda_handler = handler_module.lambda_handler
get_all_database_connections = handler_module.get_all_database_connections
queue_sync_job = handler_module.queue_sync_job


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    handler_module.DATABASE_SYNC_QUEUE = "https://sqs.example.com/db-sync"
    handler_module.WORKSPACES_TABLE = "test-workspaces"
    yield


@pytest.fixture
def sample_connections():
    return [
        {
            "PK": "WS#ws_001",
            "SK": "DATABASE_CONNECTION#conn_001",
            "connection_id": "conn_001",
            "workspace_id": "ws_001",
            "engine": "postgres",
            "scope_config": {
                "introspection": {"include": ["public.users"], "exclude": []},
                "sampling": {"enabled": False, "include": [], "exclude": []},
                "profiling": {"enabled": True},
            },
        },
        {
            "PK": "WS#ws_002",
            "SK": "DATABASE_CONNECTION#conn_002",
            "connection_id": "conn_002",
            "workspace_id": "ws_002",
            "engine": "mysql",
            "scope_config": {
                "introspection": {"include": ["app.orders"], "exclude": []},
                "sampling": {"enabled": False, "include": [], "exclude": []},
                "profiling": {"enabled": True},
            },
        },
    ]


# ============================================================================
# Get All Database Connections
# ============================================================================

class TestGetAllDatabaseConnections:

    def test_get_connections_success(self, sample_connections):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": sample_connections}]

        result = get_all_database_connections()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws_001"

    def test_get_connections_filters_empty_scope(self):
        """Connections with empty scope_config.introspection.include are
        excluded - admin hasn't picked schemas yet, scheduler shouldn't
        trigger sync on them."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "WS#ws_001",
                    "SK": "DATABASE_CONNECTION#conn_001",
                    "connection_id": "conn_001",
                    "scope_config": {
                        "introspection": {"include": ["public.users"], "exclude": []},
                    },
                },
                {
                    "PK": "WS#ws_002",
                    "SK": "DATABASE_CONNECTION#conn_002",
                    "connection_id": "conn_002",
                    "scope_config": {
                        "introspection": {"include": [], "exclude": []},
                    },
                },
                {
                    "PK": "WS#ws_003",
                    "SK": "DATABASE_CONNECTION#conn_003",
                    "connection_id": "conn_003",
                    # No scope_config at all - newly created, hasn't been configured
                },
            ]}
        ]

        result = get_all_database_connections()
        assert len(result) == 1
        assert result[0]["connection_id"] == "conn_001"

    def test_get_connections_empty(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [{"Items": []}]

        result = get_all_database_connections()
        assert result == []

    def test_get_connections_dynamodb_error(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "Scan"
        )

        result = get_all_database_connections()
        assert result == []

    def test_extracts_workspace_id_from_pk(self):
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [{
                "PK": "WS#my_ws",
                "SK": "DATABASE_CONNECTION#cx",
                "connection_id": "cx",
                "scope_config": {
                    "introspection": {"include": ["public.foo"], "exclude": []},
                },
            }]}
        ]

        result = get_all_database_connections()
        assert result[0]["workspace_id"] == "my_ws"

    def test_skips_non_ws_pk(self):
        """SK begins_with DATABASE_CONNECTION# might match a non-WS#
        PK if anyone ever adds one. Defensive filter."""
        mock_table = MagicMock()
        mock_dynamodb_resource.Table.return_value = mock_table
        mock_paginator = MagicMock()
        mock_table.meta.client.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Items": [
                {
                    "PK": "OTHER#x",  # not WS#
                    "SK": "DATABASE_CONNECTION#c1",
                    "scope_config": {"introspection": {"include": ["a.b"]}},
                },
                {
                    "PK": "WS#real",
                    "SK": "DATABASE_CONNECTION#c2",
                    "connection_id": "c2",
                    "scope_config": {"introspection": {"include": ["a.b"]}},
                },
            ]}
        ]

        result = get_all_database_connections()
        assert len(result) == 1
        assert result[0]["connection_id"] == "c2"


# ============================================================================
# Queue Sync Job
# ============================================================================

class TestQueueSyncJob:

    def test_success(self):
        result = queue_sync_job({"workspace_id": "ws1", "connection_id": "c1"})
        assert result is True
        msg = json.loads(mock_sqs_client.send_message.call_args[1]["MessageBody"])
        assert msg["triggered_by"] == "scheduler"
        assert msg["workspace_id"] == "ws1"
        assert msg["connection_id"] == "c1"

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
    @patch.object(handler_module, 'get_all_database_connections')
    def test_handler_queues_all(self, mock_get, mock_queue, sample_connections):
        mock_get.return_value = sample_connections
        mock_queue.return_value = True

        result = lambda_handler({}, None)
        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["connections_found"] == 2
        assert body["jobs_queued"] == 2

    @patch.object(handler_module, 'get_all_database_connections')
    def test_handler_no_connections(self, mock_get):
        mock_get.return_value = []
        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["connections_found"] == 0

    def test_handler_no_queue(self):
        handler_module.DATABASE_SYNC_QUEUE = None
        result = lambda_handler({}, None)
        assert result["statusCode"] == 500

    @patch.object(handler_module, 'queue_sync_job')
    @patch.object(handler_module, 'get_all_database_connections')
    def test_handler_partial_failure(self, mock_get, mock_queue, sample_connections):
        mock_get.return_value = sample_connections
        mock_queue.side_effect = [True, False]

        result = lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["jobs_queued"] == 1
