"""Replicator-applier — consume cross-region replication events and
apply them against local DDB / S3 / S3 Vectors.

Subscribes to this region's EventBridge replication bus. Each event
describes a single write that happened in the OTHER region; this
handler replays the operation locally so both regions converge.

Idempotency is delegated to whatever ConditionExpression the original
writer used. Most platform writers go through helpers that already
include attribute_exists / attribute_not_exists guards (see
``shared.sha_truth``); those naturally dedupe on EventBridge retry.
For writers that don't have a natural guard (audit-log put-items),
chunk 06 adds a synthetic guard via emitted_at on the SK so replays
collide on uniqueness.

Failure modes:
- ConditionalCheckFailedException -> already applied or superseded; OK,
  log info, return.
- ClientError (throttle / 5xx) -> raise so EventBridge retries.
- ValueError / KeyError on event shape -> log error, return (event
  hits the DLQ via the rule's retry policy).
- Self-region events -> short-circuit; we never apply our OWN events.
"""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Callable, Dict

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# ---------------------------------------------------------------------------
# Replication ordering guard
#
# EventBridge delivery is at-least-once and NOT ordered: two writes that
# fire close together in the source region can land here out of order,
# and an unconditional put/update lets the OLDER event win, overwriting
# the newer state. Each apply now stamps the row with the source's
# ``emitted_at`` and conditions itself on "no prior stamp OR existing
# stamp older than ours", so a late-arriving older event is rejected as
# ConditionalCheckFailed and handled by the existing ``already_applied``
# branch.
#
# Active-region local writes don't carry this marker; the first
# replicated event after such a write installs it. After failover, the
# new-active's writes also start without the marker; the new-passive's
# applier re-installs it on the next event. Wall-clock skew across
# regions is sub-second in practice and well below typical write
# spacing on the same row.
# ---------------------------------------------------------------------------
_REPL_TS_ATTR = "_repl_emitted_at"
_REPL_NAME_KEY = "#__repl_ts__"
_REPL_VALUE_KEY = ":__repl_ts__"
_REPL_COND_EXPR = (
    f"attribute_not_exists({_REPL_NAME_KEY}) OR "
    f"{_REPL_NAME_KEY} < {_REPL_VALUE_KEY}"
)
_REPL_SET_ACTION = f"{_REPL_NAME_KEY} = {_REPL_VALUE_KEY}"
_UPDATE_KEYWORDS_RE = re.compile(r"\b(SET|REMOVE|ADD|DELETE)\b")


def _splice_set_action(expr: str, action: str) -> str:
    """Insert ``action`` into a DDB ``UpdateExpression``'s SET clause.

    DDB expressions chain at most one of each (SET / REMOVE / ADD /
    DELETE) clause, so we splice into the existing SET if present and
    otherwise prepend a fresh SET. Used by the applier to add its own
    ``_repl_emitted_at`` marker without disturbing the source update.
    """

    if not expr or not expr.strip():
        return f"SET {action}"
    m = re.search(r"\bSET\b", expr)
    if not m:
        return f"SET {action} {expr}".strip()
    end_of_set = len(expr)
    for m2 in _UPDATE_KEYWORDS_RE.finditer(expr, m.end()):
        if m2.group() != "SET":
            end_of_set = m2.start()
            break
    before = expr[:end_of_set].rstrip()
    after = expr[end_of_set:].lstrip()
    suffix = f" {after}" if after else ""
    return f"{before}, {action}{suffix}"


def _emitted_at(detail: dict) -> int:
    try:
        return int(detail.get("emitted_at") or 0)
    except (TypeError, ValueError):
        return 0


THIS_REGION = os.environ.get(
    "THIS_REGION", os.environ.get("AWS_REGION", "")
)


_dynamodb = None
_s3 = None
_s3vectors = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3")
    return _s3


def _s3vectors_client():
    global _s3vectors
    if _s3vectors is None:
        _s3vectors = boto3.client("s3vectors")
    return _s3vectors


def _set_clients_for_tests(
    *, dynamodb=None, s3=None, s3vectors=None
) -> None:
    global _dynamodb, _s3, _s3vectors
    if dynamodb is not None:
        _dynamodb = dynamodb
    if s3 is not None:
        _s3 = s3
    if s3vectors is not None:
        _s3vectors = s3vectors


def _reset_clients_for_tests() -> None:
    global _dynamodb, _s3, _s3vectors
    _dynamodb = None
    _s3 = None
    _s3vectors = None


def lambda_handler(event: dict, context: Any) -> dict:
    """EventBridge entry point. One event per invocation."""

    detail = event.get("detail") or {}
    version = detail.get("version")
    if version != 1:
        logger.error("[applier] unknown event version: %s", version)
        return {"status": "skipped", "reason": "unknown_version"}

    source_region = detail.get("source_region")
    if source_region and source_region == THIS_REGION:
        # Defense in depth: don't replay our own emissions even if the
        # EventBridge rule pattern lets one slip through.
        logger.info(
            "[applier] short-circuit self-event from %s", source_region
        )
        return {"status": "skipped", "reason": "self_event"}

    if detail.get("oversized") or detail.get("unserializable"):
        # Marker-only events tell us a write happened but the payload
        # didn't fit. For DDB items there's nothing to do until the next
        # full event for that key arrives; for S3 we COULD cross-region
        # refetch by key, but the marker doesn't always carry enough
        # context. Today: log + drop. Operator replays by triggering a
        # fresh write upstream.
        logger.warning(
            "[applier] marker-only event for %s; skipping. marker=%s",
            detail.get("operation"),
            detail.get("marker"),
        )
        return {"status": "skipped", "reason": "marker_only"}

    operation = detail.get("operation")
    dispatcher = _DISPATCH.get(operation)
    if dispatcher is None:
        logger.error("[applier] unknown operation: %s", operation)
        return {"status": "skipped", "reason": "unknown_operation"}

    try:
        dispatcher(detail)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.info(
                "[applier] %s skipped (already applied): key=%s",
                operation,
                detail.get("key") or detail.get("bucket"),
            )
            return {"status": "already_applied", "operation": operation}
        # Transient / unknown — propagate so EventBridge retries.
        raise

    return {"status": "applied", "operation": operation}


def _apply_ddb_put_item(detail: dict) -> None:
    table_name = detail["table"]
    args = dict(detail.get("args") or {})
    if "Item" not in args:
        raise ValueError("ddb_put_item event missing Item")
    ts = _emitted_at(detail)
    item = dict(args["Item"])
    item[_REPL_TS_ATTR] = ts
    args["Item"] = item
    # The applier owns ordering; replace any source-side condition.
    args["ConditionExpression"] = _REPL_COND_EXPR
    args["ExpressionAttributeNames"] = {
        **(args.get("ExpressionAttributeNames") or {}),
        _REPL_NAME_KEY: _REPL_TS_ATTR,
    }
    args["ExpressionAttributeValues"] = {
        **(args.get("ExpressionAttributeValues") or {}),
        _REPL_VALUE_KEY: ts,
    }
    _ddb().Table(table_name).put_item(**args)


def _apply_ddb_update_item(detail: dict) -> None:
    # update_item is partial and the row-level _repl_emitted_at marker
    # introduced for put_item cannot be applied here: different events
    # legitimately touch DIFFERENT attributes of the same row, so a
    # newer event setting one attribute (and bumping the marker) would
    # cause an older event updating a DIFFERENT attribute to be
    # incorrectly rejected as already_applied. A live failback found
    # this on the workspaces GITHUB_REPO# rows -- a later sync-status
    # update advanced the marker, then earlier last_code_map_sha /
    # last_docs_sha updates got persistently rejected.
    #
    # The original same-attribute 21 ms race that motivated the guard
    # returns for update_item; that bug is self-healing (next write
    # rolls it forward) where this introduced bug was not. The guard
    # remains on put_item and delete_item, which are whole-row.
    table_name = detail["table"]
    key = detail["key"]
    args = dict(detail.get("args") or {})
    _ddb().Table(table_name).update_item(Key=key, **args)


def _apply_ddb_delete_item(detail: dict) -> None:
    table_name = detail["table"]
    key = detail["key"]
    args = dict(detail.get("args") or {})
    ts = _emitted_at(detail)
    args["ConditionExpression"] = _REPL_COND_EXPR
    args["ExpressionAttributeNames"] = {
        **(args.get("ExpressionAttributeNames") or {}),
        _REPL_NAME_KEY: _REPL_TS_ATTR,
    }
    args["ExpressionAttributeValues"] = {
        **(args.get("ExpressionAttributeValues") or {}),
        _REPL_VALUE_KEY: ts,
    }
    _ddb().Table(table_name).delete_item(Key=key, **args)


def _apply_s3_put_object(detail: dict) -> None:
    bucket = detail["bucket"]
    key = detail["key"]
    source_region = detail.get("source_region")
    if not source_region:
        raise ValueError("s3_put_object event missing source_region")

    # Cross-region GetObject from the SOURCE region's bucket. Note: the
    # source bucket name in the event is the SOURCE-region bucket name,
    # which (per chunk 02e) differs from this region's bucket name by a
    # "-secondary" suffix on whichever is the secondary. The emitter
    # records its own region's bucket name in `bucket`; the local
    # mirror is resolved from the env-var SSM path.
    source_s3 = boto3.client("s3", region_name=source_region)
    body = source_s3.get_object(Bucket=bucket, Key=key)["Body"].read()

    local_bucket = os.environ.get("LOCAL_KB_BUCKET", "")
    if not local_bucket:
        raise RuntimeError("LOCAL_KB_BUCKET env var not configured")

    args = {k: v for k, v in (detail.get("args") or {}).items() if k != "Body"}
    _s3_client().put_object(Bucket=local_bucket, Key=key, Body=body, **args)


def _apply_s3vectors_put_vectors(detail: dict) -> None:
    bucket = detail["bucket"]
    index = detail["index"]
    args = detail.get("args") or {}
    # The emitter's bucket name is regional, but s3vectors bucket names
    # are unique PER REGION and identical across our regions (per chunk
    # 02e), so we can use the source bucket name as the local bucket
    # name directly.
    _s3vectors_client().put_vectors(
        vectorBucketName=bucket,
        indexName=index,
        vectors=args.get("vectors", []),
    )


def _apply_s3vectors_delete_vectors(detail: dict) -> None:
    bucket = detail["bucket"]
    index = detail["index"]
    args = detail.get("args") or {}
    _s3vectors_client().delete_vectors(
        vectorBucketName=bucket,
        indexName=index,
        keys=args.get("keys", []),
    )


_DISPATCH: Dict[str, Callable[[dict], None]] = {
    "ddb_put_item": _apply_ddb_put_item,
    "ddb_update_item": _apply_ddb_update_item,
    "ddb_delete_item": _apply_ddb_delete_item,
    "s3_put_object": _apply_s3_put_object,
    "s3vectors_put_vectors": _apply_s3vectors_put_vectors,
    "s3vectors_delete_vectors": _apply_s3vectors_delete_vectors,
}
