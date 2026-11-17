# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Salesforce Sync Lambda.

SQS-driven cursor sync of Account/Contact/Opportunity records + their
recent activities. Renders each record to markdown and fans out to
file-ingestion for S3 upload + downstream vector indexing.

Cursor shape (persisted on connection row's ``config.cursors``):

    {
      "Account":        "2026-06-01T12:00:00Z",   # parent SystemModstamp
      "Contact":        "2026-06-01T12:00:00Z",
      "Opportunity":    "2026-06-01T12:00:00Z",
      "Task":           "2026-06-01T11:00:00Z",   # activity LastModifiedDate
      "Event":          "2026-06-01T11:00:00Z",
      "ContentVersion": "2026-06-01T10:00:00Z",   # attachment LastModifiedDate
    }

On full sync (all cursors empty): walk every record the OAuth user
can see, then baseline the activity cursors from
``latest_modified_date(Task|Event)`` so the next incremental only
scans activities changed after this run.

Incremental (cursors present):
1. Walk parents with ``SystemModstamp > cursor[object_type]`` and
   enqueue rendered docs.
2. Walk Task + Event changes since their cursors; find parents whose
   own SystemModstamp didn't move but whose activities did; fetch and
   re-enqueue those parents so the activity section stays current.
3. Walk ContentVersion changes since ``cursor["ContentVersion"]`` and
   enqueue every attached file for file-ingestion to fetch + persist.
   Cursor advances to the max LastModifiedDate seen. (Parity with
   app.retrieveit.ai/lambda/salesforce-integration -- OutcomeOps ported
   the API helpers but originally forgot the sync-side wiring.)

Rate limit signal: ``Salesforce rate limited`` log phrase from the
shared API layer -- CloudWatch metric filter alarms on it.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set, Tuple

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.oauth import salesforce as salesforce_oauth  # noqa: E402
from shared.region_guard import (  # noqa: E402
    RegionPassiveError,
    guard_active,
)
from shared.salesforce_api import (  # noqa: E402
    SalesforceRateLimitedError,
    changed_activity_parent_ids,
    changed_file_parent_ids,
    fetch_records_by_ids,
    iterate_records,
    latest_modified_date,
    list_content_versions,
    list_recent_activities,
)
from shared.salesforce_render import render_record_markdown, source_for


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")


PARENT_OBJECT_TYPES = ("Account", "Contact", "Opportunity")
ACTIVITY_OBJECT_TYPES = ("Task", "Event")
ATTACHMENT_CURSOR_KEY = "ContentVersion"

_SF_CONN_SK = "SALESFORCE_CONNECTION#"

_dynamodb = None
_sqs = None
_s3 = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ============================================================================
# Connection row + status updates
# ============================================================================


def _get_connection(workspace_id: str, connection_id: str) -> Dict[str, Any]:
    resp = _get_dynamodb().Table(WORKSPACES_TABLE).get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"{_SF_CONN_SK}{connection_id}",
        }
    )
    return resp.get("Item") or {}


def _update_sync_status(
    workspace_id: str,
    connection_id: str,
    *,
    status: str,
    error: str = "",
    cursors: Optional[Dict[str, str]] = None,
) -> None:
    """Best-effort connection row status update. Failures swallowed so a
    DDB throttle doesn't sink the whole sync."""
    try:
        expr_names: Dict[str, str] = {}
        expr_values: Dict[str, Any] = {
            ":s": status,
            ":now": _now_iso(),
        }
        set_parts = ["last_sync_status = :s", "updated_at = :now"]
        remove_parts: List[str] = []
        if error:
            set_parts.append("last_sync_error = :e")
            expr_values[":e"] = error[:500]
        else:
            expr_names["#err"] = "last_sync_error"
            remove_parts.append("#err")
        if cursors is not None:
            set_parts.append("#config = :cfg")
            expr_names["#config"] = "config"
            expr_values[":cfg"] = {"cursors": cursors}
        update_expr = "SET " + ", ".join(set_parts)
        if remove_parts:
            update_expr += " REMOVE " + ", ".join(remove_parts)
        kwargs = {
            "Key": {
                "PK": f"WS#{workspace_id}",
                "SK": f"{_SF_CONN_SK}{connection_id}",
            },
            "UpdateExpression": update_expr,
            "ExpressionAttributeValues": expr_values,
        }
        if expr_names:
            kwargs["ExpressionAttributeNames"] = expr_names
        _get_dynamodb().Table(WORKSPACES_TABLE).update_item(**kwargs)
    except Exception:
        logger.exception(
            f"Failed to update sync status for connection {connection_id}"
        )


# ============================================================================
# File-ingestion fan-out (FIFO queue)
# ============================================================================


def _enqueue_ingestion(
    payload: Dict[str, Any],
    *,
    sync_id: str,
) -> None:
    """Send one rendered-record message to the file-ingestion FIFO queue.

    MessageGroupId: (workspace, connection) so bursts from one sync run
    interleave with other connections' traffic. MessageDeduplicationId:
    sync_id + object_type + record_id so an SQS redelivery of THIS
    salesforce-sync message doesn't re-fan the same ingest.
    """
    workspace_id = payload["workspace_id"]
    connection_id = payload["connection_id"]
    source = payload["source"]
    file_info = payload.get("file", {})
    # ContentVersion messages don't carry record_id -- fall through to
    # content_version_id so distinct attachments in the same sync don't
    # collapse into one FIFO dedupe slot.
    ident = (
        file_info.get("record_id")
        or file_info.get("content_version_id")
        or "unknown"
    )
    _get_sqs().send_message(
        QueueUrl=FILE_INGESTION_QUEUE,
        MessageBody=json.dumps(payload),
        MessageGroupId=f"{workspace_id}-{connection_id}",
        MessageDeduplicationId=f"{sync_id}-{source}-{ident}",
    )


def _enqueue_record(
    *,
    workspace_id: str,
    connection_id: str,
    instance_url: str,
    object_type: str,
    record: Dict[str, Any],
    title: str,
    markdown: str,
    sync_id: str,
) -> None:
    record_id = record.get("Id", "")
    payload = {
        "source": source_for(object_type),
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "instance_url": instance_url,
        "file": {
            "record_id": record_id,
            "object_type": object_type,
            "title": title,
            "markdown": markdown,
            "sys_updated": record.get("SystemModstamp") or "",
        },
    }
    _enqueue_ingestion(payload, sync_id=sync_id)


def _enqueue_attachment(
    *,
    workspace_id: str,
    connection_id: str,
    instance_url: str,
    parent_id: str,
    content_version: Dict[str, Any],
    sync_id: str,
) -> None:
    """Enqueue one Salesforce attachment for file-ingestion to fetch.

    file-ingestion downloads the binary via VersionData REST (it holds
    the source OAuth), so we send only metadata -- matches the
    ServiceNow attachment path shape.
    """
    version_id = content_version.get("Id") or ""
    document_id = content_version.get("ContentDocumentId") or ""
    title = content_version.get("Title") or version_id
    extension = content_version.get("FileExtension") or ""
    file_name = f"{title}.{extension}" if extension else title
    payload = {
        "source": "salesforce_attachment",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "instance_url": instance_url,
        "file": {
            "content_version_id": version_id,
            "content_document_id": document_id,
            "parent_record_id": parent_id,
            "file_name": file_name,
            "content_size": content_version.get("ContentSize") or 0,
        },
    }
    _enqueue_ingestion(payload, sync_id=sync_id)


# ============================================================================
# Sync loop
# ============================================================================


def _walk_parents(
    *,
    workspace_id: str,
    connection_id: str,
    instance_url: str,
    access_token: str,
    object_type: str,
    cursor_in: str,
    seen_ids: Set[str],
    sync_id: str,
) -> Tuple[int, str, str]:
    """Walk records of ``object_type`` modified since ``cursor_in``,
    enqueue each with recent activities.

    Returns (count_enqueued, new_cursor, status). ``status`` is
    ``"ok"`` or ``"partial_success"`` if activity fetch failed for
    some records.
    """
    count = 0
    new_cursor = cursor_in
    partial = False
    for record in iterate_records(instance_url, access_token, object_type, cursor_in):
        record_id = record.get("Id")
        if not record_id:
            continue
        sys_mod = record.get("SystemModstamp") or ""
        if sys_mod > new_cursor:
            new_cursor = sys_mod
        try:
            activities = list_recent_activities(
                instance_url, access_token, object_type, record_id,
            )
        except (SalesforceRateLimitedError, ValueError) as e:
            logger.warning(
                f"Activity fetch failed for {object_type} {record_id}: {e}"
            )
            activities = []
            partial = True
        title, markdown = render_record_markdown(
            object_type, record, activities, instance_url,
        )
        _enqueue_record(
            workspace_id=workspace_id,
            connection_id=connection_id,
            instance_url=instance_url,
            object_type=object_type,
            record=record,
            title=title,
            markdown=markdown,
            sync_id=sync_id,
        )
        seen_ids.add(record_id)
        count += 1
    return count, new_cursor, ("partial_success" if partial else "ok")


def _rerender_parents_from_activities(
    *,
    workspace_id: str,
    connection_id: str,
    instance_url: str,
    access_token: str,
    cursors_in: Dict[str, str],
    seen_ids: Set[str],
    sync_id: str,
) -> Tuple[int, Dict[str, str]]:
    """Walk changed Task + Event; find parents that weren't already
    seen in the parent walk; fetch + re-enqueue them.

    Returns (count_enqueued, new_activity_cursors).
    """
    count = 0
    new_cursors: Dict[str, str] = {}
    # Collect all parent IDs across both activity kinds first so we
    # dedupe before the SOQL round-trips.
    parents_by_type: Dict[str, Set[str]] = {t: set() for t in PARENT_OBJECT_TYPES}
    for activity_type in ACTIVITY_OBJECT_TYPES:
        cursor = cursors_in.get(activity_type, "")
        try:
            parent_ids, new_cursor = changed_activity_parent_ids(
                instance_url, access_token, activity_type, cursor,
            )
        except (SalesforceRateLimitedError, ValueError) as e:
            logger.warning(
                f"Activity cursor walk failed ({activity_type}): {e}"
            )
            new_cursor = cursor
            parent_ids = set()
        new_cursors[activity_type] = new_cursor
        # An activity's parent can be any of the 3 object types; we
        # don't know from the ID alone, so try to fetch as each. The
        # fetch_records_by_ids call filters by Id + object_type at the
        # SOQL level, so mismatched IDs come back empty.
        for object_type in PARENT_OBJECT_TYPES:
            for rid in parent_ids:
                if rid not in seen_ids:
                    parents_by_type[object_type].add(rid)

    for object_type, ids in parents_by_type.items():
        if not ids:
            continue
        try:
            records = fetch_records_by_ids(
                instance_url, access_token, object_type, list(ids),
            )
        except (SalesforceRateLimitedError, ValueError) as e:
            logger.warning(
                f"fetch_records_by_ids failed ({object_type}): {e}"
            )
            continue
        for record in records:
            rid = record.get("Id", "")
            if not rid or rid in seen_ids:
                continue
            try:
                activities = list_recent_activities(
                    instance_url, access_token, object_type, rid,
                )
            except (SalesforceRateLimitedError, ValueError) as e:
                logger.warning(
                    f"Activity fetch failed on re-render for {rid}: {e}"
                )
                activities = []
            title, markdown = render_record_markdown(
                object_type, record, activities, instance_url,
            )
            _enqueue_record(
                workspace_id=workspace_id,
                connection_id=connection_id,
                instance_url=instance_url,
                object_type=object_type,
                record=record,
                title=title,
                markdown=markdown,
                sync_id=sync_id,
            )
            seen_ids.add(rid)
            count += 1

    return count, new_cursors


def _walk_attachments(
    *,
    workspace_id: str,
    connection_id: str,
    instance_url: str,
    access_token: str,
    cursor_in: str,
    sync_id: str,
) -> Tuple[int, str]:
    """Walk ContentVersions changed since ``cursor_in``, enqueue each
    for file-ingestion to fetch + persist.

    Returns (count_enqueued, new_cursor). The API layer's
    ``changed_file_parent_ids`` returns (parent_ids, new_cursor) where
    new_cursor is the max LastModifiedDate seen on any candidate
    ContentVersion -- that's what we persist so the next incremental
    picks up only newer changes.

    A single ContentVersion can be linked to multiple parents (rare but
    real -- ContentDocumentLink is many-to-one). We enqueue one message
    per (parent, version) so each parent's S3 prefix gets its own copy;
    file-ingestion dedupes at the S3 level if the same content lands
    twice.
    """
    try:
        parent_ids, new_cursor = changed_file_parent_ids(
            instance_url, access_token, cursor_in,
        )
    except (SalesforceRateLimitedError, ValueError) as e:
        logger.warning(f"changed_file_parent_ids failed: {e}")
        return 0, cursor_in

    if not parent_ids:
        return 0, new_cursor

    count = 0
    for parent_id in parent_ids:
        try:
            versions = list_content_versions(
                instance_url, access_token, parent_id,
            )
        except (SalesforceRateLimitedError, ValueError) as e:
            logger.warning(
                f"list_content_versions failed for parent {parent_id}: {e}"
            )
            continue
        for cv in versions:
            if not cv.get("Id"):
                continue
            _enqueue_attachment(
                workspace_id=workspace_id,
                connection_id=connection_id,
                instance_url=instance_url,
                parent_id=parent_id,
                content_version=cv,
                sync_id=sync_id,
            )
            count += 1

    return count, new_cursor


# ============================================================================
# SQS entrypoint
# ============================================================================


def process_sync_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Return status dict. Raises on unexpected exceptions so SQS
    partial-batch-failure can DLQ the record."""
    workspace_id = message["workspace_id"]
    connection_id = message["connection_id"]
    cursors_in: Dict[str, str] = message.get("cursors") or {}
    sync_id = message.get("sync_id") or str(uuid.uuid4())

    try:
        guard_active(
            "salesforce_sync",
            user_email="LAMBDA#salesforce-sync",
            workspace_id=workspace_id,
        )
    except RegionPassiveError:
        logger.info("Passive region; skipping sync message")
        return {"status": "skipped", "reason": "passive_region"}

    _update_sync_status(workspace_id, connection_id, status="in_progress")

    try:
        access_token, instance_url = salesforce_oauth.refresh_access_token(
            workspace_id, connection_id,
        )
    except ValueError as e:
        msg = str(e)
        logger.warning(f"Token refresh failed: {msg}")
        if "re-authenticate" in msg or "expired" in msg.lower():
            _update_sync_status(
                workspace_id, connection_id,
                status="reauth_required", error=msg,
            )
        else:
            _update_sync_status(
                workspace_id, connection_id,
                status="auth_error", error=msg,
            )
        return {"status": "reauth_required"}

    conn = _get_connection(workspace_id, connection_id)
    if not conn:
        logger.warning(f"Connection {connection_id} disappeared mid-sync")
        return {"status": "skipped", "reason": "connection_gone"}

    if not instance_url:
        instance_url = conn.get("instance_url", "")
    if not instance_url:
        _update_sync_status(
            workspace_id, connection_id,
            status="failed", error="No instance_url on connection or token",
        )
        return {"status": "failed"}

    is_full_sync = not any(cursors_in.get(t) for t in PARENT_OBJECT_TYPES)
    new_cursors: Dict[str, str] = dict(cursors_in)
    seen_ids: Set[str] = set()
    total_enqueued = 0
    partial = False

    try:
        for object_type in PARENT_OBJECT_TYPES:
            cursor_in = cursors_in.get(object_type, "")
            count, new_cursor, status_hint = _walk_parents(
                workspace_id=workspace_id,
                connection_id=connection_id,
                instance_url=instance_url,
                access_token=access_token,
                object_type=object_type,
                cursor_in=cursor_in,
                seen_ids=seen_ids,
                sync_id=sync_id,
            )
            total_enqueued += count
            new_cursors[object_type] = new_cursor
            if status_hint == "partial_success":
                partial = True

        if is_full_sync:
            # Baseline activity cursors so the next incremental only
            # looks at activities changed after this run.
            for activity_type in ACTIVITY_OBJECT_TYPES:
                try:
                    new_cursors[activity_type] = latest_modified_date(
                        instance_url, access_token, activity_type,
                    )
                except (SalesforceRateLimitedError, ValueError) as e:
                    logger.warning(
                        f"Baseline {activity_type} cursor failed: {e}"
                    )
                    new_cursors[activity_type] = cursors_in.get(activity_type, "")
        else:
            act_count, act_cursors = _rerender_parents_from_activities(
                workspace_id=workspace_id,
                connection_id=connection_id,
                instance_url=instance_url,
                access_token=access_token,
                cursors_in=cursors_in,
                seen_ids=seen_ids,
                sync_id=sync_id,
            )
            total_enqueued += act_count
            new_cursors.update(act_cursors)

        # Attachment walk runs after parent + activity paths on both
        # full and incremental syncs. cursor_in="" on full drives a
        # ContentVersion IsLatest=true full inventory scan.
        att_count, att_cursor = _walk_attachments(
            workspace_id=workspace_id,
            connection_id=connection_id,
            instance_url=instance_url,
            access_token=access_token,
            cursor_in=cursors_in.get(ATTACHMENT_CURSOR_KEY, ""),
            sync_id=sync_id,
        )
        total_enqueued += att_count
        new_cursors[ATTACHMENT_CURSOR_KEY] = att_cursor

    except SalesforceRateLimitedError as e:
        logger.warning(f"Salesforce rate limited on {e.endpoint}; partial sync")
        _update_sync_status(
            workspace_id, connection_id,
            status=("partial_success" if total_enqueued else "failed"),
            error="rate_limited",
            cursors=new_cursors,
        )
        return {
            "status": ("partial_success" if total_enqueued else "failed"),
            "enqueued": total_enqueued,
        }

    status = "partial_success" if partial else "success"
    _update_sync_status(
        workspace_id, connection_id,
        status=status,
        cursors=new_cursors,
    )
    logger.info(
        f"Sync done connection={connection_id} enqueued={total_enqueued} "
        f"status={status}"
    )
    return {"status": status, "enqueued": total_enqueued}


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """SQS partial-batch-failure entry."""
    records = event.get("Records", []) or []
    logger.info(f"Salesforce sync invoked with {len(records)} record(s)")

    batch_item_failures: List[Dict[str, str]] = []
    re_delivered = 0
    for record in records:
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record.get("body") or "{}")
            result = process_sync_message(body)
            if result.get("status") == "failed":
                batch_item_failures.append({"itemIdentifier": message_id})
                re_delivered += 1
        except Exception as e:
            logger.exception(f"Failed to process message {message_id}: {e}")
            batch_item_failures.append({"itemIdentifier": message_id})
            re_delivered += 1
    logger.info(f"Salesforce sync batch complete: re-delivered={re_delivered}")
    return {"batchItemFailures": batch_item_failures}
