# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.sha_truth``.

Covers the four-helper public surface (open_pending_batch,
mark_unit_complete, mark_unit_failed, try_advance_sha) and the
monotonic-advance guard that gates the workspaces-row SHA columns.

Test isolation mirrors test_symbol_graph_writer / _reader:
- ``mock_aws`` autouse so no test touches real AWS endpoints.
- Each test creates the three required tables (pending-work,
  failed-files, workspaces) under moto and points the module's
  resource handle at it via ``_set_resource_for_tests``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.sha_truth import ...`` works.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


PENDING_TABLE = "test-sha-truth-pending"
FAILED_TABLE = "test-sha-truth-failed"
WORKSPACES_TABLE = "test-sha-truth-workspaces"

WORKSPACE_ID = "ws_test"
REPO = "owner/test-repo"
SHA_A = "a" * 40
SHA_B = "b" * 40
COMMIT_TS_OLD = 1_700_000_000
COMMIT_TS_NEW = 1_700_001_000


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("PENDING_WORK_TABLE", PENDING_TABLE)
    monkeypatch.setenv("FAILED_FILES_TABLE", FAILED_TABLE)
    monkeypatch.setenv("WORKSPACES_TABLE", WORKSPACES_TABLE)
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_module_clients():
    from shared import sha_truth  # type: ignore

    sha_truth._reset_clients_for_tests()
    real = boto3.resource("dynamodb", region_name="us-west-2")
    sha_truth._set_resource_for_tests(real)
    yield
    sha_truth._reset_clients_for_tests()


@pytest.fixture
def tables():
    """Create the three tables required by ``shared.sha_truth``."""

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
    resource = boto3.resource("dynamodb", region_name="us-west-2")
    # The repo is attached to the workspace; try_advance_sha only updates an
    # existing row (it must not recreate a removed repo).
    resource.Table(WORKSPACES_TABLE).put_item(
        Item={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )
    return resource


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_batch(**overrides):
    from shared.sha_truth import PendingBatch  # type: ignore

    kwargs = dict(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        pipeline="docs",
        target_sha=SHA_A,
        expected_count=3,
        target_commit_timestamp=COMMIT_TS_NEW,
    )
    kwargs.update(overrides)
    return PendingBatch(**kwargs)


def _ledger_pk():
    return f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}"


# ===========================================================================
# open_pending_batch
# ===========================================================================


def test_open_pending_batch_writes_item(tables):
    from shared.sha_truth import open_pending_batch  # type: ignore

    open_pending_batch(_make_batch())

    table = tables.Table(PENDING_TABLE)
    resp = table.get_item(
        Key={
            "PK": _ledger_pk(),
            "SK": f"PENDING#docs#{SHA_A}",
        }
    )
    item = resp["Item"]
    assert item["workspace_id"] == WORKSPACE_ID
    assert item["repo"] == REPO
    assert item["pipeline"] == "docs"
    assert item["target_sha"] == SHA_A
    assert int(item["expected_count"]) == 3
    assert int(item["completed_count"]) == 0
    assert int(item["failed_count"]) == 0
    assert int(item["target_commit_timestamp"]) == COMMIT_TS_NEW
    assert int(item["ttl"]) > int(item["dispatched_at"])


def test_open_pending_batch_idempotent(tables):
    """Re-opening the same (workspace, repo, pipeline, target_sha) is a
    silent no-op — second call must not raise and must not overwrite
    expected_count / completed_count."""

    from shared.sha_truth import open_pending_batch  # type: ignore

    open_pending_batch(_make_batch(expected_count=3))
    # Second call with different expected_count would clobber the first
    # if put_item weren't gated. Verify it's gated.
    open_pending_batch(_make_batch(expected_count=999))

    table = tables.Table(PENDING_TABLE)
    resp = table.get_item(
        Key={
            "PK": _ledger_pk(),
            "SK": f"PENDING#docs#{SHA_A}",
        }
    )
    assert int(resp["Item"]["expected_count"]) == 3


def test_open_pending_batch_rejects_invalid_pipeline(tables):
    from shared.sha_truth import open_pending_batch  # type: ignore

    with pytest.raises(ValueError):
        open_pending_batch(_make_batch(pipeline="not_a_pipeline"))


def test_open_pending_batch_rejects_zero_expected_count(tables):
    from shared.sha_truth import open_pending_batch  # type: ignore

    with pytest.raises(ValueError):
        open_pending_batch(_make_batch(expected_count=0))


# ===========================================================================
# mark_unit_complete
# ===========================================================================


def test_mark_unit_complete_increments(tables):
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
    )

    open_pending_batch(_make_batch(expected_count=3))
    item1 = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    item2 = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    assert int(item1["completed_count"]) == 1
    assert int(item2["completed_count"]) == 2


def test_mark_unit_complete_no_ledger_returns_none(tables):
    """When the ledger row is missing (TTL'd or never opened), the
    helper returns None rather than raising. Consumers treat this as
    'this SHA is no longer in flight'."""

    from shared.sha_truth import mark_unit_complete  # type: ignore

    result = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    assert result is None


# ===========================================================================
# mark_unit_failed
# ===========================================================================


def test_mark_unit_failed_writes_failed_row_and_increments(tables):
    from shared.sha_truth import (  # type: ignore
        mark_unit_failed,
        open_pending_batch,
    )

    open_pending_batch(_make_batch(expected_count=3))
    item = mark_unit_failed(
        WORKSPACE_ID,
        REPO,
        "docs",
        SHA_A,
        file_path="lambda/foo/handler.py",
        failure_reason="extract_timeout",
        failure_detail="tree-sitter timed out after 30s",
        attempt_count=3,
    )
    assert int(item["failed_count"]) == 1

    # Failed-files row written with the right shape.
    failed_table = tables.Table(FAILED_TABLE)
    failed_resp = failed_table.get_item(
        Key={
            "PK": _ledger_pk(),
            "SK": f"FAILED#{SHA_A}#docs#lambda/foo/handler.py",
        }
    )
    failed = failed_resp["Item"]
    assert failed["failure_reason"] == "extract_timeout"
    assert failed["failure_detail"] == "tree-sitter timed out after 30s"
    assert int(failed["attempt_count"]) == 3


def test_mark_unit_failed_truncates_long_detail(tables):
    from shared.sha_truth import (  # type: ignore
        mark_unit_failed,
        open_pending_batch,
    )

    open_pending_batch(_make_batch(expected_count=3))
    long_detail = "x" * 5000
    mark_unit_failed(
        WORKSPACE_ID,
        REPO,
        "docs",
        SHA_A,
        file_path="lambda/foo/handler.py",
        failure_reason="boom",
        failure_detail=long_detail,
        attempt_count=1,
    )
    failed_table = tables.Table(FAILED_TABLE)
    item = failed_table.get_item(
        Key={
            "PK": _ledger_pk(),
            "SK": f"FAILED#{SHA_A}#docs#lambda/foo/handler.py",
        }
    )["Item"]
    # 1024-char cap enforced server-side.
    assert len(item["failure_detail"]) == 1024


# ===========================================================================
# try_advance_sha
# ===========================================================================


def test_try_advance_not_complete(tables):
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(_make_batch(expected_count=3))
    item = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    result = try_advance_sha(item)
    assert result.advanced is False
    assert result.reason == "not_complete"


def test_try_advance_has_failures_blocks(tables):
    """Default policy: any failed unit blocks the advance."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        mark_unit_failed,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(_make_batch(expected_count=2))
    mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    closing = mark_unit_failed(
        WORKSPACE_ID,
        REPO,
        "docs",
        SHA_A,
        file_path="x.py",
        failure_reason="boom",
        failure_detail="",
        attempt_count=1,
    )
    result = try_advance_sha(closing)
    assert result.advanced is False
    assert result.reason == "has_failures"


def test_try_advance_has_failures_with_partial_allowed(tables):
    """Per-workspace opt-in lets a partial-success batch advance."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        mark_unit_failed,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(_make_batch(expected_count=2))
    mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    closing = mark_unit_failed(
        WORKSPACE_ID,
        REPO,
        "docs",
        SHA_A,
        file_path="x.py",
        failure_reason="boom",
        failure_detail="",
        attempt_count=1,
    )
    result = try_advance_sha(closing, allow_partial_advance=True)
    assert result.advanced is True
    assert result.reason == "ok"


def test_try_advance_writes_column_and_ts(tables):
    """Successful advance writes both ``last_docs_sha`` and
    ``last_docs_sha_ts`` on the workspaces row."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(_make_batch(expected_count=1))
    closing = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    result = try_advance_sha(closing)
    assert result.advanced is True

    ws_table = tables.Table(WORKSPACES_TABLE)
    ws_item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    ).get("Item", {})
    assert ws_item["last_docs_sha"] == SHA_A
    assert int(ws_item["last_docs_sha_ts"]) == COMMIT_TS_NEW


def test_try_advance_does_not_recreate_removed_repo(tables):
    """A pipeline that closes after the repo was removed from the workspace
    must not upsert a stub workspaces row holding only the SHA columns."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    ws_table = tables.Table(WORKSPACES_TABLE)
    key = {"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}

    open_pending_batch(_make_batch(expected_count=1))
    closing = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    ws_table.delete_item(Key=key)  # repo removed mid-pipeline

    result = try_advance_sha(closing)
    assert result.advanced is False
    assert result.reason == "repo_removed"
    assert "Item" not in ws_table.get_item(Key=key)


def test_try_advance_monotonic_blocks_older_sha(tables):
    """An older SHA (lower commit_timestamp) must NOT advance past a
    newer one. Commit timestamps are the monotonic ordering — SHAs
    aren't ordered."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    # Advance to SHA_B (newer).
    open_pending_batch(
        _make_batch(
            target_sha=SHA_B, expected_count=1, target_commit_timestamp=COMMIT_TS_NEW
        )
    )
    closing_b = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_B)
    assert try_advance_sha(closing_b).advanced is True

    # Now try to advance to SHA_A (older). Must be blocked.
    open_pending_batch(
        _make_batch(
            target_sha=SHA_A, expected_count=1, target_commit_timestamp=COMMIT_TS_OLD
        )
    )
    closing_a = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    result = try_advance_sha(closing_a)
    assert result.advanced is False
    assert result.reason == "stale_sha"

    # Workspaces row still reflects SHA_B.
    ws_table = tables.Table(WORKSPACES_TABLE)
    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_docs_sha"] == SHA_B
    assert int(item["last_docs_sha_ts"]) == COMMIT_TS_NEW


def test_try_advance_idempotent_under_concurrent_callers(tables):
    """Two consumers closing the same batch at the same time: one
    advances, the other gets ``stale_sha`` harmlessly."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        open_pending_batch,
        try_advance_sha,
    )

    open_pending_batch(_make_batch(expected_count=1))
    closing = mark_unit_complete(WORKSPACE_ID, REPO, "docs", SHA_A)
    first = try_advance_sha(closing)
    second = try_advance_sha(closing)
    assert first.advanced is True
    assert second.advanced is False
    assert second.reason == "stale_sha"


# ===========================================================================
# Two-stage code-map ledger
# ===========================================================================
#
# The code-maps pipeline has two fan-out points:
#   github-sync          -> 1 repo-summaries SQS message  (code_map_dispatch)
#   process-repo-summary -> N code-maps SQS messages      (code_map_units)
# Both ledger stages must close before last_code_map_sha advances on the
# workspaces row. The pairing is invisible to consumers — each just calls
# try_advance_sha on its closing ledger, and the helper checks the other
# stage internally.


def _open_code_map_pair(*, sha=SHA_A, dispatch_count=1, units_count=3,
                       ts=COMMIT_TS_NEW):
    """Open both code-map ledger stages for the same SHA."""
    from shared.sha_truth import PendingBatch, open_pending_batch  # type: ignore

    open_pending_batch(PendingBatch(
        workspace_id=WORKSPACE_ID, repo=REPO,
        pipeline="code_map_dispatch", target_sha=sha,
        expected_count=dispatch_count, target_commit_timestamp=ts,
    ))
    open_pending_batch(PendingBatch(
        workspace_id=WORKSPACE_ID, repo=REPO,
        pipeline="code_map_units", target_sha=sha,
        expected_count=units_count, target_commit_timestamp=ts,
    ))


def test_try_advance_code_map_waits_for_both_stages(tables):
    """Close only the units stage -> no advance. Then close the
    dispatch stage -> advance to last_code_map_sha."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    _open_code_map_pair(units_count=2)

    # Close units fully (2 of 2) — dispatch still open. Should not advance.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_A)
    closing_units = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_units", SHA_A
    )
    result = try_advance_sha(closing_units)
    assert result.advanced is False
    assert result.reason == "not_complete"

    # Workspaces row still lacks the column.
    ws_table = tables.Table(WORKSPACES_TABLE)
    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    ).get("Item", {}) or {}
    assert "last_code_map_sha" not in item

    # Now close the dispatch stage. That close fires its own
    # try_advance_sha, which should succeed because units is already
    # closed.
    closing_dispatch = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_A
    )
    assert try_advance_sha(closing_dispatch).advanced is True

    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_code_map_sha"] == SHA_A
    assert int(item["last_code_map_sha_ts"]) == COMMIT_TS_NEW


def test_try_advance_code_map_partial_advances_with_policy(tables):
    """Units stage has a failed unit; default policy blocks the
    advance. With allow_partial_advance=True, both stages closed
    (with the failure recorded) advance the SHA."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        mark_unit_failed,
        try_advance_sha,
    )

    _open_code_map_pair(units_count=2)

    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_A)
    units_closing = mark_unit_failed(
        WORKSPACE_ID, REPO, "code_map_units", SHA_A,
        file_path="lambda/foo/handler.py",
        failure_reason="extract_timeout",
        failure_detail="",
        attempt_count=3,
    )
    dispatch_closing = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_A
    )

    # Default policy: no advance because units has a failure.
    assert try_advance_sha(dispatch_closing).reason == "not_complete"

    # Opt-in policy: both stages now consider themselves closed,
    # and dispatch's close triggers the advance.
    assert (
        try_advance_sha(dispatch_closing, allow_partial_advance=True).advanced
        is True
    )

    ws_table = tables.Table(WORKSPACES_TABLE)
    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_code_map_sha"] == SHA_A


def test_two_overlapping_shas_independent_ledgers(tables):
    """SHA_A and SHA_B in flight at the same time get separate ledger
    rows. Closing A first lands last_code_map_sha = SHA_A; closing B
    second moves it forward to SHA_B (newer ts)."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    # SHA_A (older) full pair
    _open_code_map_pair(sha=SHA_A, units_count=1, ts=COMMIT_TS_OLD)
    # SHA_B (newer) full pair
    _open_code_map_pair(sha=SHA_B, units_count=1, ts=COMMIT_TS_NEW)

    # Close SHA_A first.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_A)
    dispatch_a = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_A
    )
    assert try_advance_sha(dispatch_a).advanced is True

    ws_table = tables.Table(WORKSPACES_TABLE)
    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_code_map_sha"] == SHA_A
    assert int(item["last_code_map_sha_ts"]) == COMMIT_TS_OLD

    # Close SHA_B next.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_B)
    dispatch_b = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_B
    )
    assert try_advance_sha(dispatch_b).advanced is True

    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_code_map_sha"] == SHA_B
    assert int(item["last_code_map_sha_ts"]) == COMMIT_TS_NEW


def test_two_overlapping_shas_late_finisher_does_not_clobber(tables):
    """SHA_B (newer) closes first and advances. SHA_A (older) closes
    second — the conditional update sees a newer ts already in place
    and returns stale_sha. workspaces.last_code_map_sha stays at B."""

    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    _open_code_map_pair(sha=SHA_A, units_count=1, ts=COMMIT_TS_OLD)
    _open_code_map_pair(sha=SHA_B, units_count=1, ts=COMMIT_TS_NEW)

    # Close B first (newer commit).
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_B)
    dispatch_b = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_B
    )
    assert try_advance_sha(dispatch_b).advanced is True

    # Close A second. Should NOT clobber B.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_A)
    dispatch_a = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_A
    )
    result = try_advance_sha(dispatch_a)
    assert result.advanced is False
    assert result.reason == "stale_sha"

    ws_table = tables.Table(WORKSPACES_TABLE)
    item = ws_table.get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    )["Item"]
    assert item["last_code_map_sha"] == SHA_B
    assert int(item["last_code_map_sha_ts"]) == COMMIT_TS_NEW
