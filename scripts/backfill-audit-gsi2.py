#!/usr/bin/env python3
"""Backfill GSI2PK/GSI2SK on audit rows that pre-date the GSI2 commit.

The audit table got a new GSI2 (CAT#<category> partition) so the dashboard
can answer "all admin actions org-wide" with a single Query instead of a
Scan + FilterExpression. New writes from the redeployed Lambdas already
carry GSI2PK and GSI2SK; rows written before the redeploy don't, so they
are invisible to the GSI2 Query.

This script reads the audit table for a recent time window, infers the
category from the row shape (existing ``category`` attribute first, then
``action`` => admin, ``model_id``/``cost_usd`` => bedrock), and adds the
GSI2 keys + a ``category`` attribute via UpdateItem. The update is
idempotent: a ConditionExpression on ``attribute_not_exists(GSI2PK)``
guarantees a re-run is a no-op.

Usage:
    source venv/bin/activate
    python scripts/backfill-audit-gsi2.py --env prd --hours 24 --dry-run
    python scripts/backfill-audit-gsi2.py --env prd --hours 24 --apply

Defaults to dry-run; --apply is required for any writes.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill-audit-gsi2")


def _resolve_table_name(ssm, env: str, app_name: str) -> str:
    param = f"/{env}/{app_name}/dynamodb/audit-logs-table"
    return ssm.get_parameter(Name=param)["Parameter"]["Value"]


def _infer_category(item: Dict) -> Optional[str]:
    """Return the category for a pre-GSI2 row, or None if the shape is
    unrecognized (we'd rather skip than mis-categorize)."""
    existing = item.get("category")
    if existing:
        return str(existing)
    # Old _write_audit_log rows: have action set, no category. Admin shape.
    if item.get("action"):
        return "admin"
    # Bedrock rows from shared/audit.py pre-GSI2: model_id and cost_usd.
    if item.get("model_id") or item.get("cost_usd") is not None or item.get("lambda_name"):
        return "bedrock"
    return None


def _iter_audit_rows_missing_gsi2(audit_table, start_iso: str) -> Iterator[Dict]:
    """Scan the audit table for rows in the time window that lack GSI2PK.

    A Scan is fine here because the audit table is small for a single
    customer's ~24h window and we do this exactly once per deploy.
    """
    scan_kwargs = {
        "FilterExpression": (
            "#ts >= :s AND attribute_not_exists(GSI2PK)"
        ),
        "ExpressionAttributeNames": {"#ts": "timestamp"},
        "ExpressionAttributeValues": {":s": start_iso},
    }
    while True:
        resp = audit_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            yield item
        if "LastEvaluatedKey" not in resp:
            return
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _update_gsi2(audit_table, item: Dict, category: str) -> bool:
    """Set GSI2PK / GSI2SK / category on a single item. Returns True on
    write, False if the conditional check failed (already backfilled).
    Other ClientErrors propagate."""
    try:
        audit_table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression=(
                "SET GSI2PK = :pk, GSI2SK = :sk, "
                "#cat = if_not_exists(#cat, :cat)"
            ),
            ConditionExpression="attribute_not_exists(GSI2PK)",
            ExpressionAttributeNames={"#cat": "category"},
            ExpressionAttributeValues={
                ":pk": f"CAT#{category}",
                ":sk": item["SK"],
                ":cat": category,
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def backfill(env: str, app_name: str, hours: int, apply: bool) -> Tuple[int, int, int, Dict[str, int]]:
    session = boto3.session.Session()
    ssm = session.client("ssm")
    dynamodb = session.resource("dynamodb")

    audit_table_name = _resolve_table_name(ssm, env, app_name)
    log.info("audit table: %s", audit_table_name)

    audit_table = dynamodb.Table(audit_table_name)

    start_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    log.info("scanning rows with timestamp >= %s (last %dh)", start_iso, hours)
    log.info("mode: %s", "APPLY (writes enabled)" if apply else "dry-run")

    scanned = 0
    written = 0
    skipped = 0
    by_cat: Dict[str, int] = {}
    skipped_shapes = []

    for item in _iter_audit_rows_missing_gsi2(audit_table, start_iso):
        scanned += 1
        cat = _infer_category(item)
        if cat is None:
            skipped += 1
            skipped_shapes.append(sorted(item.keys()))
            continue
        by_cat[cat] = by_cat.get(cat, 0) + 1

        if not apply:
            log.info(
                "[dry-run] would set GSI2PK=CAT#%s on PK=%s SK=%s "
                "(action=%s lambda=%s)",
                cat,
                item.get("PK", ""),
                item.get("SK", "")[:30],
                item.get("action", "-"),
                item.get("lambda_name", "-"),
            )
            continue

        try:
            if _update_gsi2(audit_table, item, cat):
                written += 1
            else:
                # Already backfilled (e.g., re-run); count as skip.
                skipped += 1
        except ClientError as e:
            log.error(
                "update failed for PK=%s SK=%s: %s",
                item.get("PK"), item.get("SK"), e,
            )
            skipped += 1

    return scanned, written, skipped, by_cat


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", help="Environment (dev/prd)")
    parser.add_argument("--app-name", default="outcome-ops-ai-assist")
    parser.add_argument("--hours", type=int, default=24, help="How far back to scan (hours)")
    parser.add_argument("--apply", action="store_true", help="Actually write (default is dry-run)")
    args = parser.parse_args()

    scanned, written, skipped, by_cat = backfill(
        env=args.env,
        app_name=args.app_name,
        hours=args.hours,
        apply=args.apply,
    )

    log.info("---")
    log.info("scanned (missing GSI2PK):  %d", scanned)
    for cat, n in sorted(by_cat.items()):
        log.info("  category=%s: %d", cat, n)
    log.info("written:                   %d", written)
    log.info("skipped (unknown shape or already done): %d", skipped)
    if not args.apply:
        log.info("(dry-run; nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
