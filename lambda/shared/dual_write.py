"""shared.dual_write — local write + cross-region replication emit.

Every write that needs to land in BOTH regions under HA goes through
this module instead of calling boto3 directly. In single-region
deployments (REPLICATION_EVENT_BUS_NAME unset), the helpers skip the
emit entirely and behave as a thin pass-through.

Public surface (six helpers):
- ``ddb_put_item(table_name, item, **kwargs)``
- ``ddb_update_item(table_name, key, **kwargs)`` -> dict
- ``ddb_delete_item(table_name, key, **kwargs)``
- ``ddb_batch_delete_items(table_name, keys)`` -> int
- ``s3_put_object(bucket, key, body, **kwargs)``
- ``s3vectors_put_vectors(bucket, index, vectors, **kwargs)``
- ``s3vectors_delete_vectors(bucket, index, keys)``

Each helper performs the local write through boto3 first, then emits
a versioned EventBridge envelope to the local replication bus
describing what was written. The replicator-applier Lambda subscribes
to the local bus in the OTHER region and replays the operation there.

Failure semantics:
- Local write fails -> raise. Caller decides what to do (most callers
  re-raise to SQS for retry).
- Local write succeeds but EventBridge emit fails -> log a warning,
  return normally. The local write is the source of truth for the
  operation completing. EventBridge has its own retry budget; the emit
  failure is generally transient. Worst case: the passive region is
  one operation behind, which the next emit (or an operator replay)
  reconciles.

Idempotency:
- Callers expecting idempotency must use DDB ConditionExpression
  (attribute_not_exists / equality checks). See ``shared.sha_truth``
  for the standard pattern. The applier replays exactly what the
  emitter sent, so conditional writes survive replay correctly.

S3 body handling:
- S3 PutObject events deliberately do NOT carry the body. The applier
  does a cross-region GetObject from the source bucket. This keeps the
  EventBridge payload small and avoids any KMS-encrypted-body
  re-encryption nuance at replay time.

Oversized payloads:
- EventBridge tops out at 256KB per event. We allow 240KB of room for
  the envelope. Anything larger gets sent as a marker-only event
  carrying just the operation + key/bucket; the applier knows to
  refetch from the source. DDB items are capped at 400KB by DDB
  itself, so the marker path is the only way large items replicate.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Optional

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Adaptive retries for DynamoDB: the symbol-graph writer's per-file replace
# (ADR-046) roughly doubles write ops, and a full-repo re-extract fires ~30
# parallel process-batch-summary batches at the on-demand table -- bursts that
# briefly outrun auto-scaling. Adaptive mode client-side-throttles and retries
# ThrottlingException (up to max_attempts) so transient throttles are absorbed
# instead of surfacing as BatchWriteItem failures. Write-then-prune ordering in
# the writer makes a failure that still slips through non-destructive.
_DDB_RETRY_CONFIG = BotoConfig(retries={"mode": "adaptive", "max_attempts": 10})


REPLICATION_EVENT_BUS_NAME = os.environ.get("REPLICATION_EVENT_BUS_NAME", "")
THIS_REGION = os.environ.get("THIS_REGION", os.environ.get("AWS_REGION", ""))
ENVIRONMENT = os.environ.get("ENV", os.environ.get("ENVIRONMENT", ""))
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")


_PAYLOAD_MAX_BYTES = 240_000
_EVENT_SCHEMA_VERSION = 1


_dynamodb = None
_s3 = None
_s3vectors = None
_events = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", config=_DDB_RETRY_CONFIG)
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


def _events_client():
    global _events
    if _events is None:
        _events = boto3.client("events")
    return _events


def _json_default(obj: Any) -> Any:
    """JSON fallback for boto3 types that aren't natively serializable.

    DDB items can carry ``Decimal`` (from boto3.dynamodb.types) and
    ``set`` types; bytes shows up for S3 metadata. Cast to JSON-safe
    primitives so the EventBridge payload is well-formed.
    """

    try:
        from decimal import Decimal

        if isinstance(obj, Decimal):
            if obj % 1 == 0:
                return int(obj)
            return float(obj)
    except ImportError:
        pass
    if isinstance(obj, set):
        return sorted(obj)
    if isinstance(obj, bytes):
        return obj.decode("utf-8", errors="replace")
    raise TypeError(f"Object of type {type(obj).__name__} not JSON serializable")


def _emit(operation: str, **detail) -> None:
    """Best-effort PutEvents to the local replication bus.

    Logs + swallows on error. The local write already happened; callers
    shouldn't fail an operation because the replication emit hiccupped.

    Only the ACTIVE region originates replication. Replication is one-way
    (active -> passive) at any instant; the passive region applies the
    active region's events via the replication-applier (which writes
    directly, not through this module) and must NOT emit events of its
    own. Without this gate a passive-region local write — e.g. the
    ADMIN_MUTATION_BLOCKED_PASSIVE forensic audit row that region_guard
    records for blocked mutation attempts — would replicate back to the
    active region, polluting the audit / analytics tables with
    bidirectional data. Such forensic rows stay local to the region
    where they happened (per ADR-022). is_active_region() returns True
    for single-region deploys (AppConfig env unset), so this is a no-op
    outside HA.
    """

    if not REPLICATION_EVENT_BUS_NAME:
        return

    # Lazy import: shared.active_region pulls in boto3 + AppConfig client
    # construction; keep it off dual_write's import path for callers that
    # never emit. No circular import — active_region doesn't import here.
    from shared.active_region import is_active_region

    if not is_active_region():
        return

    payload = {
        "version": _EVENT_SCHEMA_VERSION,
        "operation": operation,
        "source_region": THIS_REGION,
        "emitted_at": int(time.time()),
        **detail,
    }

    try:
        payload_str = json.dumps(payload, default=_json_default)
    except (TypeError, ValueError) as exc:
        logger.warning(
            "[dual_write] failed to serialize %s payload: %s; sending marker",
            operation,
            exc,
        )
        payload_str = json.dumps(
            {
                "version": _EVENT_SCHEMA_VERSION,
                "operation": operation,
                "source_region": THIS_REGION,
                "emitted_at": int(time.time()),
                "unserializable": True,
                "marker": detail.get("key") or detail.get("bucket"),
            }
        )

    if len(payload_str.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
        logger.warning(
            "[dual_write] %s payload %d bytes exceeds limit; sending marker only",
            operation,
            len(payload_str.encode("utf-8")),
        )
        payload_str = json.dumps(
            {
                "version": _EVENT_SCHEMA_VERSION,
                "operation": operation,
                "source_region": THIS_REGION,
                "emitted_at": int(time.time()),
                "oversized": True,
                "marker": detail.get("key") or detail.get("bucket"),
                "table": detail.get("table"),
                "index": detail.get("index"),
                "bucket": detail.get("bucket"),
            }
        )

    try:
        _events_client().put_events(
            Entries=[
                {
                    "Source": f"{ENVIRONMENT}.{APP_NAME}.ha-replication",
                    "DetailType": operation,
                    "EventBusName": REPLICATION_EVENT_BUS_NAME,
                    "Detail": payload_str,
                }
            ]
        )
    except ClientError as exc:
        logger.warning(
            "[dual_write] event emit failed for %s: %s "
            "(local write succeeded; passive region will lag)",
            operation,
            exc.response.get("Error", {}).get("Code", "unknown"),
        )


def ddb_put_item(table_name: str, item: dict, **kwargs) -> None:
    table = _ddb().Table(table_name)
    table.put_item(Item=item, **kwargs)
    _emit("ddb_put_item", table=table_name, args={"Item": item, **kwargs})


def replicate_put_item(table_name: str, item: dict) -> None:
    """Replicate an already-written item WITHOUT a second local write.

    For callers whose authoritative local write happens through a path
    dual_write can't express as a simple put — e.g. a conditional
    ``update_item``. The passive region applies this as an unconditional
    put, converging its copy to ``item``. Emits only (the local write
    already happened); no-op outside the active region / HA.
    """
    _emit("ddb_put_item", table=table_name, args={"Item": item})


def ddb_batch_put_items(table_name: str, items: list) -> int:
    """Batch-put items via DDB BatchWriteItem and emit one event per item.

    Mirrors the ``table.batch_writer()`` shape used by callers like
    ``shared.symbol_graph_writer``: writes the full set locally with
    automatic 25-item chunking and unprocessed-item retries, then
    emits a replication event per item. EventBridge PutEvents is
    batched 10-per-call (the PutEvents API limit) to stay under the
    account quota for large symbol-graph regens.

    Returns the count of items actually written.
    """

    if not items:
        return 0

    table = _ddb().Table(table_name)
    written = 0
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
            written += 1

    if REPLICATION_EVENT_BUS_NAME:
        _emit_batch_for_items(table_name, items)

    return written


def _emit_batch_for_items(table_name: str, items: list) -> None:
    """Helper: PutEvents-batched (10 per call) emissions for a list of items.

    Each item becomes one ddb_put_item event. Failures are logged and
    swallowed — the local write already succeeded.
    """

    if not REPLICATION_EVENT_BUS_NAME or not items:
        return

    PUT_EVENTS_BATCH = 10
    entries: list = []
    for item in items:
        payload = {
            "version": _EVENT_SCHEMA_VERSION,
            "operation": "ddb_put_item",
            "source_region": THIS_REGION,
            "emitted_at": int(time.time()),
            "table": table_name,
            "args": {"Item": item},
        }
        try:
            payload_str = json.dumps(payload, default=_json_default)
        except (TypeError, ValueError):
            payload_str = json.dumps(
                {
                    "version": _EVENT_SCHEMA_VERSION,
                    "operation": "ddb_put_item",
                    "source_region": THIS_REGION,
                    "emitted_at": int(time.time()),
                    "unserializable": True,
                    "table": table_name,
                }
            )
        if len(payload_str.encode("utf-8")) > _PAYLOAD_MAX_BYTES:
            payload_str = json.dumps(
                {
                    "version": _EVENT_SCHEMA_VERSION,
                    "operation": "ddb_put_item",
                    "source_region": THIS_REGION,
                    "emitted_at": int(time.time()),
                    "oversized": True,
                    "table": table_name,
                }
            )
        entries.append(
            {
                "Source": f"{ENVIRONMENT}.{APP_NAME}.ha-replication",
                "DetailType": "ddb_put_item",
                "EventBusName": REPLICATION_EVENT_BUS_NAME,
                "Detail": payload_str,
            }
        )

    for chunk_start in range(0, len(entries), PUT_EVENTS_BATCH):
        chunk = entries[chunk_start : chunk_start + PUT_EVENTS_BATCH]
        try:
            _events_client().put_events(Entries=chunk)
        except ClientError as exc:
            logger.warning(
                "[dual_write] batch emit failed for %s: %s "
                "(local write succeeded; passive region will lag for %d items)",
                table_name,
                exc.response.get("Error", {}).get("Code", "unknown"),
                len(chunk),
            )


def ddb_batch_delete_items(table_name: str, keys: list) -> int:
    """Batch-delete items by key via DDB BatchWriteItem and emit one
    ddb_delete_item event per key.

    Symmetric with ``ddb_batch_put_items`` so HA replication converges
    deletes as well as puts — without this, a caller that batch-puts
    through this module but deletes through a raw ``batch_writer`` would
    leave the passive region holding rows the active region removed. Each
    ``key`` is a primary-key dict (``{"PK": ..., "SK": ...}``). Returns the
    count of keys deleted.
    """

    if not keys:
        return 0

    table = _ddb().Table(table_name)
    deleted = 0
    with table.batch_writer() as batch:
        for key in keys:
            batch.delete_item(Key=key)
            deleted += 1

    if REPLICATION_EVENT_BUS_NAME:
        _emit_batch_delete_for_keys(table_name, keys)

    return deleted


def _emit_batch_delete_for_keys(table_name: str, keys: list) -> None:
    """Helper: PutEvents-batched (10 per call) ddb_delete_item emissions.

    Mirrors ``_emit_batch_for_items`` for the delete path. Each key becomes
    one ddb_delete_item event. Failures are logged and swallowed — the local
    deletes already succeeded.
    """

    if not REPLICATION_EVENT_BUS_NAME or not keys:
        return

    PUT_EVENTS_BATCH = 10
    entries: list = []
    for key in keys:
        payload = {
            "version": _EVENT_SCHEMA_VERSION,
            "operation": "ddb_delete_item",
            "source_region": THIS_REGION,
            "emitted_at": int(time.time()),
            "table": table_name,
            "key": key,
        }
        try:
            payload_str = json.dumps(payload, default=_json_default)
        except (TypeError, ValueError):
            payload_str = json.dumps(
                {
                    "version": _EVENT_SCHEMA_VERSION,
                    "operation": "ddb_delete_item",
                    "source_region": THIS_REGION,
                    "emitted_at": int(time.time()),
                    "unserializable": True,
                    "table": table_name,
                }
            )
        entries.append(
            {
                "Source": f"{ENVIRONMENT}.{APP_NAME}.ha-replication",
                "DetailType": "ddb_delete_item",
                "EventBusName": REPLICATION_EVENT_BUS_NAME,
                "Detail": payload_str,
            }
        )

    for chunk_start in range(0, len(entries), PUT_EVENTS_BATCH):
        chunk = entries[chunk_start : chunk_start + PUT_EVENTS_BATCH]
        try:
            _events_client().put_events(Entries=chunk)
        except ClientError as exc:
            logger.warning(
                "[dual_write] batch delete emit failed for %s: %s "
                "(local deletes succeeded; passive region will lag for %d keys)",
                table_name,
                exc.response.get("Error", {}).get("Code", "unknown"),
                len(chunk),
            )


def ddb_update_item(table_name: str, key: dict, **kwargs) -> dict:
    table = _ddb().Table(table_name)
    resp = table.update_item(Key=key, **kwargs)
    _emit("ddb_update_item", table=table_name, key=key, args=kwargs)
    return resp


def ddb_delete_item(table_name: str, key: dict, **kwargs) -> None:
    table = _ddb().Table(table_name)
    table.delete_item(Key=key, **kwargs)
    _emit("ddb_delete_item", table=table_name, key=key, args=kwargs)


def s3_put_object(
    bucket: Optional[str] = None,
    key: Optional[str] = None,
    body: Optional[bytes] = None,
    **kwargs,
) -> None:
    """Local S3 PutObject + replication emit.

    Accepts both lowercase keyword args (bucket/key/body) and the boto3
    PascalCase form (Bucket/Key/Body) by popping the PascalCase
    versions out of **kwargs. Callers can therefore pass either shape
    without a translation layer at the call site.
    """

    if "Bucket" in kwargs:
        bucket = kwargs.pop("Bucket")
    if "Key" in kwargs:
        key = kwargs.pop("Key")
    if "Body" in kwargs:
        body = kwargs.pop("Body")
    if bucket is None or key is None or body is None:
        raise TypeError("s3_put_object requires bucket, key, and body")

    _s3_client().put_object(Bucket=bucket, Key=key, Body=body, **kwargs)
    _emit(
        "s3_put_object",
        bucket=bucket,
        key=key,
        args={k: v for k, v in kwargs.items() if k != "Body"},
    )


_S3VECTORS_PUT_CHUNK_SIZE = 500  # AWS S3 Vectors PutVectors hard limit


def s3vectors_put_vectors(
    bucket: str, index: str, vectors: list, **kwargs
) -> None:
    """Write vectors to S3 Vectors + emit one replication event per chunk.

    fable-scale chunk 07: chunks at 500 to respect the AWS PutVectors
    per-call limit. A single call with >500 vectors used to fail with
    ValidationException at the SDK; ingestion handlers now call this
    helper without worrying about chunk size.

    Each chunk emits its own replication event so the passive-region
    applier can play back per-chunk rather than needing to reconstruct
    a giant payload.
    """
    if not vectors:
        return
    client = _s3vectors_client()
    for i in range(0, len(vectors), _S3VECTORS_PUT_CHUNK_SIZE):
        chunk = vectors[i:i + _S3VECTORS_PUT_CHUNK_SIZE]
        client.put_vectors(
            vectorBucketName=bucket,
            indexName=index,
            vectors=chunk,
            **kwargs,
        )
        _emit(
            "s3vectors_put_vectors",
            bucket=bucket,
            index=index,
            args={"vectors": chunk, **kwargs},
        )


def s3vectors_delete_vectors(bucket: str, index: str, keys: list) -> None:
    _s3vectors_client().delete_vectors(
        vectorBucketName=bucket,
        indexName=index,
        keys=keys,
    )
    _emit(
        "s3vectors_delete_vectors",
        bucket=bucket,
        index=index,
        args={"keys": keys},
    )


def _set_clients_for_tests(
    *,
    dynamodb=None,
    s3=None,
    s3vectors=None,
    events=None,
) -> None:
    """Inject stub clients for unit tests."""

    global _dynamodb, _s3, _s3vectors, _events
    if dynamodb is not None:
        _dynamodb = dynamodb
    if s3 is not None:
        _s3 = s3
    if s3vectors is not None:
        _s3vectors = s3vectors
    if events is not None:
        _events = events


def _reset_clients_for_tests() -> None:
    """Clear cached clients so tests start from cold state."""

    global _dynamodb, _s3, _s3vectors, _events
    _dynamodb = None
    _s3 = None
    _s3vectors = None
    _events = None
