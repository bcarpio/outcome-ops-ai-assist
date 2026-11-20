# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""SHA-truth bookkeeping for per-pipeline ingest correctness.

Three pipelines (``code_map``, ``docs``, ``gkg``) each track their own
last-fully-ingested SHA on the workspaces row, gated by a pending-work
ledger so the SHA on the row is the SHA whose work is actually done.

Callers:
- dispatchers (github-sync, process-repo-summary's fan-out,
  process-batch-summary for gkg) call ``open_pending_batch`` before
  publishing SQS messages.
- consumers (process-repo-summary, file-ingestion,
  process-batch-summary) call ``mark_unit_complete`` /
  ``mark_unit_failed`` per message, then call ``try_advance_sha``
  when their increment closes the batch.

This module does NOT publish to SQS. It only writes to the ledger and
the workspaces row.

Schema lives in ``terraform/dynamodb-code-map-pending-work.tf`` and
``terraform/dynamodb-code-map-failed-files.tf``. The wider design
rationale (overlapping commits, monotonic SHA advance, DLQ wedge
policy) lives in the operator runbook.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Literal, Optional

import boto3
from botocore.exceptions import ClientError

from shared import dual_write

logger = logging.getLogger(__name__)


# Ledger-row pipeline values are finer-grained than the SHA-column
# pipeline. The code-maps flow has two fan-out stages — github-sync
# emits one repo-summaries message (``code_map_dispatch``);
# process-repo-summary fans that out into N per-code-unit messages
# (``code_map_units``). Both stages must close before
# ``last_code_map_sha`` advances on the workspaces row.
Pipeline = Literal["code_map_dispatch", "code_map_units", "docs", "gkg"]
_VALID_PIPELINES: tuple[str, ...] = (
    "code_map_dispatch",
    "code_map_units",
    "docs",
    "gkg",
)

# Maps a ledger pipeline to the SHA-column pipeline it backs. The
# workspaces row has three SHA columns; ledger rows are finer-grained
# so the two code-map stages can be observed independently.
_LEDGER_TO_SHA_COLUMN: dict[str, str] = {
    "code_map_dispatch": "code_map",
    "code_map_units": "code_map",
    "docs": "docs",
    "gkg": "gkg",
}

_PENDING_TTL_SECONDS = 7 * 86400  # ledger row auto-expires after 7 days
_FAILED_TTL_SECONDS = 30 * 86400  # per-file failure row keeps 30 days
_FAILURE_DETAIL_MAX_CHARS = 1024


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PendingBatch:
    """Input to ``open_pending_batch``.

    ``target_commit_timestamp`` is the epoch-seconds timestamp of the
    GitHub commit being processed (NOT the dispatch time). It drives
    the monotonic-advance guard in ``try_advance_sha`` so an older SHA
    finishing after a newer one does not clobber the newer SHA on the
    workspaces row.
    """

    workspace_id: str
    repo: str
    pipeline: Pipeline
    target_sha: str
    expected_count: int
    target_commit_timestamp: int


@dataclass(frozen=True)
class AdvanceResult:
    """Return value from ``try_advance_sha``.

    ``advanced`` is True iff the workspaces row's
    ``last_{pipeline}_sha`` was updated to ``target_sha``.

    ``reason`` is a stable short string for audit / dashboards:
      - ``ok``            advance happened
      - ``not_complete``  ledger still has outstanding work
      - ``has_failures``  failed_count > 0 and partial-advance disabled
      - ``stale_sha``     a newer SHA already advanced past this one
      - ``repo_removed``  the workspaces row no longer exists (repo was
                          removed mid-pipeline); nothing is written
    """

    advanced: bool
    reason: str


# ---------------------------------------------------------------------------
# Resource accessors (lazy boto3 init for cold-start friendliness +
# test injection — see test_sha_truth.py for the moto wiring).
# ---------------------------------------------------------------------------


_dynamodb_resource = None
_test_resource_override = None


def _resource():
    global _dynamodb_resource
    if _test_resource_override is not None:
        return _test_resource_override
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def _set_resource_for_tests(resource) -> None:
    """Inject a moto-backed resource so unit tests don't reach real DDB."""

    global _test_resource_override
    _test_resource_override = resource


def _reset_clients_for_tests() -> None:
    global _dynamodb_resource, _test_resource_override
    _dynamodb_resource = None
    _test_resource_override = None


def _pending_table():
    return _resource().Table(os.environ["PENDING_WORK_TABLE"])


def _failed_table():
    return _resource().Table(os.environ["FAILED_FILES_TABLE"])


def _workspaces_table():
    return _resource().Table(os.environ["WORKSPACES_TABLE"])


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def _ledger_pk(workspace_id: str, repo: str) -> str:
    # The pending-work and failed-files tables share this PK shape.
    return f"WS#{workspace_id}#GITHUB_REPO#{repo}"


def _pending_sk(pipeline: str, target_sha: str) -> str:
    return f"PENDING#{pipeline}#{target_sha}"


def _failed_sk(target_sha: str, pipeline: str, file_path: str) -> str:
    return f"FAILED#{target_sha}#{pipeline}#{file_path}"


def _ws_pk(workspace_id: str) -> str:
    return f"WS#{workspace_id}"


def _ws_sk(repo: str) -> str:
    # ``GITHUB_REPO#owner/repo`` — see feedback_ddb_repo_sk_convention.
    return f"GITHUB_REPO#{repo}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def open_pending_batch(batch: PendingBatch) -> None:
    """Write the ledger row before publishing SQS messages.

    Idempotent on (workspace, repo, pipeline, target_sha): re-
    dispatching the same SHA is a no-op write
    (``attribute_not_exists(SK)`` suppresses overwrite). If a second
    dispatcher hits ``ConditionalCheckFailed``, the first dispatcher
    already opened the batch — the consumers will close it; we return
    silently.
    """

    if batch.pipeline not in _VALID_PIPELINES:
        raise ValueError(f"invalid pipeline: {batch.pipeline}")
    if batch.expected_count <= 0:
        # Caller should skip the open if there is no work. Treating
        # this as a programming error so the dispatcher learns to
        # short-circuit early.
        raise ValueError("expected_count must be > 0")
    if not batch.workspace_id or not batch.repo or not batch.target_sha:
        raise ValueError("workspace_id, repo, target_sha are required")

    now = int(time.time())
    item = {
        "PK": _ledger_pk(batch.workspace_id, batch.repo),
        "SK": _pending_sk(batch.pipeline, batch.target_sha),
        "workspace_id": batch.workspace_id,
        "repo": batch.repo,
        "pipeline": batch.pipeline,
        "target_sha": batch.target_sha,
        "target_commit_timestamp": batch.target_commit_timestamp,
        "expected_count": batch.expected_count,
        "completed_count": 0,
        "failed_count": 0,
        "dispatched_at": now,
        "last_progress_at": now,
        "ttl": now + _PENDING_TTL_SECONDS,
    }
    try:
        dual_write.ddb_put_item(
            os.environ["PENDING_WORK_TABLE"],
            item,
            ConditionExpression="attribute_not_exists(SK)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.info(
                "pending batch already open for %s/%s %s@%s",
                batch.workspace_id,
                batch.repo,
                batch.pipeline,
                batch.target_sha,
            )
            return
        raise


def mark_unit_complete(
    workspace_id: str,
    repo: str,
    pipeline: Pipeline,
    target_sha: str,
) -> Optional[dict]:
    """Increment ``completed_count`` on the ledger.

    Returns the updated ledger item (so the caller can pass it
    straight to ``try_advance_sha``), or ``None`` if the ledger row
    no longer exists (TTL'd or never opened). The condition is
    ``attribute_exists(SK)`` — losing the row is non-fatal, the
    consumer just logs a warning and moves on.
    """

    if pipeline not in _VALID_PIPELINES:
        raise ValueError(f"invalid pipeline: {pipeline}")

    try:
        resp = dual_write.ddb_update_item(
            os.environ["PENDING_WORK_TABLE"],
            {
                "PK": _ledger_pk(workspace_id, repo),
                "SK": _pending_sk(pipeline, target_sha),
            },
            UpdateExpression=(
                "ADD completed_count :one SET last_progress_at = :now"
            ),
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeValues={":one": 1, ":now": int(time.time())},
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning(
                "pending row missing for %s/%s %s@%s -- skipping",
                workspace_id,
                repo,
                pipeline,
                target_sha,
            )
            return None
        raise


def mark_unit_failed(
    workspace_id: str,
    repo: str,
    pipeline: Pipeline,
    target_sha: str,
    file_path: str,
    failure_reason: str,
    failure_detail: str,
    attempt_count: int,
) -> Optional[dict]:
    """Record a permanent per-unit failure.

    Writes a row to the failed-files table AND increments
    ``failed_count`` on the ledger. Returns the updated ledger item
    (same shape as ``mark_unit_complete``) so the consumer can pass
    it into ``try_advance_sha``; ``None`` if the ledger row is gone.

    Consumers call this AFTER exhausting in-message retries -- the
    rest of the SQS batch keeps processing instead of bubbling the
    record into the DLQ.
    """

    if pipeline not in _VALID_PIPELINES:
        raise ValueError(f"invalid pipeline: {pipeline}")

    now = int(time.time())
    detail = (failure_detail or "")[:_FAILURE_DETAIL_MAX_CHARS]
    dual_write.ddb_put_item(
        os.environ["FAILED_FILES_TABLE"],
        {
            "PK": _ledger_pk(workspace_id, repo),
            "SK": _failed_sk(target_sha, pipeline, file_path),
            "workspace_id": workspace_id,
            "repo": repo,
            "target_sha": target_sha,
            "pipeline": pipeline,
            "file_path": file_path,
            "failure_reason": failure_reason,
            "failure_detail": detail,
            "failed_at": now,
            "attempt_count": attempt_count,
            "ttl": now + _FAILED_TTL_SECONDS,
        },
    )
    try:
        resp = dual_write.ddb_update_item(
            os.environ["PENDING_WORK_TABLE"],
            {
                "PK": _ledger_pk(workspace_id, repo),
                "SK": _pending_sk(pipeline, target_sha),
            },
            UpdateExpression=(
                "ADD failed_count :one SET last_progress_at = :now"
            ),
            ConditionExpression="attribute_exists(SK)",
            ExpressionAttributeValues={":one": 1, ":now": now},
            ReturnValues="ALL_NEW",
        )
        return resp.get("Attributes")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise


def _stage_closed(
    workspace_id: str,
    repo: str,
    pipeline: str,
    target_sha: str,
    *,
    allow_partial_advance: bool,
) -> Optional[bool]:
    """Return True if the given ledger stage is closed (completed +
    failed >= expected, with the failed-count policy applied), False
    if it's still in flight, or None if the row doesn't exist.

    Used by ``try_advance_sha`` to check the OTHER code-map stage
    when one stage's close fires. The ``None`` case (TTL'd row, or
    pipeline that doesn't use a paired stage) is treated as
    "stage not present" by the caller.
    """

    try:
        resp = _pending_table().get_item(
            Key={
                "PK": _ledger_pk(workspace_id, repo),
                "SK": _pending_sk(pipeline, target_sha),
            },
        )
    except ClientError:
        return None
    item = resp.get("Item")
    if not item:
        return None
    expected = int(item.get("expected_count", 0))
    completed = int(item.get("completed_count", 0))
    failed = int(item.get("failed_count", 0))
    if completed + failed < expected:
        return False
    if failed > 0 and not allow_partial_advance:
        return False
    return True


def batch_in_flight(
    workspace_id: str,
    repo: str,
    pipeline: Pipeline,
    target_sha: str,
    *,
    stale_after_seconds: int,
) -> bool:
    """True when a ledger batch for ``target_sha`` is open (completed +
    failed < expected) AND made progress within ``stale_after_seconds``.

    Lets a dispatcher tell "still running" from "died partway": an open
    row whose consumers stopped reporting progress is treated as dead so
    the caller can redo the work. A missing row, a closed row, or a read
    error all return False (redoing is safe, skipping is not).
    """

    if pipeline not in _VALID_PIPELINES:
        raise ValueError(f"invalid pipeline: {pipeline}")
    try:
        resp = _pending_table().get_item(
            Key={
                "PK": _ledger_pk(workspace_id, repo),
                "SK": _pending_sk(pipeline, target_sha),
            },
        )
    except ClientError:
        return False
    item = resp.get("Item")
    if not item:
        return False
    expected = int(item.get("expected_count", 0))
    done = int(item.get("completed_count", 0)) + int(item.get("failed_count", 0))
    if done >= expected:
        return False
    last_progress = int(item.get("last_progress_at") or item.get("dispatched_at") or 0)
    return int(time.time()) - last_progress < stale_after_seconds


def try_advance_sha(
    ledger_item: dict,
    *,
    allow_partial_advance: bool = False,
) -> AdvanceResult:
    """Decide whether to advance the per-pipeline SHA on the
    workspaces row, given a ledger item returned by
    ``mark_unit_complete`` or ``mark_unit_failed``.

    Two-stage handling for ``code_map``: both
    ``code_map_dispatch`` and ``code_map_units`` ledger rows for
    the SHA must be closed before ``last_code_map_sha`` advances.
    The other stage is read via ``_stage_closed``.

    Idempotent: concurrent consumers closing the same batch race
    into a single advance; the conditional update fails the
    others harmlessly with ``stale_sha``.

    Monotonic: comparison is by ``last_{pipeline}_sha_ts`` (commit
    timestamp), not the SHA string — Git SHAs aren't ordered, but
    commit timestamps from the dispatcher are.
    """

    expected = int(ledger_item["expected_count"])
    completed = int(ledger_item.get("completed_count", 0))
    failed = int(ledger_item.get("failed_count", 0))
    if completed + failed < expected:
        return AdvanceResult(False, "not_complete")
    if failed > 0 and not allow_partial_advance:
        return AdvanceResult(False, "has_failures")

    ledger_pipeline = ledger_item["pipeline"]
    target_sha = ledger_item["target_sha"]
    target_ts = int(ledger_item["target_commit_timestamp"])
    workspace_id = ledger_item["workspace_id"]
    repo = ledger_item["repo"]

    sha_column_pipeline = _LEDGER_TO_SHA_COLUMN.get(ledger_pipeline)
    if sha_column_pipeline is None:
        raise ValueError(f"unknown ledger pipeline: {ledger_pipeline}")

    # Two-stage gate for code_map: when this close fires, also check
    # the paired stage. Skip the gate if we're already on the paired
    # stage (otherwise we'd recurse on the row we just closed).
    if sha_column_pipeline == "code_map":
        paired_stage = (
            "code_map_units"
            if ledger_pipeline == "code_map_dispatch"
            else "code_map_dispatch"
        )
        if not _stage_closed(
            workspace_id,
            repo,
            paired_stage,
            target_sha,
            allow_partial_advance=allow_partial_advance,
        ):
            return AdvanceResult(False, "not_complete")

    column = f"last_{sha_column_pipeline}_sha"
    ts_column = f"last_{sha_column_pipeline}_sha_ts"

    try:
        dual_write.ddb_update_item(
            os.environ["WORKSPACES_TABLE"],
            {"PK": _ws_pk(workspace_id), "SK": _ws_sk(repo)},
            UpdateExpression=f"SET {column} = :sha, {ts_column} = :ts",
            # attribute_exists(PK): UpdateItem upserts, so without this a
            # pipeline that finishes after the repo was removed from the
            # workspace recreates the deleted row as a stub holding only the
            # SHA columns.
            ConditionExpression=(
                f"attribute_exists(PK) AND "
                f"(attribute_not_exists({ts_column}) OR {ts_column} < :ts)"
            ),
            ExpressionAttributeValues={":sha": target_sha, ":ts": target_ts},
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        return AdvanceResult(True, "ok")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            if not exc.response.get("Item"):
                return AdvanceResult(False, "repo_removed")
            return AdvanceResult(False, "stale_sha")
        raise


def advance_sha_without_work(
    workspace_id: str,
    repo: str,
    pipeline: str,
    target_sha: str,
    target_commit_timestamp: int,
) -> AdvanceResult:
    """Advance ``last_{pipeline}_sha`` for a commit that needed no work.

    For a commit that changed nothing this pipeline tracks (for example a
    docs- or lockfile-only commit leaves zero code units to rebuild), no
    ledger row is ever opened, so ``try_advance_sha`` can never fire and
    the column would stay on the previous SHA -- indistinguishable from a
    real failure. This writes the column directly, with the same guards as
    ``try_advance_sha``: the repo row must still exist, and the commit
    timestamp must be newer than the stored one.

    ``pipeline`` is the column stem: ``code_map``, ``docs`` or ``gkg``.
    """

    column = f"last_{pipeline}_sha"
    ts_column = f"last_{pipeline}_sha_ts"
    try:
        dual_write.ddb_update_item(
            os.environ["WORKSPACES_TABLE"],
            {"PK": _ws_pk(workspace_id), "SK": _ws_sk(repo)},
            UpdateExpression=f"SET {column} = :sha, {ts_column} = :ts",
            ConditionExpression=(
                f"attribute_exists(PK) AND "
                f"(attribute_not_exists({ts_column}) OR {ts_column} < :ts)"
            ),
            ExpressionAttributeValues={
                ":sha": target_sha,
                ":ts": int(target_commit_timestamp),
            },
            ReturnValuesOnConditionCheckFailure="ALL_OLD",
        )
        return AdvanceResult(True, "ok")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            if not exc.response.get("Item"):
                return AdvanceResult(False, "repo_removed")
            return AdvanceResult(False, "stale_sha")
        raise


# SHA-column pipeline -> (ledger stages that must be closed, SHA columns
# to advance). ``code_map`` also advances ``gkg``: the symbol graph is
# built from the same code units, so skipping past a code-map failure
# skips it for the graph too.
_ADVANCE_PAST_FAILURES_PLAN: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "docs": (("docs",), ("docs",)),
    "code_map": (("code_map_dispatch", "code_map_units"), ("code_map", "gkg")),
}


def advance_past_failures(
    workspace_id: str,
    repo: str,
    pipeline: str,
    target_sha: str,
    target_commit_timestamp: int,
) -> AdvanceResult:
    """Advance a SHA column past a closed batch that recorded failures.

    Used once the scheduled-sync heal budget for ``target_sha`` is spent,
    so one permanently failing unit does not pin the repo's completed SHA
    forever (the next diff would otherwise grow without bound). The
    failed units stay recorded in the failed-files table; this only moves
    the baseline.

    ``pipeline`` is ``docs`` or ``code_map``. ``docs`` requires the
    ``docs`` ledger row; ``code_map`` requires both the
    ``code_map_dispatch`` and ``code_map_units`` rows and advances both
    ``last_code_map_sha`` and ``last_gkg_sha``.

    Reasons:
      - ``not_closed``    a required ledger row is missing, unreadable or
                          still has outstanding units
      - ``no_failures``   every required row closed clean (the normal
                          ``try_advance_sha`` path handles that)
      - ``ok``            at least one column advanced
      - ``stale_sha``     every column already holds a newer commit
      - ``repo_removed``  the workspaces row no longer exists
    """

    plan = _ADVANCE_PAST_FAILURES_PLAN.get(pipeline)
    if plan is None:
        raise ValueError(f"invalid pipeline for advance_past_failures: {pipeline}")
    stages, columns = plan

    failed_total = 0
    for stage in stages:
        try:
            resp = _pending_table().get_item(
                Key={
                    "PK": _ledger_pk(workspace_id, repo),
                    "SK": _pending_sk(stage, target_sha),
                },
            )
        except ClientError:
            logger.warning(
                "advance_past_failures: ledger read failed for %s %s@%s",
                repo,
                stage,
                target_sha[:7],
                exc_info=True,
            )
            return AdvanceResult(False, "not_closed")
        item = resp.get("Item")
        if not item:
            return AdvanceResult(False, "not_closed")
        expected = int(item.get("expected_count", 0))
        completed = int(item.get("completed_count", 0))
        failed = int(item.get("failed_count", 0))
        if completed + failed < expected:
            return AdvanceResult(False, "not_closed")
        failed_total += failed

    if failed_total <= 0:
        return AdvanceResult(False, "no_failures")

    logger.warning(
        "advancing %s past %d failed units for %s@%s",
        pipeline,
        failed_total,
        repo,
        target_sha[:7],
    )

    results = [
        advance_sha_without_work(
            workspace_id, repo, column, target_sha, target_commit_timestamp
        )
        for column in columns
    ]
    if any(r.reason == "repo_removed" for r in results):
        return AdvanceResult(False, "repo_removed")
    if any(r.advanced for r in results):
        return AdvanceResult(True, "ok")
    return AdvanceResult(False, "stale_sha")


__all__ = (
    "AdvanceResult",
    "PendingBatch",
    "Pipeline",
    "advance_past_failures",
    "advance_sha_without_work",
    "batch_in_flight",
    "mark_unit_complete",
    "mark_unit_failed",
    "open_pending_batch",
    "try_advance_sha",
)
