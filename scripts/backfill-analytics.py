#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Backfill analytics aggregates from existing audit-log rows.

The analytics-aggregator Lambda only sees audit records written after it is
deployed (DynamoDB stream starting_position=LATEST). This script replays
historical audit rows through the same rollup logic so a freshly-deployed
dashboard shows useful numbers immediately.

Usage:
    source venv/bin/activate
    python scripts/backfill-analytics.py --env prd --days 30
    python scripts/backfill-analytics.py --env prd --days 30 --dry-run

The script is safe to re-run: each rollup item's ``processed_requests`` set
guards against double-counting a given audit ``request_id``, and the same
guard also makes it safe to run while the live stream consumer is active.
"""

import argparse
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, Iterator

import boto3

# Make the analytics-aggregator module importable so we reuse the exact same
# rollup logic the live stream consumer runs. Drift between the two paths is
# the failure mode we most want to avoid.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "lambda", "analytics-aggregator"))

import handler as aggregator  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill-analytics")


# The fields we aggregate. input_text / output_text are intentionally excluded
# (they can be up to 50KB each and we never use them for rollups).
PROJECTION_FIELDS = [
    "PK", "SK", "workspace_id", "user_email", "model_id",
    "cost_usd", "input_tokens", "output_tokens",
    "duration_ms", "status", "timestamp", "request_id",
]


def _resolve_table_name(ssm, env: str, app_name: str, which: str) -> str:
    """Look up a table name from SSM using the same parameters Terraform writes."""
    param = f"/{env}/{app_name}/dynamodb/{which}"
    return ssm.get_parameter(Name=param)["Parameter"]["Value"]


def _iter_workspace_ids(ws_table) -> Iterator[str]:
    """Scan the workspaces table for METADATA items and yield each workspace_id."""
    scan_kwargs = {
        "FilterExpression": "SK = :sk AND begins_with(PK, :pk_prefix)",
        "ExpressionAttributeValues": {":sk": "METADATA", ":pk_prefix": "WS#"},
        "ProjectionExpression": "workspace_id",
    }
    while True:
        resp = ws_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            wid = item.get("workspace_id")
            if wid:
                yield wid
        if "LastEvaluatedKey" not in resp:
            return
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _iter_audit_items(audit_table, workspace_id: str, start_iso: str, end_iso: str) -> Iterator[Dict]:
    """Yield audit items for a workspace within [start_iso, end_iso]."""
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND SK BETWEEN :s AND :e",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":s": start_iso,
            ":e": end_iso + "~",  # ~ sorts above any timestamp suffix char
        },
        "ProjectionExpression": ", ".join(f"#{f}" for f in PROJECTION_FIELDS),
        "ExpressionAttributeNames": {f"#{f}": f for f in PROJECTION_FIELDS},
    }
    while True:
        resp = audit_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            yield item
        if "LastEvaluatedKey" not in resp:
            return
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _coerce_cost(raw) -> Decimal:
    if raw is None:
        return Decimal("0")
    try:
        return Decimal(str(raw))
    except Exception:
        return Decimal("0")


def _coerce_int(raw) -> int:
    if raw is None:
        return 0
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def backfill(env: str, app_name: str, days: int, dry_run: bool) -> int:
    session = boto3.session.Session()
    ssm = session.client("ssm")
    dynamodb = session.resource("dynamodb")

    audit_table_name = _resolve_table_name(ssm, env, app_name, "audit-logs-table")
    analytics_table_name = _resolve_table_name(ssm, env, app_name, "analytics-table")
    workspaces_table_name = _resolve_table_name(ssm, env, app_name, "workspaces-table")

    log.info("audit table:      %s", audit_table_name)
    log.info("analytics table:  %s", analytics_table_name)
    log.info("workspaces table: %s", workspaces_table_name)

    audit_table = dynamodb.Table(audit_table_name)
    analytics_table = dynamodb.Table(analytics_table_name)
    workspaces_table = dynamodb.Table(workspaces_table_name)

    now = datetime.now(timezone.utc)
    start_iso = (now - timedelta(days=days)).isoformat()
    end_iso = now.isoformat()
    log.info("range: %s  ->  %s  (%d days)", start_iso, end_iso, days)

    scanned = 0
    aggregated = 0
    skipped = 0

    for ws_id in _iter_workspace_ids(workspaces_table):
        ws_scanned = 0
        ws_aggregated = 0
        for item in _iter_audit_items(audit_table, ws_id, start_iso, end_iso):
            scanned += 1
            ws_scanned += 1
            if dry_run:
                ws_aggregated += 1
                aggregated += 1
                continue
            ok = aggregator.aggregate_item(
                table=analytics_table,
                workspace_id=item.get("workspace_id", ""),
                user_email=item.get("user_email", ""),
                model_id=item.get("model_id", ""),
                cost=_coerce_cost(item.get("cost_usd")),
                input_tokens=_coerce_int(item.get("input_tokens")),
                output_tokens=_coerce_int(item.get("output_tokens")),
                duration_ms=_coerce_int(item.get("duration_ms")),
                status=item.get("status", ""),
                timestamp=item.get("timestamp", ""),
                request_id=item.get("request_id", ""),
            )
            if ok:
                ws_aggregated += 1
                aggregated += 1
            else:
                skipped += 1
        if ws_scanned:
            log.info("workspace %s: scanned=%d aggregated=%d", ws_id, ws_scanned, ws_aggregated)

    log.info(
        "done  scanned=%d aggregated=%d skipped=%d%s",
        scanned, aggregated, skipped, "  (dry-run)" if dry_run else "",
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default=os.environ.get("ENV", "prd"),
                        help="Deployment environment (dev|prd). Default: prd")
    parser.add_argument("--app-name", default=os.environ.get("APP_NAME", "outcome-ops-ai-assist"),
                        help="App name used in SSM parameter paths")
    parser.add_argument("--days", type=int, default=30,
                        help="How many days of history to backfill. Default: 30")
    parser.add_argument("--dry-run", action="store_true",
                        help="Scan and count without writing to the analytics table")
    args = parser.parse_args()

    if args.days < 1 or args.days > 365:
        log.error("--days must be between 1 and 365")
        return 2

    return backfill(args.env, args.app_name, args.days, args.dry_run)


if __name__ == "__main__":
    sys.exit(main())
