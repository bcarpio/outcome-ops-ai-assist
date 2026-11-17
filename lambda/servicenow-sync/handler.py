# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
ServiceNow sync worker.

SQS-triggered. For each message ``{workspace_id, connection_id, cursor}``:

1. ``guard_active()`` gate. Passive region short-circuits; SQS retries.
2. Refresh the access token via ``shared.oauth.servicenow``. ``invalid_grant``
   flips the row to ``reauth_required`` and the message is ack'd (not
   retried -- user has to reconnect).
3. Walk ``kb_knowledge`` filtered on ``workflow_state=published`` +
   ``sys_updated_on > cursor`` in 200-page chunks; per-article + per-attachment
   enqueue to ``FILE_INGESTION_QUEUE``.
4. Orphan cleanup on complete enumeration: list S3 keys under
   ``workspaces/{ws}/servicenow/{conn}/`` and enqueue delete-worker messages
   for any ``sys_id`` not seen in this walk.
5. Advance ``config.cursor`` on the connection row.

Failures land in ``batchItemFailures`` for redelivery (chunk 02 remediation
contract). Ingestion writes are idempotent (S3 puts on stable keys, DDB
upserts).
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
import uuid
from typing import Any, Dict, List, Set, Tuple

import boto3

# shared/ importable via zip layout.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.oauth import servicenow as servicenow_oauth  # noqa: E402
from shared.region_guard import RegionPassiveError, guard_active  # noqa: E402

# REST client lives under shared/ per chunk 16 pattern -- one canonical copy
# both the integration and sync Lambdas import.
from shared.servicenow_api import (  # noqa: E402
    ARTICLE_LIST_LIMIT,
    ServiceNowRateLimitedError,
    list_articles,
    list_attachments_for_article,
)



logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
KB_BUCKET = os.environ.get("KB_BUCKET")


# Extension allowlist mirrors RetrieveIT's -- what file-ingestion knows how
# to render for KB retrieval. Attachments outside this set are silently
# skipped (counted as "skipped" not "failed").
_ALLOWED_EXTENSIONS = {
    ".md", ".txt", ".pdf", ".yaml", ".yml", ".json", ".docx", ".xlsx",
    ".pptx", ".csv", ".html", ".htm",
}


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


def _get_s3():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _has_allowed_extension(name: str) -> bool:
    if not name:
        return False
    lower = name.lower()
    return any(lower.endswith(ext) for ext in _ALLOWED_EXTENSIONS)


def _update_sync_status(
    workspace_id: str,
    connection_id: str,
    *,
    status: str,
    error: str = "",
    cursor: str = "",
) -> None:
    """Best-effort status update. Failures are logged and swallowed so the
    ingest side keeps working even if DDB is throttled at write time."""
    try:
        expr_names: Dict[str, str] = {}
        expr_values: Dict[str, Any] = {
            ":s": status,
            ":now": datetime.now(timezone.utc).isoformat(),
        }
        set_parts = ["last_sync_status = :s", "updated_at = :now"]
        remove_parts: List[str] = []
        if error:
            set_parts.append("last_sync_error = :e")
            expr_values[":e"] = error[:500]
        else:
            # Clear stale error on success/in-progress. REMOVE (not SET :null)
            # because that keeps the row shape clean for downstream scans.
            expr_names["#err"] = "last_sync_error"
            remove_parts.append("#err")
        if cursor:
            set_parts.append("#config = :cfg")
            expr_names["#config"] = "config"
            expr_values[":cfg"] = {"cursor": cursor}
        update_expr = "SET " + ", ".join(set_parts)
        if remove_parts:
            update_expr += " REMOVE " + ", ".join(remove_parts)
        kwargs = {
            "Key": {
                "PK": f"WS#{workspace_id}",
                "SK": f"SERVICENOW_CONNECTION#{connection_id}",
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


def _get_connection(workspace_id: str, connection_id: str) -> Dict[str, Any]:
    resp = _get_dynamodb().Table(WORKSPACES_TABLE).get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
        }
    )
    return resp.get("Item") or {}


def _enqueue_ingestion(payload: Dict[str, Any], *, sync_id: str) -> None:
    """Enqueue a single article/attachment for file-ingestion.

    file-ingestion queue is FIFO, so MessageGroupId + MessageDeduplicationId
    are required. Group by (workspace, connection) so a burst of ingest
    calls from one sync run interleaves cleanly with other connections'
    traffic. Dedup by sync_id + article/attachment sys_id so an SQS
    redelivery of THIS servicenow-sync message doesn't re-fan the same
    ingest twice.
    """
    workspace_id = payload["workspace_id"]
    connection_id = payload["connection_id"]
    file_info = payload.get("file", {})
    source = payload.get("source", "servicenow")
    # Prefer attachment_sys_id over article_sys_id for attachment messages
    # so an article and its attachment don't collide on the same dedup id.
    ident = (
        file_info.get("attachment_sys_id")
        or file_info.get("article_sys_id")
        or "unknown"
    )
    _get_sqs().send_message(
        QueueUrl=FILE_INGESTION_QUEUE,
        MessageBody=json.dumps(payload),
        MessageGroupId=f"{workspace_id}-{connection_id}",
        MessageDeduplicationId=f"{sync_id}-{source}-{ident}",
    )


def _list_existing_sys_ids(workspace_id: str, connection_id: str) -> Set[str]:
    """List sys_ids already in S3 under this connection's prefix.

    Key shape (chunk 05):
      workspaces/{ws}/servicenow/{conn}/{sys_id}.html                    (article)
      workspaces/{ws}/servicenow/{conn}/{article_sys_id}/attachments/... (attachment)
    We only care about the top-level article sys_ids for orphan diff --
    an article's attachments get cleaned up alongside the article by the
    delete-worker when the article's sys_id lands in an orphan message.
    """
    prefix = f"workspaces/{workspace_id}/servicenow/{connection_id}/"
    sys_ids: Set[str] = set()
    if not KB_BUCKET:
        return sys_ids
    s3 = _get_s3()
    continuation = None
    while True:
        kwargs = {"Bucket": KB_BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if continuation:
            kwargs["ContinuationToken"] = continuation
        resp = s3.list_objects_v2(**kwargs)
        for obj in resp.get("Contents", []) or []:
            key = obj["Key"]
            remainder = key[len(prefix):]
            # article file: "{sys_id}.html"
            # attachment file: "{article_sys_id}/attachments/{...}"
            first_segment = remainder.split("/", 1)[0]
            if first_segment.endswith(".html"):
                sys_ids.add(first_segment[:-5])
            else:
                sys_ids.add(first_segment)
        if not resp.get("IsTruncated"):
            break
        continuation = resp.get("NextContinuationToken")
    return sys_ids


def _enqueue_orphan_deletes(
    workspace_id: str, connection_id: str, orphan_sys_ids: Set[str]
) -> int:
    """Send one delete-worker message per orphaned article sys_id."""
    if not orphan_sys_ids:
        return 0
    sqs = _get_sqs()
    for sys_id in orphan_sys_ids:
        # delete-worker dispatches on `action`; without it the message
        # is skipped and the orphaned article + attachments stay in S3
        # + vectors forever. delete_prefix walks the whole article's
        # sub-tree (HTML at {sys_id}.html and attachments/*) in one
        # message.
        sqs.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(
                {
                    "action": "delete_prefix",
                    "source": "servicenow",
                    "workspace_id": workspace_id,
                    "connection_id": connection_id,
                    "article_sys_id": sys_id,
                    "prefix": f"{connection_id}/{sys_id}",
                }
            ),
        )
    return len(orphan_sys_ids)


def process_sync_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Return ``{"status": "success"|"partial_success"|"failed"|"skipped"|"reauth_required", ...}``.

    Raises on unexpected exceptions so the SQS caller can add the record
    to ``batchItemFailures``.
    """
    workspace_id = message["workspace_id"]
    connection_id = message["connection_id"]
    cursor_in = message.get("cursor") or ""
    # sync_id scopes FIFO dedup for the fan-out to file-ingestion; an SQS
    # redelivery of THIS message uses the same sync_id so the same
    # article/attachment isn't ingested twice.
    sync_id = message.get("sync_id") or str(uuid.uuid4())

    try:
        guard_active(
            "servicenow_sync",
            user_email="LAMBDA#servicenow-sync",
            workspace_id=workspace_id,
        )
    except RegionPassiveError:
        logger.info("Passive region; skipping sync message")
        return {"status": "skipped", "reason": "passive_region"}

    _update_sync_status(workspace_id, connection_id, status="in_progress")

    try:
        access_token = servicenow_oauth.refresh_access_token(workspace_id, connection_id)
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
        # Not retry-worthy -- return success so SQS deletes the message.
        return {"status": "reauth_required"}

    conn = _get_connection(workspace_id, connection_id)
    if not conn:
        logger.warning(f"Connection {connection_id} disappeared mid-sync")
        return {"status": "skipped", "reason": "connection_gone"}
    instance_url = servicenow_oauth.get_instance_url()

    articles_synced = 0
    attachments_synced = 0
    files_skipped = 0
    current_sys_ids: Set[str] = set()
    new_cursor = cursor_in
    enumeration_complete = True
    failures: List[str] = []

    offset = 0
    try:
        while True:
            articles, has_more = list_articles(
                instance_url, access_token, cursor_in, offset,
            )
            for article in articles:
                sys_id = article.get("sys_id")
                if not sys_id:
                    continue
                current_sys_ids.add(sys_id)

                sys_updated_on = article.get("sys_updated_on") or ""
                if sys_updated_on > new_cursor:
                    new_cursor = sys_updated_on

                _enqueue_ingestion(
                    {
                        "source": "servicenow_article",
                        "workspace_id": workspace_id,
                        "connection_id": connection_id,
                        "instance_url": instance_url,
                        "file": {
                            "article_sys_id": sys_id,
                            "number": article.get("number"),
                            "short_description": article.get("short_description"),
                            "sys_updated_on": sys_updated_on,
                        },
                    },
                    sync_id=sync_id,
                )
                articles_synced += 1

                try:
                    atts = list_attachments_for_article(
                        instance_url, access_token, sys_id,
                    )
                except (ServiceNowRateLimitedError, ValueError) as e:
                    logger.warning(f"Attachment list failed for {sys_id}: {e}")
                    atts = []

                for att in atts:
                    att_sys_id = att.get("sys_id")
                    file_name = att.get("file_name") or ""
                    if not att_sys_id:
                        continue
                    if not _has_allowed_extension(file_name):
                        files_skipped += 1
                        continue
                    _enqueue_ingestion(
                        {
                            "source": "servicenow_attachment",
                            "workspace_id": workspace_id,
                            "connection_id": connection_id,
                            "instance_url": instance_url,
                            "file": {
                                "attachment_sys_id": att_sys_id,
                                "article_sys_id": sys_id,
                                "file_name": file_name,
                                "content_type": att.get("content_type"),
                                "size_bytes": att.get("size_bytes"),
                            },
                        },
                        sync_id=sync_id,
                    )
                    attachments_synced += 1

            if not has_more:
                break
            offset += ARTICLE_LIST_LIMIT
    except ServiceNowRateLimitedError as e:
        # Chunk 08 alarm hook.
        logger.warning(f"ServiceNow rate limited during sync: {e}")
        failures.append(f"rate_limited:offset:{offset}")
        enumeration_complete = False
    except ValueError as e:
        logger.warning(f"Enumeration failed at offset={offset}: {e}")
        failures.append(f"offset:{offset}")
        enumeration_complete = False

    files_orphaned = 0
    if enumeration_complete:
        try:
            existing_sys_ids = _list_existing_sys_ids(workspace_id, connection_id)
            orphan_sys_ids = existing_sys_ids - current_sys_ids
            files_orphaned = _enqueue_orphan_deletes(
                workspace_id, connection_id, orphan_sys_ids,
            )
        except Exception:
            logger.exception("Orphan cleanup failed (non-fatal)")

    if failures and articles_synced == 0 and files_orphaned == 0:
        status = "failed"
        err = "; ".join(failures)
    elif failures:
        status = "partial_success"
        err = f"Some pages failed: {'; '.join(failures)}"
    else:
        status = "success"
        err = ""

    _update_sync_status(
        workspace_id, connection_id,
        status=status, error=err,
        cursor=new_cursor if enumeration_complete else "",
    )

    logger.info(
        f"Sync done connection={connection_id} articles={articles_synced} "
        f"attachments={attachments_synced} skipped={files_skipped} "
        f"orphans={files_orphaned} status={status}"
    )
    return {
        "status": status,
        "articles": articles_synced,
        "attachments": attachments_synced,
        "orphaned": files_orphaned,
    }


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """SQS partial-batch-failure entry (chunk 02 remediation contract)."""
    records = event.get("Records", []) or []
    logger.info(f"ServiceNow sync invoked with {len(records)} record(s)")

    batch_item_failures: List[Dict[str, str]] = []
    for record in records:
        message_id = record.get("messageId", "")
        try:
            body = json.loads(record["body"])
            result = process_sync_message(body)
            if result.get("status") == "failed":
                # Business-level failure -- retry via SQS so DLQ can
                # accumulate the poison payloads for inspection.
                batch_item_failures.append({"itemIdentifier": message_id})
        except Exception as e:
            logger.exception(f"Failed to process message {message_id}: {e}")
            batch_item_failures.append({"itemIdentifier": message_id})

    logger.info(
        f"ServiceNow sync batch complete: re-delivered={len(batch_item_failures)}"
    )
    return {"batchItemFailures": batch_item_failures}
