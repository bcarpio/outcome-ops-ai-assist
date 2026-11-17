# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Analytics Cost Alert Lambda.

Triggered once per day by EventBridge. Reads month-to-date spend from the
analytics aggregates table and publishes an SNS notification the first time
the org-wide or a per-workspace budget threshold is crossed in the current
calendar month.

Alert de-dup is stored as items in the analytics table with keys:

  PK=ALERT, SK=MTD#{YYYY-MM}#ORG
  PK=ALERT, SK=MTD#{YYYY-MM}#WS#{workspace_id}

Conditional writes guarantee exactly one alert per (target, month) regardless
of how many times the Lambda is invoked.
"""

import logging
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Iterable, List, Tuple

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import (  # noqa: E402
    ACTION_BUDGET_THRESHOLD_CROSSED,
    log_admin_action,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ANALYTICS_TABLE = os.environ.get("ANALYTICS_TABLE", "")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE", "")
SNS_TOPIC_ARN = os.environ.get("SNS_TOPIC_ARN", "")
ORG_MONTHLY_USD = float(os.environ.get("ORG_MONTHLY_USD", "0") or 0)
WORKSPACE_MONTHLY_USD = float(os.environ.get("WORKSPACE_MONTHLY_USD", "0") or 0)
ENV = os.environ.get("ENV", "unknown")
# Keep alert-state items around for 2 years so the dedup record survives well
# past each month's reset.
ALERT_TTL_SECONDS = 2 * 365 * 24 * 60 * 60

_dynamodb = None
_sns = None


def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _sns_client():
    global _sns
    if _sns is None:
        _sns = boto3.client("sns")
    return _sns


def _month_bounds(now: datetime) -> Tuple[str, str, str]:
    """Return (month_key, start_date, end_date) for the calendar month of ``now``."""
    month_key = now.strftime("%Y-%m")
    start_date = now.strftime("%Y-%m-01")
    end_date = now.strftime("%Y-%m-%d")
    return month_key, start_date, end_date


def _iter_workspaces(ws_table) -> Iterable[Tuple[str, str, float]]:
    """Yield (workspace_id, name, per_workspace_budget) for each workspace.

    ``per_workspace_budget`` is 0.0 when the workspace has no override; the
    caller falls back to ``WORKSPACE_MONTHLY_USD`` in that case.
    """
    scan_kwargs = {
        "FilterExpression": "SK = :sk AND begins_with(PK, :pkp)",
        "ExpressionAttributeValues": {":sk": "METADATA", ":pkp": "WS#"},
        "ProjectionExpression": "workspace_id, #n, monthly_budget_usd",
        "ExpressionAttributeNames": {"#n": "name"},
    }
    while True:
        resp = ws_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            wid = item.get("workspace_id")
            if not wid:
                continue
            raw_budget = item.get("monthly_budget_usd")
            try:
                budget = float(raw_budget) if raw_budget is not None else 0.0
            except (TypeError, ValueError):
                budget = 0.0
            yield wid, (item.get("name") or wid), budget
        if "LastEvaluatedKey" not in resp:
            return
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _mtd_cost(analytics_table, workspace_id: str, start_date: str, end_date: str) -> float:
    """Sum DT# rollup cost for the month-to-date window."""
    total = Decimal("0")
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND SK BETWEEN :s AND :e",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":s": f"DT#{start_date}",
            ":e": f"DT#{end_date}~",
        },
        "ProjectionExpression": "cost_usd",
    }
    while True:
        resp = analytics_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            raw = item.get("cost_usd", 0)
            try:
                total += Decimal(str(raw))
            except Exception:
                continue
        if "LastEvaluatedKey" not in resp:
            break
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return float(total)


def _claim_alert(analytics_table, sk: str, amount: float, threshold: float) -> bool:
    """Atomically record that an alert has been sent for (target, month).

    Returns True if this invocation won the race and should publish the alert.
    Returns False when a prior invocation has already published.
    """
    ttl = int(time.time()) + ALERT_TTL_SECONDS
    try:
        analytics_table.put_item(
            Item={
                "PK": "ALERT",
                "SK": sk,
                "amount_usd": Decimal(str(round(amount, 4))),
                "threshold_usd": Decimal(str(round(threshold, 4))),
                "alerted_at": datetime.now(timezone.utc).isoformat(),
                "ttl": ttl,
            },
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _publish(subject: str, message: str) -> None:
    if not SNS_TOPIC_ARN:
        logger.warning("[cost-alert] SNS_TOPIC_ARN not configured; would have sent: %s", subject)
        return
    _sns_client().publish(
        TopicArn=SNS_TOPIC_ARN,
        Subject=subject[:100],
        Message=message,
    )


def _emit_threshold_audit(
    scope: str,
    threshold_usd: float,
    mtd_spend_usd: float,
    workspace_id: str = "",
    workspace_name: str = "",
    source: str = "",
) -> None:
    """Emit ACTION_BUDGET_THRESHOLD_CROSSED. Non-fatal."""
    audit_table = os.environ.get("AUDIT_TABLE", "")
    if not audit_table:
        return
    try:
        details = {
            "scope": scope,
            "threshold_usd": str(round(threshold_usd, 2)),
            "mtd_spend_usd": str(round(mtd_spend_usd, 2)),
        }
        if workspace_id:
            details["workspace_name"] = workspace_name
            details["threshold_source"] = source
        log_admin_action(
            audit_table,
            ACTION_BUDGET_THRESHOLD_CROSSED,
            workspace_id=workspace_id,
            user_email="",  # cron-driven, no user
            source_ip="",
            user_agent="",
            details=details,
        )
    except Exception as e:
        logger.warning("[cost-alert] Failed to emit BUDGET_THRESHOLD_CROSSED: %s", e)


def handler(event, context):
    # only the active region sends daily cost-threshold
    # SNS alerts. Without this gate, customers would receive doubled
    # daily emails because both regions' EventBridge crons fire and
    # both regions' analytics tables hold replicated cost data.
    try:
        from shared.active_region import is_active_region  # type: ignore

        if not is_active_region():
            logger.info(
                "[cost-alert] region is passive; skipping daily-roll-up alert"
            )
            return {"statusCode": 200, "body": "passive-region-skip"}
    except ImportError:
        pass

    if not ANALYTICS_TABLE or not WORKSPACES_TABLE:
        logger.warning("[cost-alert] Tables not configured; skipping")
        return {"statusCode": 200, "body": "Tables not configured"}

    # Note: per-workspace overrides may exist even when the global default is
    # disabled, so don't short-circuit on WORKSPACE_MONTHLY_USD alone here.
    # The org check is purely on the global tfvar, so we still bail when both
    # the global and the org threshold are zero AND no per-workspace overrides
    # are set — but checking that requires a scan, so we just enter the loop.

    now = datetime.now(timezone.utc)
    month_key, start_date, end_date = _month_bounds(now)
    logger.info(
        "[cost-alert] evaluating month=%s start=%s end=%s org_threshold=%.2f ws_threshold=%.2f",
        month_key, start_date, end_date, ORG_MONTHLY_USD, WORKSPACE_MONTHLY_USD,
    )

    analytics_table = _ddb().Table(ANALYTICS_TABLE)
    ws_table = _ddb().Table(WORKSPACES_TABLE)

    alerts_sent: List[str] = []
    org_total = 0.0
    workspaces = list(_iter_workspaces(ws_table))

    if not workspaces:
        logger.info("[cost-alert] no workspaces; nothing to do")
        return {"statusCode": 200, "body": "no workspaces"}

    for wid, wname, ws_override in workspaces:
        cost = _mtd_cost(analytics_table, wid, start_date, end_date)
        org_total += cost

        # Per-workspace override wins over the org-wide default. A 0 / unset
        # override falls through to the default, which itself may be 0
        # (meaning per-workspace alerts are disabled for this workspace).
        threshold = ws_override if ws_override > 0 else WORKSPACE_MONTHLY_USD
        if threshold > 0 and cost >= threshold:
            sk = f"MTD#{month_key}#WS#{wid}"
            if _claim_alert(analytics_table, sk, cost, threshold):
                source = "workspace override" if ws_override > 0 else "org default"
                subject = f"[{ENV.upper()}] Workspace budget crossed: {wname}"
                message = (
                    f"Workspace Budget Alert\n"
                    f"{'=' * 50}\n\n"
                    f"Environment: {ENV}\n"
                    f"Workspace:   {wname} ({wid})\n"
                    f"Month:       {month_key}\n"
                    f"Spend (MTD): ${cost:.2f}\n"
                    f"Threshold:   ${threshold:.2f} ({source})\n"
                )
                _publish(subject, message)
                alerts_sent.append(sk)
                _emit_threshold_audit(
                    scope="workspace",
                    threshold_usd=threshold,
                    mtd_spend_usd=cost,
                    workspace_id=wid,
                    workspace_name=wname,
                    source=source,
                )
                logger.info(
                    "[cost-alert] published workspace alert for %s (%s) threshold=$%.2f source=%s",
                    wname, wid, threshold, source,
                )

    if ORG_MONTHLY_USD > 0 and org_total >= ORG_MONTHLY_USD:
        sk = f"MTD#{month_key}#ORG"
        if _claim_alert(analytics_table, sk, org_total, ORG_MONTHLY_USD):
            subject = f"[{ENV.upper()}] Organization budget crossed"
            message = (
                f"Organization Budget Alert\n"
                f"{'=' * 50}\n\n"
                f"Environment: {ENV}\n"
                f"Month:       {month_key}\n"
                f"Spend (MTD): ${org_total:.2f}\n"
                f"Threshold:   ${ORG_MONTHLY_USD:.2f}\n"
                f"Workspaces:  {len(workspaces)}\n"
            )
            _publish(subject, message)
            alerts_sent.append(sk)
            _emit_threshold_audit(
                scope="org",
                threshold_usd=ORG_MONTHLY_USD,
                mtd_spend_usd=org_total,
            )
            logger.info("[cost-alert] published org alert ($%.2f)", org_total)

    logger.info(
        "[cost-alert] done workspaces=%d org_total=%.2f alerts_sent=%d",
        len(workspaces), org_total, len(alerts_sent),
    )
    return {
        "statusCode": 200,
        "body": f"alerts={len(alerts_sent)}",
    }
