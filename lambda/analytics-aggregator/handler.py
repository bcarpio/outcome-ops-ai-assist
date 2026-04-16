"""
Analytics Aggregator Lambda - DynamoDB Streams consumer for audit-logs.

Produces daily roll-ups in the analytics table so the dashboard can answer
summary queries with a bounded number of reads instead of scanning raw audit
rows.

For each audit record with a workspace_id, we emit three atomic ADD updates:

  PK=WS#{workspace_id}, SK=DT#{date}              (day totals)
  PK=WS#{workspace_id}, SK=DM#{date}#{model_id}   (day x model)
  PK=WS#{workspace_id}, SK=DU#{date}#{email}      (day x user)

ADD is idempotent-friendly under DynamoDB Streams retries only when paired
with distinct request IDs. To keep counts correct across retries we track the
set of processed request_ids on each rollup item and skip duplicates.
"""

import logging
import os
import time
from decimal import Decimal
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ANALYTICS_TABLE = os.environ.get("ANALYTICS_TABLE", "")
# Retain aggregate items for 400 days (longer than the 365-day audit TTL so
# full-year trend comparisons do not lose their tail).
TTL_SECONDS = 400 * 24 * 60 * 60

_dynamodb = None


def _get_table():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb.Table(ANALYTICS_TABLE)


def _s(image: Dict[str, Any], key: str, default: str = "") -> str:
    return image.get(key, {}).get("S", default)


def _n(image: Dict[str, Any], key: str, default: int = 0) -> int:
    raw = image.get(key, {}).get("N")
    if raw is None:
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _cost(image: Dict[str, Any]) -> Decimal:
    """Audit logs store cost_usd as a string. Convert to Decimal for ADD."""
    raw = image.get("cost_usd", {}).get("S")
    if raw is None:
        raw = image.get("cost_usd", {}).get("N")
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def _date_from_timestamp(ts: str) -> Optional[str]:
    """Extract YYYY-MM-DD from an ISO 8601 timestamp."""
    if not ts or len(ts) < 10:
        return None
    return ts[:10]


def _apply_update(
    table,
    pk: str,
    sk: str,
    request_id: str,
    queries_inc: int,
    cost_inc: Decimal,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    refusal_inc: int,
    error_inc: int,
    last_active: Optional[str] = None,
) -> None:
    """Apply an atomic ADD update to a rollup item, deduped by request_id.

    The processed_requests string-set guarantees that a given audit record is
    counted at most once per (PK, SK) rollup even if the stream redelivers.
    """
    now = int(time.time())
    update_parts = [
        "queries :qone",
        "cost_usd :cost",
        "input_tokens :inp",
        "output_tokens :out",
        "total_duration_ms :dur",
        "refusal_count :ref",
        "error_count :err",
        "processed_requests :rid",
    ]
    expr_values: Dict[str, Any] = {
        ":qone": queries_inc,
        ":cost": cost_inc,
        ":inp": input_tokens,
        ":out": output_tokens,
        ":dur": duration_ms,
        ":ref": refusal_inc,
        ":err": error_inc,
        ":rid": {request_id},
    }

    set_parts = ["#ttl = :ttl"]
    expr_names = {"#ttl": "ttl"}
    expr_values[":ttl"] = now + TTL_SECONDS

    if last_active:
        # last_active_ts = max(existing, new) via if_not_exists + condition
        # Simpler: just SET unconditionally to new value. Race is benign; max
        # drift is at most the batch window.
        set_parts.append("last_active_ts = :la")
        expr_values[":la"] = last_active

    update_expression = (
        "ADD " + ", ".join(update_parts) + " SET " + ", ".join(set_parts)
    )

    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=update_expression,
            ConditionExpression="NOT contains(processed_requests, :rid_scalar)",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues={
                **expr_values,
                ":rid_scalar": request_id,
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # Already processed this request_id for this rollup; safe to skip.
            return
        raise


def _process_record(record: Dict[str, Any]) -> None:
    if record.get("eventName") != "INSERT":
        return

    new_image = record.get("dynamodb", {}).get("NewImage", {})
    if not new_image:
        return

    workspace_id = _s(new_image, "workspace_id")
    if not workspace_id or workspace_id == "SYSTEM":
        # Skip system/pipeline calls that aren't attributable to a workspace.
        return

    timestamp = _s(new_image, "timestamp")
    date = _date_from_timestamp(timestamp)
    if not date:
        return

    request_id = _s(new_image, "request_id")
    if not request_id:
        return

    user_email = _s(new_image, "user_email").lower()
    model_id = _s(new_image, "model_id") or "unknown"
    status = _s(new_image, "status")

    cost = _cost(new_image)
    input_tokens = _n(new_image, "input_tokens")
    output_tokens = _n(new_image, "output_tokens")
    duration_ms = _n(new_image, "duration_ms")
    refusal_inc = 1 if status == "refusal" else 0
    error_inc = 1 if status == "error" else 0

    table = _get_table()
    pk = f"WS#{workspace_id}"

    # Day totals
    _apply_update(
        table, pk, f"DT#{date}", request_id,
        queries_inc=1, cost_inc=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
        duration_ms=duration_ms,
        refusal_inc=refusal_inc, error_inc=error_inc,
    )

    # Day x model
    _apply_update(
        table, pk, f"DM#{date}#{model_id}", request_id,
        queries_inc=1, cost_inc=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
        duration_ms=duration_ms,
        refusal_inc=refusal_inc, error_inc=error_inc,
    )

    # Day x user (skip when user_email unknown, e.g. system calls)
    if user_email:
        _apply_update(
            table, pk, f"DU#{date}#{user_email}", request_id,
            queries_inc=1, cost_inc=cost,
            input_tokens=input_tokens, output_tokens=output_tokens,
            duration_ms=duration_ms,
            refusal_inc=refusal_inc, error_inc=error_inc,
            last_active=timestamp,
        )


def handler(event, context):
    """Process DynamoDB Stream records and update aggregate rollups."""
    if not ANALYTICS_TABLE:
        logger.warning("[analytics-aggregator] ANALYTICS_TABLE not configured")
        return {"statusCode": 200, "body": "No analytics table configured"}

    processed = 0
    failed = 0
    for record in event.get("Records", []):
        try:
            _process_record(record)
            processed += 1
        except Exception as e:
            failed += 1
            logger.error(f"[analytics-aggregator] Failed to process record: {e}", exc_info=True)

    logger.info(
        f"[analytics-aggregator] processed={processed} failed={failed}"
    )
    return {"statusCode": 200, "body": f"processed={processed} failed={failed}"}
