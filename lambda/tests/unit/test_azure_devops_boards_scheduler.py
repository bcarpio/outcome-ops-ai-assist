# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the azure-devops-boards-scheduler Lambda."""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


mock_dynamodb_resource = MagicMock()
mock_sqs_client = MagicMock()


def _load_module(mod_name, filename, directory):
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)
    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


_HANDLER_LOAD_ENV = {
    "WORKSPACES_TABLE": "test-workspaces",
    "AZURE_DEVOPS_BOARDS_SYNC_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/boards-sync",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

handler = _load_module(
    "azdo_boards_scheduler",
    "handler.py",
    "../../azure-devops-boards-scheduler",
)
handler.dynamodb = mock_dynamodb_resource
handler.sqs = mock_sqs_client

for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_dynamodb_resource.reset_mock()
    mock_sqs_client.reset_mock()
    mock_sqs_client.send_message.side_effect = None
    yield


@pytest.fixture
def mock_table():
    table = MagicMock()
    mock_dynamodb_resource.Table.return_value = table
    return table


def _project_item(slug="example-org/widgets", connection_id="c1", org="example-org",
                  project="widgets", workspace_id="ws1"):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"AZDO_BOARDS_PROJECT#{slug}",
        "workspace_id": workspace_id,
        "slug": slug,
        "connection_id": connection_id,
        "org": org,
        "project": project,
        "enabled": True,
    }


class TestGetAllEnabledProjects:

    def test_single_page(self, mock_table):
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Items": [_project_item(), _project_item(slug="o/p2")]},
        ])
        mock_table.meta.client.get_paginator.return_value = paginator

        result = handler.get_all_enabled_projects()
        assert len(result) == 2
        assert result[0]["workspace_id"] == "ws1"

    def test_multiple_pages_walked(self, mock_table):
        """Regression guard for feedback_enterprise_scale_pagination:
        every page's items must appear in the result."""
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Items": [_project_item(slug="a/1")]},
            {"Items": [_project_item(slug="a/2")]},
            {"Items": [_project_item(slug="a/3")]},
        ])
        mock_table.meta.client.get_paginator.return_value = paginator

        result = handler.get_all_enabled_projects()
        assert [p["slug"] for p in result] == ["a/1", "a/2", "a/3"]

    def test_filter_expression_scans_only_enabled_boards_projects(self, mock_table):
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Items": []}])
        mock_table.meta.client.get_paginator.return_value = paginator

        handler.get_all_enabled_projects()

        kwargs = paginator.paginate.call_args.kwargs
        assert "begins_with(SK, :sk_prefix)" in kwargs["FilterExpression"]
        assert "enabled = :true" in kwargs["FilterExpression"]
        assert kwargs["ExpressionAttributeValues"][":sk_prefix"] == (
            "AZDO_BOARDS_PROJECT#"
        )
        assert kwargs["ExpressionAttributeValues"][":true"] is True


class TestQueueSyncJob:

    def test_message_carries_required_fields(self):
        assert handler.queue_sync_job(_project_item()) is True
        call = mock_sqs_client.send_message.call_args
        body = json.loads(call.kwargs["MessageBody"])
        assert body["workspace_id"] == "ws1"
        assert body["connection_id"] == "c1"
        assert body["slug"] == "example-org/widgets"
        assert body["org"] == "example-org"
        assert body["project"] == "widgets"
        assert body["triggered_by"] == "scheduler"

    def test_malformed_row_returns_false_and_does_not_send(self):
        result = handler.queue_sync_job({"workspace_id": "ws1"})
        assert result is False
        mock_sqs_client.send_message.assert_not_called()


class TestLambdaHandler:

    def test_active_region_scans_and_enqueues(self, mock_table):
        paginator = MagicMock()
        paginator.paginate.return_value = iter([
            {"Items": [_project_item(), _project_item(slug="o/p2")]},
        ])
        mock_table.meta.client.get_paginator.return_value = paginator

        with patch("shared.active_region.is_active_region", return_value=True):
            result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["projects_found"] == 2
        assert body["jobs_queued"] == 2
        assert mock_sqs_client.send_message.call_count == 2

    def test_passive_region_short_circuits(self, mock_table):
        with patch("shared.active_region.is_active_region", return_value=False):
            result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert "passive-region-skip" in result["body"]
        mock_sqs_client.send_message.assert_not_called()

    def test_no_projects_returns_early(self, mock_table):
        paginator = MagicMock()
        paginator.paginate.return_value = iter([{"Items": []}])
        mock_table.meta.client.get_paginator.return_value = paginator

        with patch("shared.active_region.is_active_region", return_value=True):
            result = handler.lambda_handler({}, None)

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["projects_found"] == 0
        mock_sqs_client.send_message.assert_not_called()

    def test_missing_queue_env_returns_500(self, mock_table, monkeypatch):
        monkeypatch.setattr(handler, "AZURE_DEVOPS_BOARDS_SYNC_QUEUE", "")
        with patch("shared.active_region.is_active_region", return_value=True):
            result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 500
