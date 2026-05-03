#!/usr/bin/env python3
"""Backfill source_ip (and optionally user_agent) on existing audit rows.

Used when audit rows were written before the IP-capture fix landed and the
operator wants the demo / dashboard to show a realistic source. Defaults to
admin-category rows in the last 24h - bedrock rows are server-side LLM
calls and shouldn't acquire a fake browser IP.

Usage:
    source venv/bin/activate
    python scripts/backfill-audit-source-ip.py --env prd --ip 24.253.92.153 --dry-run
    python scripts/backfill-audit-source-ip.py --env prd --ip 24.253.92.153 --apply

Options:
    --ip                Source IP to write (required).
    --user-agent        Optional UA to write alongside the IP. If omitted,
                        only source_ip is touched.
    --hours             Lookback window in hours (default 24).
    --category          Audit row category to target (default "admin"; pass
                        "bedrock" or another value to retarget).
    --apply             Required for any writes; default is dry-run.

Idempotent: a ConditionExpression on source_ip = "" guarantees the script
only touches rows that genuinely need the backfill, so re-runs are safe.
"""

import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from typing import Dict, Iterator, Tuple

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill-audit-source-ip")


def _resolve_table_name(ssm, env: str, app_name: str) -> str:
    param = f"/{env}/{app_name}/dynamodb/audit-logs-table"
    return ssm.get_parameter(Name=param)["Parameter"]["Value"]


def _iter_target_rows(
    audit_table, start_iso: str, category: str
) -> Iterator[Dict]:
    """Query GSI2 for category rows in the time window. We use GSI2 here
    because chunk-08 (2fd19e9) backfilled it, so this query reads only
    rows in the requested category - no Scan + post-filter required."""
    query_kwargs = {
        "IndexName": "GSI2",
        "KeyConditionExpression": "GSI2PK = :pk AND GSI2SK >= :s",
        "ExpressionAttributeValues": {
            ":pk": f"CAT#{category}",
            ":s": start_iso,
        },
    }
    while True:
        resp = audit_table.query(**query_kwargs)
        for item in resp.get("Items", []):
            yield item
        if "LastEvaluatedKey" not in resp:
            return
        query_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _update_source_ip(
    audit_table,
    item: Dict,
    ip: str,
    user_agent: str = "",
) -> bool:
    """Set source_ip (and user_agent if provided) on a single row.

    Conditional on source_ip currently being absent or empty. Returns True
    on write, False if the row was already populated (idempotent).
    """
    update_parts = ["source_ip = :ip"]
    expr_values = {":ip": ip, ":empty": ""}
    expr_names = {}

    if user_agent:
        update_parts.append("user_agent = :ua")
        expr_values[":ua"] = user_agent

    try:
        audit_table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET " + ", ".join(update_parts),
            # Only fill when the row genuinely lacks an IP. Either the
            # attribute is missing entirely or it's the empty string.
            ConditionExpression=(
                "attribute_not_exists(source_ip) OR source_ip = :empty"
            ),
            ExpressionAttributeValues=expr_values,
            **({"ExpressionAttributeNames": expr_names} if expr_names else {}),
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def backfill(
    env: str,
    app_name: str,
    ip: str,
    user_agent: str,
    hours: int,
    category: str,
    apply: bool,
) -> Tuple[int, int, int]:
    session = boto3.session.Session()
    ssm = session.client("ssm")
    dynamodb = session.resource("dynamodb")

    audit_table_name = _resolve_table_name(ssm, env, app_name)
    log.info("audit table:        %s", audit_table_name)
    log.info("target category:    %s", category)
    log.info("source_ip to write: %s", ip)
    if user_agent:
        log.info("user_agent override: %s", user_agent)
    log.info("mode:               %s", "APPLY (writes enabled)" if apply else "dry-run")

    audit_table = dynamodb.Table(audit_table_name)
    start_iso = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    log.info("range:              %s onward (last %dh)", start_iso, hours)

    examined = 0
    written = 0
    already_set = 0

    for item in _iter_target_rows(audit_table, start_iso, category):
        examined += 1
        existing_ip = item.get("source_ip", "")
        if existing_ip:
            already_set += 1
            continue

        if not apply:
            log.info(
                "[dry-run] would set source_ip=%s on PK=%s SK=%s (action=%s)",
                ip,
                item.get("PK", ""),
                item.get("SK", "")[:30],
                item.get("action", "-"),
            )
            written += 1
            continue

        try:
            if _update_source_ip(audit_table, item, ip, user_agent):
                written += 1
            else:
                already_set += 1
        except ClientError as e:
            log.error(
                "update failed for PK=%s SK=%s: %s",
                item.get("PK"), item.get("SK"), e,
            )

    return examined, written, already_set


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev", help="Environment (dev/prd)")
    parser.add_argument("--app-name", default="outcome-ops-ai-assist")
    parser.add_argument("--ip", required=True, help="Source IP to write")
    parser.add_argument("--user-agent", default="", help="Optional UA override")
    parser.add_argument("--hours", type=int, default=24, help="Lookback window (hours)")
    parser.add_argument("--category", default="admin", help="Row category to target")
    parser.add_argument("--apply", action="store_true", help="Actually write")
    args = parser.parse_args()

    examined, written, already_set = backfill(
        env=args.env,
        app_name=args.app_name,
        ip=args.ip,
        user_agent=args.user_agent,
        hours=args.hours,
        category=args.category,
        apply=args.apply,
    )

    log.info("---")
    log.info("examined:                  %d", examined)
    log.info("would-write / written:     %d", written)
    log.info("already populated (skip):  %d", already_set)
    if not args.apply:
        log.info("(dry-run; nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
