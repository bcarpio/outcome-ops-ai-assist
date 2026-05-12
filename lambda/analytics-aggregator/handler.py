"""
Analytics Aggregator Lambda - DynamoDB Streams consumer for audit-logs.

Produces daily roll-ups in the analytics table so the dashboard can answer
summary queries with a bounded number of reads instead of scanning raw audit
rows.

For each Bedrock audit record with a workspace_id, we emit three atomic ADD
updates:

  PK=WS#{workspace_id}, SK=DT#{date}              (day totals - bedrock)
  PK=WS#{workspace_id}, SK=DM#{date}#{model_id}   (day x model)
  PK=WS#{workspace_id}, SK=DU#{date}#{email}      (day x user)

For each admin audit record (category="admin") we emit three more shapes:

  PK=WS#{workspace_id}, SK=DA#{date}               (day total - actions)
  PK=WS#{workspace_id}, SK=DC#{date}#{action}      (day x action type)
  PK=WS#{workspace_id}, SK=DCU#{date}#{email}      (day x user, actions)

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


def aggregate_item(
    table,
    workspace_id: str,
    user_email: str,
    model_id: str,
    cost: Decimal,
    input_tokens: int,
    output_tokens: int,
    duration_ms: int,
    status: str,
    timestamp: str,
    request_id: str,
) -> bool:
    """Apply DT/DM/DU rollup updates for a single audit record.

    Returns True if the record was aggregated, False if it was skipped (for
    example missing workspace, system call, or missing request_id).

    This is the shared entry point used by both the live stream consumer and
    the one-shot backfill script (``scripts/backfill-analytics.py``). The
    ``processed_requests`` dedup guard inside ``_apply_update`` makes repeat
    calls for the same ``request_id`` safe, so the two paths can overlap.
    """
    if not workspace_id or workspace_id == "SYSTEM":
        return False
    date = _date_from_timestamp(timestamp)
    if not date or not request_id:
        return False

    model_id = model_id or "unknown"
    refusal_inc = 1 if status == "refusal" else 0
    error_inc = 1 if status == "error" else 0
    pk = f"WS#{workspace_id}"

    _apply_update(
        table, pk, f"DT#{date}", request_id,
        queries_inc=1, cost_inc=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
        duration_ms=duration_ms,
        refusal_inc=refusal_inc, error_inc=error_inc,
    )
    _apply_update(
        table, pk, f"DM#{date}#{model_id}", request_id,
        queries_inc=1, cost_inc=cost,
        input_tokens=input_tokens, output_tokens=output_tokens,
        duration_ms=duration_ms,
        refusal_inc=refusal_inc, error_inc=error_inc,
    )
    if user_email:
        _apply_update(
            table, pk, f"DU#{date}#{user_email.lower()}", request_id,
            queries_inc=1, cost_inc=cost,
            input_tokens=input_tokens, output_tokens=output_tokens,
            duration_ms=duration_ms,
            refusal_inc=refusal_inc, error_inc=error_inc,
            last_active=timestamp,
        )
    return True


# ============================================================================
# Admin-action rollups (DA / DC / DCU)
# ============================================================================

# Action vocabulary lives in shared/audit_writer.py. Mirror it here as a
# sentinel so the aggregator can warn on unknown values without taking a
# hard dependency on the shared module from a streams consumer that may run
# without sys.path configured for it. Keep in sync with chunk 02.
_KNOWN_ADMIN_ACTIONS = frozenset(
    {
        "WORKSPACE_CREATED",
        "WORKSPACE_UPDATED",
        "WORKSPACE_DELETED",
        "WORKSPACE_BUDGET_UPDATED",
        "WORKSPACE_MEMBER_ADDED",
        "WORKSPACE_MEMBER_REMOVED",
        "WORKSPACE_REPO_ADDED",
        "WORKSPACE_REPO_REMOVED",
        "ORG_MEMBER_ADDED",
        "ORG_MEMBER_REMOVED",
        "MCP_SERVER_CREATED",
        "MCP_SERVER_UPDATED",
        "MCP_SERVER_DELETED",
        "API_KEY_CREATED",
        "API_KEY_REVOKED",
        "SYSTEM_PROMPT_UPDATED",
        "SYSTEM_PROMPT_RESET",
        "PRIVACY_MODE_UPDATED",
        "DOCUMENT_INGESTED",
        # plan/audit-completeness — must mirror shared/audit_writer.ADMIN_ACTIONS
        "LOGIN_SUCCEEDED",
        "API_KEY_USED",
        "MCP_JWT_VALIDATED",
        "REPO_SYNCED",
        "CONFLUENCE_SYNCED",
        "JIRA_SYNCED",
        "OUTLOOK_SYNCED",
        "TEAMS_SYNCED",
        "SHAREPOINT_SYNCED",
        "ONEDRIVE_SYNCED",
        "ONENOTE_SYNCED",
        "DATABASE_SYNCED",
        "OAUTH_CONNECTION_ESTABLISHED",
        "OAUTH_TOKEN_REFRESHED",
        "DB_CONNECTION_ESTABLISHED",
        "DB_CREDENTIAL_USED",
        "DB_CONNECTION_DELETED",
        "DOCUMENT_DELETED",
        "CONVERSATION_DELETED",
        "WORKSPACE_SHARING_UPDATED",
        "MCP_SERVER_ASSIGNED_TO_WORKSPACE",
        "MCP_SERVER_UNASSIGNED_FROM_WORKSPACE",
        "PR_ANALYZED",
        "CODE_GENERATED",
        "CODE_GEN_DLQ_SALVAGE_PR_CREATED",
        "BUDGET_THRESHOLD_CROSSED",
        "DOCUMENT_DOWNLOADED",
        "MCP_TOOL_INVOKED",
        "TESTS_RAN",
        # ADR-019 license-layer regression detector
        "LICENSE_FAIL_OPEN",
    }
)

# Actions whose audit rows are written for forensic / per-document
# provenance but are NOT rolled up into DA / DC / DCU - they would
# dominate the dashboard otherwise. Mirrors
# shared/audit_writer.HIDDEN_DASHBOARD_ACTIONS; kept local so the
# streams consumer doesn't import from the shared module.
_HIDDEN_DASHBOARD_ACTIONS = frozenset({
    "DOCUMENT_INGESTED",
    "API_KEY_USED",
    "MCP_TOOL_INVOKED",
    "LOGIN_SUCCEEDED",
    "REPO_SYNCED",
    "CONFLUENCE_SYNCED",
    "JIRA_SYNCED",
    "OUTLOOK_SYNCED",
    "TEAMS_SYNCED",
    "SHAREPOINT_SYNCED",
    "ONEDRIVE_SYNCED",
    "ONENOTE_SYNCED",
    "DATABASE_SYNCED",
    "DB_CREDENTIAL_USED",
    "DOCUMENT_DELETED",
    "CONVERSATION_DELETED",
    "PR_ANALYZED",
    "TESTS_RAN",
})


def _apply_action_update(
    table,
    pk: str,
    sk: str,
    request_id: str,
    last_action_at: Optional[str] = None,
) -> None:
    """ADD-update an admin-action rollup row, deduped by request_id.

    Mirrors :func:`_apply_update` but writes the smaller set of fields the
    DA / DC / DCU shapes carry.
    """
    now = int(time.time())
    expr_values: Dict[str, Any] = {
        ":one": Decimal("1"),
        ":rid": {request_id},
        ":ttl": now + TTL_SECONDS,
        ":rid_scalar": request_id,
    }
    set_parts = ["#ttl = :ttl"]
    expr_names = {"#ttl": "ttl"}
    if last_action_at:
        set_parts.append("last_action_at = :la")
        expr_values[":la"] = last_action_at

    update_expression = (
        "ADD actions_total :one, processed_requests :rid SET "
        + ", ".join(set_parts)
    )

    try:
        table.update_item(
            Key={"PK": pk, "SK": sk},
            UpdateExpression=update_expression,
            ConditionExpression="NOT contains(processed_requests, :rid_scalar)",
            ExpressionAttributeNames=expr_names,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return
        raise


def aggregate_admin_item(
    table,
    workspace_id: str,
    user_email: str,
    action: str,
    timestamp: str,
    request_id: str,
) -> bool:
    """Apply DA / DC / DCU rollup updates for a single admin audit record.

    Returns True if aggregated, False if skipped (missing workspace, missing
    request_id, or system-level row).
    """
    if not workspace_id or workspace_id == "SYSTEM":
        return False
    date = _date_from_timestamp(timestamp)
    if not date or not request_id:
        return False
    if not action:
        action = "UNKNOWN"
    if action in _HIDDEN_DASHBOARD_ACTIONS:
        # Skip rollup contributions for actions hidden from the dashboard
        # by policy (e.g., DOCUMENT_INGESTED). The audit row itself stays
        # in the audit table for forensics; we just don't let it dominate
        # the dashboard cards.
        return False
    if action not in _KNOWN_ADMIN_ACTIONS:
        # Unknown action still increments DA so the dashboard total doesn't
        # under-count, but log a warning so a typo gets noticed.
        logger.warning(
            f"[analytics-aggregator] unknown admin action '{action}' for "
            f"ws={workspace_id} request_id={request_id}"
        )

    pk = f"WS#{workspace_id}"
    _apply_action_update(table, pk, f"DA#{date}", request_id, last_action_at=timestamp)
    _apply_action_update(table, pk, f"DC#{date}#{action}", request_id, last_action_at=timestamp)
    if user_email:
        _apply_action_update(
            table,
            pk,
            f"DCU#{date}#{user_email.lower()}",
            request_id,
            last_action_at=timestamp,
        )

    logger.info(
        f"[analytics-aggregator] admin action {action} ws={workspace_id} "
        f"user={user_email or '-'}"
    )
    return True


def _legacy_bedrock_category(image: Dict[str, Any]) -> Optional[str]:
    """Pre-existing rows lack 'category'; treat presence of cost_usd as the
    bedrock signal. After a 30-day soak, this fallback can be removed."""
    cost = image.get("cost_usd")
    if cost and ("S" in cost or "N" in cost):
        return "bedrock"
    return None


def _process_record(record: Dict[str, Any]) -> None:
    if record.get("eventName") != "INSERT":
        return

    new_image = record.get("dynamodb", {}).get("NewImage", {})
    if not new_image:
        return

    category = _s(new_image, "category") or _legacy_bedrock_category(new_image)

    if category == "bedrock":
        aggregate_item(
            table=_get_table(),
            workspace_id=_s(new_image, "workspace_id"),
            user_email=_s(new_image, "user_email"),
            model_id=_s(new_image, "model_id"),
            cost=_cost(new_image),
            input_tokens=_n(new_image, "input_tokens"),
            output_tokens=_n(new_image, "output_tokens"),
            duration_ms=_n(new_image, "duration_ms"),
            status=_s(new_image, "status"),
            timestamp=_s(new_image, "timestamp"),
            request_id=_s(new_image, "request_id"),
        )
    elif category == "admin":
        aggregate_admin_item(
            table=_get_table(),
            workspace_id=_s(new_image, "workspace_id"),
            user_email=_s(new_image, "user_email"),
            action=_s(new_image, "action"),
            timestamp=_s(new_image, "timestamp"),
            request_id=_s(new_image, "request_id"),
        )
    # auth: reserved for future


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
