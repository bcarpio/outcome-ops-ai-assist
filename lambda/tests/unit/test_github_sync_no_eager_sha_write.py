# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Regression pin for the behavior change in github-sync.

Asserts that after a successful dispatch with NO consumer running,
the per-pipeline SHA columns (``last_code_map_sha``, ``last_docs_sha``,
``last_gkg_sha``) are unchanged from the pre-dispatch state. Only the
diagnostic columns (``last_attempted_sha``, ``last_dispatched_sha``)
move forward.

The plan-git-truth commits replaced github-sync's eager
``last_commit_sha = current_sha`` write at the end of the sync flow.
Without this regression test, a future "convenience" eager-write
could silently re-introduce the original bug — the per-pipeline
columns would advance before the work actually finishes, the audit
story breaks, and the HA-failover guarantee is gone.
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

# github-sync's own sibling files must resolve at import time.
_GITHUB_SYNC = _LAMBDA_ROOT / "github-sync"
if str(_GITHUB_SYNC) not in sys.path:
    sys.path.insert(0, str(_GITHUB_SYNC))


PENDING_TABLE = "test-no-eager-pending"
FAILED_TABLE = "test-no-eager-failed"
WORKSPACES_TABLE = "test-no-eager-workspaces"

WORKSPACE_ID = "ws_test"
REPO = "owner/repo"
OLD_SHA = "0" * 40
NEW_SHA = "9" * 40


@pytest.fixture(autouse=True)
def _aws_mocks(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("PENDING_WORK_TABLE", PENDING_TABLE)
    monkeypatch.setenv("FAILED_FILES_TABLE", FAILED_TABLE)
    monkeypatch.setenv("WORKSPACES_TABLE", WORKSPACES_TABLE)
    monkeypatch.setenv("FILE_INGESTION_QUEUE", "https://sqs.fake/fileq")
    monkeypatch.setenv("REPO_SUMMARIES_QUEUE", "https://sqs.fake/reposq")
    monkeypatch.setenv("DELETE_QUEUE", "https://sqs.fake/delq")
    monkeypatch.setenv("KB_BUCKET", "test-kb-bucket")
    monkeypatch.setenv("AUDIT_TABLE", "test-audit-table")
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_sha_truth_clients():
    from shared import sha_truth  # type: ignore

    sha_truth._reset_clients_for_tests()
    real = boto3.resource("dynamodb", region_name="us-west-2")
    sha_truth._set_resource_for_tests(real)
    yield
    sha_truth._reset_clients_for_tests()


@pytest.fixture
def tables():
    client = boto3.client("dynamodb", region_name="us-west-2")
    for name in (PENDING_TABLE, FAILED_TABLE, WORKSPACES_TABLE):
        client.create_table(
            TableName=name,
            AttributeDefinitions=[
                {"AttributeName": "PK", "AttributeType": "S"},
                {"AttributeName": "SK", "AttributeType": "S"},
            ],
            KeySchema=[
                {"AttributeName": "PK", "KeyType": "HASH"},
                {"AttributeName": "SK", "KeyType": "RANGE"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
    return boto3.resource("dynamodb", region_name="us-west-2")


def _seed_ws_row(tables):
    """Plant the pre-dispatch state: row exists with OLD SHAs across
    the three per-pipeline columns. The dispatch under test must
    leave these untouched."""
    tables.Table(WORKSPACES_TABLE).put_item(Item={
        "PK": f"WS#{WORKSPACE_ID}",
        "SK": f"GITHUB_REPO#{REPO}",
        "repo_name": REPO,
        "repo_type": "application",
        "include_code_maps": True,
        "last_commit_sha": OLD_SHA,
        "last_code_map_sha": OLD_SHA,
        "last_code_map_sha_ts": 100,
        "last_docs_sha": OLD_SHA,
        "last_docs_sha_ts": 100,
        "last_gkg_sha": OLD_SHA,
        "last_gkg_sha_ts": 100,
    })


def _read_ws_row(tables):
    return tables.Table(WORKSPACES_TABLE).get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]


def _read_pending(tables, pipeline, sha):
    pending_pk = f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}"
    return tables.Table(PENDING_TABLE).get_item(
        Key={"PK": pending_pk, "SK": f"PENDING#{pipeline}#{sha}"}
    ).get("Item")


def _load_module_as(name: str, path: Path):
    """Load a handler.py under a unique name to dodge the
    sys.modules cache collision between sibling Lambdas (all of
    which expose a module called ``handler``)."""
    import importlib.util

    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(mod)
    return mod


def test_github_sync_does_not_advance_per_pipeline_sha_on_dispatch(tables):
    """github-sync detects a real delta and dispatches the SQS
    messages. ``last_code_map_sha`` / ``last_docs_sha`` /
    ``last_gkg_sha`` must remain at OLD until consumers close their
    ledgers. Only the diagnostic columns
    (``last_attempted_sha`` / ``last_dispatched_sha``) move.
    """
    _seed_ws_row(tables)

    gs = _load_module_as(
        "_test_gs",
        _LAMBDA_ROOT / "github-sync" / "handler.py",
    )
    # plan-doc-incremental: include a doc file in the mocked tree so
    # the docs ledger path opens (instead of the no-change short-
    # circuit that advances last_docs_sha directly). The point of
    # this regression test is to verify the per-pipeline SHA columns
    # stay at OLD when CONSUMERS haven't run; the no-change short-
    # circuit is a different code path tested elsewhere.
    fetch_changed_patch = patch.object(
        gs, "fetch_changed_files", return_value={"src/main.py", "README.md"}
    )

    with patch.object(gs, "get_installation_token", return_value="tok"):
        with patch.object(gs, "get_default_branch", return_value="main"):
            with patch.object(
                gs,
                "get_latest_commit",
                return_value=(NEW_SHA, 500),
            ):
                with patch.object(
                    gs,
                    "get_repo_tree",
                    return_value=[
                        {
                            "path": "src/main.py",
                            "type": "blob",
                            "sha": "blob1",
                            "size": 100,
                        },
                        {
                            "path": "README.md",
                            "type": "blob",
                            "sha": "blob2",
                            "size": 200,
                        },
                    ],
                ), fetch_changed_patch, patch.object(
                    gs, "get_file_content", return_value="# README\n\nsome content"
                ), patch.object(
                    gs, "upload_file_to_s3", return_value="some-s3-key"
                ):
                    with patch.object(
                        gs, "queue_repo_for_code_maps", return_value=True
                    ):
                        with patch.object(
                            gs, "fetch_ingestion_config", return_value=["docs/adr"]
                        ):
                            # No doc files in the tree, so _advance_docs_sha_sync
                            # is skipped. Pure code-maps dispatch path.
                            result = gs.process_sync_request({
                                "workspace_id": WORKSPACE_ID,
                                "repo_name": REPO,
                                "installation_id": "12345",
                            })

    assert result["status"] == "success"

    row = _read_ws_row(tables)
    # The diagnostic columns moved forward.
    assert row["last_attempted_sha"] == NEW_SHA
    assert row["last_dispatched_sha"] == NEW_SHA
    # Legacy mirror still gets written for now (future plan
    # removes it). last_commit_sha advances on success — this is the
    # one column github-sync still writes eagerly. That's
    # intentional for back-compat with the UI's "last synced"
    # indicator; the per-pipeline columns below are the audit
    # truth.
    # The per-pipeline columns must NOT have moved.
    assert row["last_code_map_sha"] == OLD_SHA
    assert row["last_docs_sha"] == OLD_SHA
    assert row["last_gkg_sha"] == OLD_SHA
    assert int(row["last_code_map_sha_ts"]) == 100
    assert int(row["last_docs_sha_ts"]) == 100
    assert int(row["last_gkg_sha_ts"]) == 100

    # The dispatch-stage ledger row must exist with expected_count=1
    # and completed_count=0 — that's the evidence the work was
    # dispatched but not yet completed.
    pending = _read_pending(tables, "code_map_dispatch", NEW_SHA)
    assert pending is not None
    assert int(pending["expected_count"]) == 1
    assert int(pending["completed_count"]) == 0


def test_no_change_docs_advance_routes_through_dual_write(tables):
    """Regression pin: ``_advance_docs_sha_no_changes`` must advance
    ``last_docs_sha`` via ``shared.dual_write`` — NOT a direct
    ``table.update_item``.

    A direct write bypasses the EventBridge replication emit, so the
    HA secondary's ``last_docs_sha`` never advances for commits that
    touch zero doc files — it drifts behind permanently until a commit
    DOES change docs. This is the exact bug that left one repo lagging
    in prd (the no-change shortcut went straight to boto3 and escaped
    the dual-write sweep because it called ``ddb.Table(...).update_item``
    rather than ``table.update_item``). Pin it so it can't regress.
    """
    _seed_ws_row(tables)

    gs = _load_module_as(
        "_test_gs_nochange",
        _LAMBDA_ROOT / "github-sync" / "handler.py",
    )

    with patch.object(gs.dual_write, "ddb_update_item") as mock_dw:
        gs._advance_docs_sha_no_changes(WORKSPACE_ID, REPO, NEW_SHA, 200)

    # The write went through dual_write (which emits the replication
    # event), not a direct table call.
    mock_dw.assert_called_once()
    call = mock_dw.call_args
    assert call.args[0] == WORKSPACES_TABLE
    assert call.args[1] == {"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    assert ":sha" in call.kwargs["ExpressionAttributeValues"]
    assert call.kwargs["ExpressionAttributeValues"][":sha"] == NEW_SHA


def test_no_change_docs_advance_never_recreates_removed_repo_row(tables):
    """A repo removed mid-sync must not come back as a stub row.

    UpdateItem upserts, and ``attribute_not_exists(last_docs_sha_ts)`` is
    true on a missing item, so the advance has to require the row first.
    A stub row would make the repo read as connected again (ADR-052 v1.1).
    """
    gs = _load_module_as(
        "_test_gs_nochange_removed",
        _LAMBDA_ROOT / "github-sync" / "handler.py",
    )

    gs._advance_docs_sha_no_changes(WORKSPACE_ID, REPO, NEW_SHA, 200)

    resp = tables.Table(WORKSPACES_TABLE).get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )
    assert "Item" not in resp


def test_no_change_docs_advance_still_advances_existing_row(tables):
    _seed_ws_row(tables)
    gs = _load_module_as(
        "_test_gs_nochange_existing",
        _LAMBDA_ROOT / "github-sync" / "handler.py",
    )

    gs._advance_docs_sha_no_changes(WORKSPACE_ID, REPO, NEW_SHA, 200)

    row = _read_ws_row(tables)
    assert row["last_docs_sha"] == NEW_SHA
    assert int(row["last_docs_sha_ts"]) == 200
