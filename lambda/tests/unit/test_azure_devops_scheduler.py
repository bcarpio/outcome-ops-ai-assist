# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for azure-devops-scheduler Lambda handler.

Tests focus on what the chunk plan calls out:

1. ``is_active_region()`` short-circuit -- passive ticks must NOT
   scan DDB or send SQS messages.
2. Scan pagination -- a workspace with rows that span multiple DDB
   scan pages (1MB boundary) must enqueue ALL repos, not just the
   first page. This is the regression test for
   ``feedback_enterprise_scale_pagination``.
3. ``enabled=false`` repos are skipped.
4. Malformed rows (missing slug or connection_id) are skipped, not
   raised.
5. The enqueued SQS message body has the right shape so the 
   sync worker can consume it without surprises.
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------


def _load_handler():
    if "azdo_scheduler_handler" in sys.modules:
        del sys.modules["azdo_scheduler_handler"]
    scheduler_dir = os.path.join(os.path.dirname(__file__), "../../azure-devops-scheduler")
    if scheduler_dir not in sys.path:
        sys.path.insert(0, scheduler_dir)
    handler_path = os.path.join(scheduler_dir, "handler.py")
    spec = importlib.util.spec_from_file_location(
        "azdo_scheduler_handler", handler_path,
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["azdo_scheduler_handler"] = module
    spec.loader.exec_module(module)
    return module


# Set env JUST for the handler's module-load + restore after so we
# don't pollute other test modules' handlers (see the same pattern in
# test_azure_devops_integration.py for the failure mode this avoids).
_HANDLER_LOAD_ENV = {
    "WORKSPACES_TABLE": "test-workspaces",
    "AZURE_DEVOPS_SYNC_QUEUE": "https://sqs.us-west-2.amazonaws.com/123/azure-devops-sync",
}
_orig_env = {k: os.environ.get(k) for k in _HANDLER_LOAD_ENV}
os.environ.update(_HANDLER_LOAD_ENV)

handler = _load_handler()

for _k, _v in _orig_env.items():
    if _v is None:
        os.environ.pop(_k, None)
    else:
        os.environ[_k] = _v


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _repo_row(workspace_id="w1", slug="o/p/r", enabled=True, connection_id="c1"):
    return {
        "PK": f"WS#{workspace_id}",
        "SK": f"AZDO_REPO#{slug}",
        "slug": slug,
        "org": slug.split("/")[0],
        "project": slug.split("/")[1],
        "repo": slug.split("/")[2],
        "connection_id": connection_id,
        "enabled": enabled,
    }


@pytest.fixture
def mock_dynamo_and_sqs():
    """Swap the handler's module-level boto3 clients for mocks."""
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    saved_ddb = handler.dynamodb
    saved_sqs = handler.sqs
    handler.dynamodb = resource
    sqs = MagicMock()
    handler.sqs = sqs
    yield table, sqs
    handler.dynamodb = saved_ddb
    handler.sqs = saved_sqs


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestActiveRegionShortCircuit:

    @patch("shared.active_region.is_active_region", return_value=False)
    def test_passive_region_returns_immediately(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        table, sqs = mock_dynamo_and_sqs
        result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert result["body"] == "passive-region-skip"
        # No DDB scan + no SQS work when passive.
        assert not table.scan.called
        assert not sqs.send_message.called

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_active_region_runs_scan(self, mock_active, mock_dynamo_and_sqs):
        table, sqs = mock_dynamo_and_sqs
        table.scan.return_value = {"Items": []}
        result = handler.lambda_handler({}, None)
        assert result["statusCode"] == 200
        assert table.scan.called


class TestEnabledFilter:

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_disabled_repos_are_skipped(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        table, sqs = mock_dynamo_and_sqs
        table.scan.return_value = {
            "Items": [
                _repo_row(slug="o/p/r1", enabled=True),
                _repo_row(slug="o/p/r2", enabled=False),
                _repo_row(slug="o/p/r3", enabled=True),
            ]
        }
        result = handler.lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["scanned"] == 3
        assert body["queued"] == 2
        assert body["skipped_disabled"] == 1
        # Only the enabled repos' slugs were enqueued.
        slugs = [
            json.loads(c.kwargs["MessageBody"])["slug"]
            for c in sqs.send_message.call_args_list
        ]
        assert slugs == ["o/p/r1", "o/p/r3"]

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_default_enabled_true_when_attr_missing(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        # Legacy rows may not have the ``enabled`` attribute; default
        # to True so they sync. Matches the github-scheduler precedent.
        table, sqs = mock_dynamo_and_sqs
        item = _repo_row()
        del item["enabled"]
        table.scan.return_value = {"Items": [item]}
        handler.lambda_handler({}, None)
        assert sqs.send_message.call_count == 1


class TestMalformedRows:

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_missing_slug_is_skipped_not_raised(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        table, sqs = mock_dynamo_and_sqs
        bad = _repo_row(slug="x/y/z")
        del bad["slug"]
        table.scan.return_value = {"Items": [bad, _repo_row(slug="o/p/good")]}
        result = handler.lambda_handler({}, None)
        # Scanned both; enqueued only the well-formed one.
        body = json.loads(result["body"])
        assert body["scanned"] == 2
        assert body["queued"] == 1

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_missing_connection_id_is_skipped(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        table, sqs = mock_dynamo_and_sqs
        bad = _repo_row()
        del bad["connection_id"]
        table.scan.return_value = {"Items": [bad]}
        result = handler.lambda_handler({}, None)
        body = json.loads(result["body"])
        assert body["queued"] == 0


class TestPagination:
    """The pagination regression guard. ``feedback_enterprise_scale_pagination``
    flags this as a class of bug we've already shipped once."""

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_traverses_all_scan_pages(self, mock_active, mock_dynamo_and_sqs):
        table, sqs = mock_dynamo_and_sqs
        # 3 pages of 500 items each = 1500 enabled repos.
        page1 = {
            "Items": [_repo_row(slug=f"o/p/r-p1-{i}") for i in range(500)],
            "LastEvaluatedKey": {"PK": "WS#w1", "SK": "AZDO_REPO#cursor1"},
        }
        page2 = {
            "Items": [_repo_row(slug=f"o/p/r-p2-{i}") for i in range(500)],
            "LastEvaluatedKey": {"PK": "WS#w1", "SK": "AZDO_REPO#cursor2"},
        }
        page3 = {
            "Items": [_repo_row(slug=f"o/p/r-p3-{i}") for i in range(500)],
            # No LastEvaluatedKey -- terminal page.
        }
        table.scan.side_effect = [page1, page2, page3]

        result = handler.lambda_handler({}, None)
        body = json.loads(result["body"])
        # All 1500 enqueued. NOT 500 (single page).
        assert body["scanned"] == 1500
        assert body["queued"] == 1500
        assert sqs.send_message.call_count == 1500

        # Cursor threading: page 2 + 3 calls carry ExclusiveStartKey.
        calls = table.scan.call_args_list
        assert "ExclusiveStartKey" not in calls[0].kwargs
        assert calls[1].kwargs["ExclusiveStartKey"]["SK"] == "AZDO_REPO#cursor1"
        assert calls[2].kwargs["ExclusiveStartKey"]["SK"] == "AZDO_REPO#cursor2"

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_terminal_page_with_no_lek_stops_loop(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        table, sqs = mock_dynamo_and_sqs
        table.scan.return_value = {
            "Items": [_repo_row()],
            # No LastEvaluatedKey
        }
        handler.lambda_handler({}, None)
        # Exactly one scan call.
        assert table.scan.call_count == 1


class TestQueueMessageShape:

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_message_carries_fields_the_sync_worker_will_consume(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        # The sync worker reads workspace_id, connection_id,
        # slug, org, project, repo, triggered_by, sync_id. Pin the
        # shape so a refactor to the scheduler can't silently drop
        # one of these and break the sync worker downstream.
        table, sqs = mock_dynamo_and_sqs
        table.scan.return_value = {"Items": [_repo_row(slug="example-org/example-project/example-repo")]}
        handler.lambda_handler({}, None)
        msg = json.loads(sqs.send_message.call_args.kwargs["MessageBody"])
        assert set(msg.keys()) >= {
            "workspace_id", "connection_id", "slug", "org",
            "project", "repo", "triggered_by", "sync_id",
        }
        assert msg["triggered_by"] == "scheduler"
        assert msg["slug"] == "example-org/example-project/example-repo"
        assert msg["org"] == "example-org"
        assert msg["project"] == "example-project"
        assert msg["repo"] == "example-repo"


class TestQueueConfig:

    @patch("shared.active_region.is_active_region", return_value=True)
    def test_missing_queue_url_returns_500(
        self, mock_active, mock_dynamo_and_sqs,
    ):
        with patch.object(handler, "AZURE_DEVOPS_SYNC_QUEUE", ""):
            result = handler.lambda_handler({}, None)
            assert result["statusCode"] == 500
