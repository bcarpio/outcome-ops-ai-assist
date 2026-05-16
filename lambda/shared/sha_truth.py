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

logger = logging.getLogger(__name__)


Pipeline = Literal["code_map", "docs", "gkg"]
_VALID_PIPELINES: tuple[str, ...] = ("code_map", "docs", "gkg")

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
        _pending_table().put_item(
            Item=item,
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
        resp = _pending_table().update_item(
            Key={
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
    _failed_table().put_item(
        Item={
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
        resp = _pending_table().update_item(
            Key={
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


def try_advance_sha(
    ledger_item: dict,
    *,
    allow_partial_advance: bool = False,
) -> AdvanceResult:
    """Decide whether to advance the per-pipeline SHA on the
    workspaces row, given a ledger item returned by
    ``mark_unit_complete`` or ``mark_unit_failed``.

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

    pipeline = ledger_item["pipeline"]
    target_sha = ledger_item["target_sha"]
    target_ts = int(ledger_item["target_commit_timestamp"])
    workspace_id = ledger_item["workspace_id"]
    repo = ledger_item["repo"]

    column = f"last_{pipeline}_sha"
    ts_column = f"last_{pipeline}_sha_ts"

    try:
        _workspaces_table().update_item(
            Key={"PK": _ws_pk(workspace_id), "SK": _ws_sk(repo)},
            UpdateExpression=f"SET {column} = :sha, {ts_column} = :ts",
            ConditionExpression=(
                f"attribute_not_exists({ts_column}) OR {ts_column} < :ts"
            ),
            ExpressionAttributeValues={":sha": target_sha, ":ts": target_ts},
        )
        return AdvanceResult(True, "ok")
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return AdvanceResult(False, "stale_sha")
        raise


__all__ = (
    "AdvanceResult",
    "PendingBatch",
    "Pipeline",
    "mark_unit_complete",
    "mark_unit_failed",
    "open_pending_batch",
    "try_advance_sha",
)
