# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Failure-mode tests for plan-git-truth chunk 03.

Seven tests, one per failure mode documented in
plan-git-truth/overview.md. Plus one cross-test helper (`_seed_ws_row`)
that lets a test plant a starting state on the workspaces row before
exercising the ledger paths.

Test isolation mirrors test_sha_truth.py — moto autouse, three
tables created per test, resource handle injected into
``shared.sha_truth``.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


PENDING_TABLE = "test-failmode-pending"
FAILED_TABLE = "test-failmode-failed"
WORKSPACES_TABLE = "test-failmode-workspaces"

WORKSPACE_ID = "ws1"
REPO = "owner/repo"


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


def _open(pipeline, sha, ts, expected):
    """Shorthand for opening a pending ledger row."""
    from shared.sha_truth import PendingBatch, open_pending_batch  # type: ignore

    open_pending_batch(PendingBatch(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        pipeline=pipeline,
        target_sha=sha,
        expected_count=expected,
        target_commit_timestamp=ts,
    ))


def _read_ws_row(tables):
    return tables.Table(WORKSPACES_TABLE).get_item(
        Key={"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    ).get("Item") or {}


def _seed_ws_row(tables, **attrs):
    """Plant attributes on the workspaces row before a test runs."""
    item = {"PK": f"WS#{WORKSPACE_ID}", "SK": f"GITHUB_REPO#{REPO}"}
    item.update(attrs)
    tables.Table(WORKSPACES_TABLE).put_item(Item=item)


def _scan_failed_files(tables, sha):
    """Return failed-files rows for the given SHA, any pipeline."""
    pending_pk = f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}"
    resp = tables.Table(FAILED_TABLE).query(
        KeyConditionExpression=(
            Key("PK").eq(pending_pk)
            & Key("SK").begins_with(f"FAILED#{sha}#")
        )
    )
    return resp.get("Items", [])


# ===========================================================================
# Test 1 — Mid-batch death + redrive
# ===========================================================================


def test_mid_batch_death_redrive_completes_correctly(tables):
    """SQS delivers N=10 code-units. Consumer processes 1..4, dies.
    Redrive delivers the un-acked 5..10. Consumer processes those.
    Ledger ends at completed_count=10. After paired dispatch close,
    SHA advances exactly once. Replaying the last unit returns
    stale_sha (idempotency)."""
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    SHA = "a" * 40
    _open("code_map_units", SHA, ts=1_700_000_000, expected=10)
    _open("code_map_dispatch", SHA, ts=1_700_000_000, expected=1)

    # First wave: 4 units close before the consumer dies.
    for _ in range(4):
        mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA)

    # Redrive wave: SQS re-delivers the 6 un-acked records.
    last_units_ledger = None
    for _ in range(6):
        last_units_ledger = mark_unit_complete(
            WORKSPACE_ID, REPO, "code_map_units", SHA
        )
    assert int(last_units_ledger["completed_count"]) == 10

    # Units stage closed; dispatch stage not yet. Advance blocked.
    assert try_advance_sha(last_units_ledger).reason == "not_complete"

    # Close dispatch. Advance fires.
    dispatch_ledger = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA
    )
    assert try_advance_sha(dispatch_ledger).advanced is True

    row = _read_ws_row(tables)
    assert row["last_code_map_sha"] == SHA

    # Idempotency: replay the last units close. ts already at the
    # advanced value, so the conditional update fails harmlessly.
    replay = try_advance_sha(last_units_ledger)
    assert replay.advanced is False
    assert replay.reason == "stale_sha"


# ===========================================================================
# Test 2 — Overlapping commits, newer wins
# ===========================================================================


def test_overlapping_shas_newer_commit_wins(tables):
    """github-sync dispatches SHA_A at ts=100. Before A closes, also
    dispatches SHA_B at ts=200. Close B first → advances to B. Close
    A second → stale_sha, last_code_map_sha stays at B."""
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    SHA_A = "a" * 40
    SHA_B = "b" * 40

    # Both SHAs in flight concurrently.
    _open("code_map_units", SHA_A, ts=100, expected=1)
    _open("code_map_dispatch", SHA_A, ts=100, expected=1)
    _open("code_map_units", SHA_B, ts=200, expected=1)
    _open("code_map_dispatch", SHA_B, ts=200, expected=1)

    # Close B first.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_B)
    dispatch_b = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_B
    )
    assert try_advance_sha(dispatch_b).advanced is True

    row = _read_ws_row(tables)
    assert row["last_code_map_sha"] == SHA_B
    assert int(row["last_code_map_sha_ts"]) == 200

    # Now close A. Must NOT clobber B.
    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA_A)
    dispatch_a = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA_A
    )
    result = try_advance_sha(dispatch_a)
    assert result.advanced is False
    assert result.reason == "stale_sha"

    row = _read_ws_row(tables)
    assert row["last_code_map_sha"] == SHA_B
    assert int(row["last_code_map_sha_ts"]) == 200


# ===========================================================================
# Test 3 — DLQ wedge (default blocks, opt-in unblocks)
# ===========================================================================


def test_dlq_wedge_sha_does_not_advance_by_default(tables):
    """One unit fails permanently; default policy blocks the advance.
    failed-files row is written so the operator can see why."""
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        mark_unit_failed,
        try_advance_sha,
    )

    SHA = "c" * 40
    _open("code_map_units", SHA, ts=300, expected=5)
    _open("code_map_dispatch", SHA, ts=300, expected=1)

    for _ in range(4):
        mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA)
    units_ledger = mark_unit_failed(
        WORKSPACE_ID, REPO, "code_map_units", SHA,
        file_path="src/bad.py",
        failure_reason="extract_timeout",
        failure_detail="tree-sitter timed out after 30s",
        attempt_count=3,
    )
    result = try_advance_sha(units_ledger, allow_partial_advance=False)
    assert result.advanced is False
    assert result.reason == "has_failures"

    failed = _scan_failed_files(tables, SHA)
    assert len(failed) == 1
    assert failed[0]["failure_reason"] == "extract_timeout"
    assert failed[0]["file_path"] == "src/bad.py"


def test_dlq_wedge_advances_with_partial_policy(tables):
    """Same setup but the per-workspace allow_partial_advance opt-in
    unblocks the advance. Failed-files row still written for the
    operator audit trail."""
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        mark_unit_failed,
        try_advance_sha,
    )

    SHA = "d" * 40
    _open("code_map_units", SHA, ts=400, expected=2)
    _open("code_map_dispatch", SHA, ts=400, expected=1)

    mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA)
    units_ledger = mark_unit_failed(
        WORKSPACE_ID, REPO, "code_map_units", SHA,
        file_path="src/bad.py",
        failure_reason="extract_timeout",
        failure_detail="",
        attempt_count=3,
    )

    # Default policy blocks (regression-check for the previous test).
    assert try_advance_sha(units_ledger).reason == "has_failures"

    # Dispatch close with opt-in policy fires the advance.
    dispatch_ledger = mark_unit_complete(
        WORKSPACE_ID, REPO, "code_map_dispatch", SHA
    )
    assert (
        try_advance_sha(dispatch_ledger, allow_partial_advance=True).advanced
        is True
    )
    row = _read_ws_row(tables)
    assert row["last_code_map_sha"] == SHA


# ===========================================================================
# Test 4 — Per-unit dispatch failure does NOT fail the SQS record
# ===========================================================================
#
# Regression guard for the chunk-02b/2 behavior change in
# send_code_units_to_sqs. Inject a ClientError on one unit's
# send_message; assert the other units still dispatch, the function
# returns a partial success count, and the failed unit is recorded
# in code-map-failed-files.


def _load_module_as(name: str, path: Path):
    """Load a handler.py from a Lambda dir under a unique name so the
    sys.modules cache doesn't conflate sibling Lambdas (all of which
    expose a module called ``handler``)."""
    import importlib.util

    sys.modules.pop(name, None)
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    # Inject the Lambda dir onto sys.path so sibling imports resolve.
    parent = str(path.parent)
    if parent not in sys.path:
        sys.path.insert(0, parent)
    spec.loader.exec_module(mod)
    return mod


def test_per_unit_dispatch_failure_does_not_abort_batch(tables):
    """Inject ClientError on unit 5 of 10. Assert 9 successful
    send_message calls, one failed-files row, no exception, and
    failed_count=1 on the code_map_units ledger."""
    SHA = "e" * 40
    # process-repo-summary opens the units ledger before the loop.
    _open("code_map_units", SHA, ts=500, expected=10)

    from botocore.exceptions import ClientError

    rsh = _load_module_as(
        "_test_rsh",
        _LAMBDA_ROOT / "process-repo-summary" / "handler.py",
    )

    # Mock sqs_client at the handler level — fifth call raises.
    mock_sqs = MagicMock()
    call_count = {"n": 0}

    def fake_send(*a, **kw):
        call_count["n"] += 1
        if call_count["n"] == 5:
            raise ClientError(
                {"Error": {"Code": "QueueDoesNotExist", "Message": "gone"}},
                "SendMessage",
            )
        return None

    mock_sqs.send_message.side_effect = fake_send

    code_units = [
        {
            "batch_type": "handler",
            "group_name": f"unit-{i}",
            "file_paths": [f"src/u{i}.py"],
            "storage_key": f"summary#unit-{i}",
        }
        for i in range(10)
    ]

    with patch.object(rsh, "sqs_client", mock_sqs):
        with patch.object(
            rsh, "CODE_MAPS_QUEUE_URL", "https://sqs.example.com/q"
        ):
            sent = rsh.send_code_units_to_sqs(
                code_units,
                repo="repo",
                repo_project=REPO,
                workspace_id=WORKSPACE_ID,
                commit_sha=SHA,
                commit_timestamp=500,
            )

    assert sent == 9
    assert mock_sqs.send_message.call_count == 10

    # One failed-files row for the unit that didn't dispatch.
    failed = _scan_failed_files(tables, SHA)
    assert len(failed) == 1
    assert failed[0]["failure_reason"] == "sqs_send_failed"
    assert failed[0]["file_path"] == "summary#unit-4"  # 0-indexed

    # Units ledger now shows failed_count=1.
    pending_pk = f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}"
    ledger = tables.Table(PENDING_TABLE).get_item(
        Key={"PK": pending_pk, "SK": f"PENDING#code_map_units#{SHA}"}
    )["Item"]
    assert int(ledger["failed_count"]) == 1


# ===========================================================================
# Test 5 — TTL'd ledger row, consumer doesn't error
# ===========================================================================


def test_ttld_ledger_consumer_returns_none(tables):
    """Ledger row deleted (simulating TTL fired). Consumer message
    arrives. mark_unit_complete returns None. No exception. SHA
    unchanged."""
    from shared.sha_truth import mark_unit_complete  # type: ignore

    SHA = "f" * 40
    _open("code_map_units", SHA, ts=600, expected=3)

    # Simulate the TTL having fired by explicit DeleteItem.
    pending_pk = f"WS#{WORKSPACE_ID}#GITHUB_REPO#{REPO}"
    tables.Table(PENDING_TABLE).delete_item(
        Key={"PK": pending_pk, "SK": f"PENDING#code_map_units#{SHA}"}
    )

    result = mark_unit_complete(WORKSPACE_ID, REPO, "code_map_units", SHA)
    assert result is None

    # Workspaces row never had last_code_map_sha set.
    row = _read_ws_row(tables)
    assert "last_code_map_sha" not in row


# ===========================================================================
# Test 6 — Doc-only changes
# ===========================================================================


def test_doc_only_change_advances_docs_only(tables):
    """A push with only doc-file changes (no code units) advances
    last_docs_sha but leaves last_code_map_sha + last_gkg_sha at the
    previous values. The legacy last_commit_sha mirror would stay at
    the oldest of the three (unchanged here)."""
    from shared.sha_truth import (  # type: ignore
        mark_unit_complete,
        try_advance_sha,
    )

    OLD_SHA = "0" * 40
    NEW_SHA = "9" * 40

    _seed_ws_row(
        tables,
        last_code_map_sha=OLD_SHA,
        last_code_map_sha_ts=100,
        last_docs_sha=OLD_SHA,
        last_docs_sha_ts=100,
        last_gkg_sha=OLD_SHA,
        last_gkg_sha_ts=100,
        last_commit_sha=OLD_SHA,
    )

    _open("docs", NEW_SHA, ts=500, expected=2)

    mark_unit_complete(WORKSPACE_ID, REPO, "docs", NEW_SHA)
    closing = mark_unit_complete(WORKSPACE_ID, REPO, "docs", NEW_SHA)
    assert try_advance_sha(closing).advanced is True

    row = _read_ws_row(tables)
    assert row["last_docs_sha"] == NEW_SHA
    assert row["last_code_map_sha"] == OLD_SHA
    assert row["last_gkg_sha"] == OLD_SHA


# ===========================================================================
# Test 7 — gkg pointer-vs-workspaces consistency
# ===========================================================================


def test_gkg_advance_mirrors_pointer_sha(tables):
    """The process-batch-summary helper _advance_gkg_sha synthesizes a
    single-unit ledger entry for gkg and advances last_gkg_sha. After
    it runs, workspaces.last_gkg_sha matches the SHA the pointer was
    advanced to by commit_swap (commit_swap itself is not invoked
    here; this is a unit-scope test of the helper)."""
    SHA = "1" * 40
    pbsh = _load_module_as(
        "_test_pbsh",
        _LAMBDA_ROOT / "process-batch-summary" / "handler.py",
    )

    pbsh._advance_gkg_sha(WORKSPACE_ID, REPO, SHA)

    row = _read_ws_row(tables)
    assert row["last_gkg_sha"] == SHA
    assert row.get("last_gkg_sha_ts") is not None
