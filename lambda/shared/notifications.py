# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Workspace-scoped notifications.

Any Lambda can call ``notify_workspace`` to raise a notification that the
workspace's admins see in the UI (for example: a repo whose ingest is
stalled on a target SHA). ``list_failed_files`` reads the per-file failure
rows that ``shared.sha_truth.mark_unit_failed`` writes, so the caller can
say which files failed.

Notifications table (env ``NOTIFICATIONS_TABLE``):
    PK  = ``WS#<workspace_id>``
    SK  = ``NOTIF#<notification_id>``
    ttl = epoch seconds, 7 days after the latest write

``notification_id`` is derived from (workspace, repo, target SHA, reason),
so raising the same notification again updates the existing row instead of
creating a duplicate. Writes go through ``shared.dual_write`` so they
replicate to the secondary region like other workspace data.

Writing is best-effort: ``notify_workspace`` never raises. A failed
notification must not fail the pipeline that tried to raise it.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import boto3
from boto3.dynamodb.conditions import Key

from shared import dual_write

logger = logging.getLogger(__name__)

NOTIFICATION_TTL_SECONDS = 7 * 86400
FAILED_FILES_LIMIT = 20

_missing_table_warned = False


# ---------------------------------------------------------------------------
# Resource accessors (lazy boto3 init, same pattern as shared.sha_truth).
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
    global _dynamodb_resource, _test_resource_override, _missing_table_warned
    _dynamodb_resource = None
    _test_resource_override = None
    _missing_table_warned = False


# ---------------------------------------------------------------------------
# Key builders
# ---------------------------------------------------------------------------


def notification_id_for(
    workspace_id: str, repo_name: str, target_sha: str, reason: str
) -> str:
    """Deterministic id: a repeat of the same condition hits the same row."""

    digest = hashlib.sha256(
        f"{workspace_id}|{repo_name}|{target_sha}|{reason}".encode("utf-8")
    ).hexdigest()
    return "notif_" + digest[:16]


def _notif_pk(workspace_id: str) -> str:
    return f"WS#{workspace_id}"


def _notif_sk(notification_id: str) -> str:
    return f"NOTIF#{notification_id}"


def _failed_pk(workspace_id: str, repo: str) -> str:
    # Same PK shape shared.sha_truth uses for the failed-files table.
    return f"WS#{workspace_id}#GITHUB_REPO#{repo}"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def notify_workspace(
    workspace_id: str,
    *,
    type: str,  # noqa: A002 - matches the stored attribute name
    severity: str,
    title: str,
    message: str,
    repo_name: str,
    added_by: str,
    reason: str,
    target_sha: str,
    failed_files: Optional[list[str]] = None,
    failed_files_total: int = 0,
) -> bool:
    """Create or refresh a workspace notification. Returns True on success.

    Upserts the row keyed by ``notification_id_for(...)``: every field and
    ``updated_at`` / ``ttl`` are overwritten, ``created_at`` is kept from
    the first write. ``failed_files`` is stored as given, capped at
    ``FAILED_FILES_LIMIT`` entries; ``failed_files_total`` carries the full
    count.

    Never raises. A missing ``NOTIFICATIONS_TABLE`` logs one WARNING per
    container and returns False; any other error is logged with
    ``exc_info=True`` and returns False.
    """

    global _missing_table_warned

    table_name = os.environ.get("NOTIFICATIONS_TABLE", "")
    if not table_name:
        if not _missing_table_warned:
            logger.warning(
                "[notifications] NOTIFICATIONS_TABLE not set; "
                "skipping notification for workspace=%s repo=%s reason=%s",
                workspace_id,
                repo_name,
                reason,
            )
            _missing_table_warned = True
        return False

    try:
        notification_id = notification_id_for(
            workspace_id, repo_name, target_sha, reason
        )
        now_epoch = int(time.time())
        now_iso = datetime.fromtimestamp(now_epoch, tz=timezone.utc).isoformat()
        files = list(failed_files or [])[:FAILED_FILES_LIMIT]

        dual_write.ddb_update_item(
            table_name,
            {"PK": _notif_pk(workspace_id), "SK": _notif_sk(notification_id)},
            UpdateExpression=(
                "SET notification_id = :nid, workspace_id = :ws, #type = :type, "
                "severity = :severity, title = :title, message = :message, "
                "repo_name = :repo, added_by = :added_by, reason = :reason, "
                "target_sha = :sha, failed_files = :files, "
                "failed_files_total = :total, updated_at = :now, #ttl = :ttl, "
                "created_at = if_not_exists(created_at, :now)"
            ),
            ExpressionAttributeNames={"#type": "type", "#ttl": "ttl"},
            ExpressionAttributeValues={
                ":nid": notification_id,
                ":ws": workspace_id,
                ":type": type,
                ":severity": severity,
                ":title": title,
                ":message": message,
                ":repo": repo_name,
                ":added_by": added_by,
                ":reason": reason,
                ":sha": target_sha,
                ":files": files,
                ":total": int(failed_files_total),
                ":now": now_iso,
                ":ttl": now_epoch + NOTIFICATION_TTL_SECONDS,
            },
        )
        logger.info(
            "[notifications] upserted %s workspace=%s repo=%s reason=%s",
            notification_id,
            workspace_id,
            repo_name,
            reason,
        )
        return True
    except Exception:
        logger.exception(
            "[notifications] failed to write notification workspace=%s "
            "repo=%s reason=%s sha=%s",
            workspace_id,
            repo_name,
            reason,
            target_sha,
        )
        return False


def list_failed_files(
    workspace_id: str,
    repo: str,
    target_sha: str,
    limit: int = FAILED_FILES_LIMIT,
) -> tuple[list[str], int]:
    """Return (first ``limit`` distinct failed paths, total distinct count).

    Reads the failed-files table (env ``FAILED_FILES_TABLE``) for one
    repo at one target SHA, across all pipelines. A file that failed in
    more than one pipeline counts once. Paginates on ``LastEvaluatedKey``.
    Errors propagate to the caller.
    """

    table = _resource().Table(os.environ["FAILED_FILES_TABLE"])
    query_kwargs = {
        "KeyConditionExpression": (
            Key("PK").eq(_failed_pk(workspace_id, repo))
            & Key("SK").begins_with(f"FAILED#{target_sha}#")
        ),
        "ProjectionExpression": "file_path",
    }

    seen: set[str] = set()
    paths: list[str] = []
    while True:
        resp = table.query(**query_kwargs)
        for item in resp.get("Items", []):
            path = item.get("file_path")
            if not path or path in seen:
                continue
            seen.add(path)
            if len(paths) < limit:
                paths.append(path)
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        query_kwargs["ExclusiveStartKey"] = last_key

    return paths, len(seen)


__all__ = (
    "FAILED_FILES_LIMIT",
    "NOTIFICATION_TTL_SECONDS",
    "list_failed_files",
    "notification_id_for",
    "notify_workspace",
)
