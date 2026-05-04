#!/usr/bin/env python3
"""Backfill cost_usd on historical Cohere Rerank audit rows.

Before commit 4eee0f4, log_audit_entry had no pricing entry for
``cohere.rerank-v3-5:0`` and a ``(input_tokens or output_tokens)`` guard
on the auto-calc, so every rerank row landed at ``cost_usd="0"`` and
the aggregator rolled up zero rerank cost. This script finds those rows,
updates them to the configured per-call price, and ADDs the same delta
into the matching DT / DM / DU analytics rollups so the cost-by-model
chart and totals reflect rerank spend without a full re-aggregation.

Usage:
    source venv/bin/activate
    python scripts/backfill-rerank-cost.py --env prd --dry-run
    python scripts/backfill-rerank-cost.py --env prd --apply

Idempotent: a ConditionExpression on ``cost_usd IN ('0', '0.0', '')``
guarantees a re-run only touches rows that haven't been priced yet, so
the rollup ADD never double-counts.

Optional ``--price`` override; defaults to the same env var the Lambdas
read (``BEDROCK_RERANK_PRICE_PER_1K``, default $2.00 / 1k = $0.002/call).
"""

import argparse
import logging
import os
import sys
import time
from decimal import Decimal
from typing import Any, Dict, Iterator, Tuple

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("backfill-rerank-cost")

DEFAULT_RERANK_MODEL = os.environ.get(
    "BEDROCK_RERANK_MODEL_ID", "cohere.rerank-v3-5:0"
)
DEFAULT_PRICE_PER_1K = float(
    os.environ.get("BEDROCK_RERANK_PRICE_PER_1K", "2.00")
)


def _resolve_table_name(ssm, env: str, app_name: str, which: str) -> str:
    return ssm.get_parameter(
        Name=f"/{env}/{app_name}/dynamodb/{which}"
    )["Parameter"]["Value"]


def _iter_unpriced_rerank_rows(
    audit_table, rerank_model: str
) -> Iterator[Dict]:
    """Scan the audit table for rerank rows still at cost_usd=0/empty.

    GSI2 (CAT#bedrock) would be a smaller read, but rerank rows are a
    tiny fraction of the bedrock partition and we still need a Filter
    to find unpriced ones — so a base-table Scan with the filter is
    simpler and only pays the read tax once.
    """
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": (
            "model_id = :m AND "
            "(cost_usd = :zero OR cost_usd = :zero_dot OR "
            " cost_usd = :empty OR attribute_not_exists(cost_usd))"
        ),
        "ExpressionAttributeValues": {
            ":m": rerank_model,
            ":zero": "0",
            ":zero_dot": "0.0",
            ":empty": "",
        },
        "ProjectionExpression": (
            "PK, SK, request_id, workspace_id, user_email, "
            "#ts, model_id, cost_usd"
        ),
        "ExpressionAttributeNames": {"#ts": "timestamp"},
    }
    while True:
        resp = audit_table.scan(**scan_kwargs)
        for item in resp.get("Items", []):
            yield item
        if "LastEvaluatedKey" not in resp:
            return
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]


def _update_audit_row(
    audit_table, item: Dict, price_str: str
) -> bool:
    """Set cost_usd on a single audit row, conditional on it still being
    unpriced. Returns True if updated, False if already-priced (concurrent
    write or re-run on a row touched in another pass)."""
    try:
        audit_table.update_item(
            Key={"PK": item["PK"], "SK": item["SK"]},
            UpdateExpression="SET cost_usd = :p",
            ConditionExpression=(
                "cost_usd = :zero OR cost_usd = :zero_dot OR "
                "cost_usd = :empty OR attribute_not_exists(cost_usd)"
            ),
            ExpressionAttributeValues={
                ":p": price_str,
                ":zero": "0",
                ":zero_dot": "0.0",
                ":empty": "",
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _bump_rollup(
    analytics_table,
    pk: str,
    sk: str,
    delta: Decimal,
) -> None:
    """ADD-update a rollup row's cost_usd by delta. DDB ADD is upsert:
    if the row doesn't exist, this creates it with cost_usd=delta and
    queries=0 (we don't bump query count - the rerank request itself was
    counted when the original aggregation ran)."""
    analytics_table.update_item(
        Key={"PK": pk, "SK": sk},
        UpdateExpression="ADD cost_usd :d",
        ExpressionAttributeValues={":d": delta},
    )


def _bump_rollups_for_row(
    analytics_table, item: Dict, delta: Decimal, model_id: str
) -> int:
    """ADD delta into DT / DM / DU rollups for a single audit row.
    Returns the number of rollup updates applied (1-3 depending on
    whether workspace_id and user_email are set)."""
    workspace_id = item.get("workspace_id") or ""
    user_email = (item.get("user_email") or "").lower()
    ts = item.get("timestamp") or ""
    date = ts[:10]
    if not workspace_id or workspace_id == "SYSTEM" or not date:
        # Aggregator skips these rows entirely; no rollup to bump.
        return 0

    pk = f"WS#{workspace_id}"
    bumps = 0

    _bump_rollup(analytics_table, pk, f"DT#{date}", delta)
    bumps += 1
    _bump_rollup(analytics_table, pk, f"DM#{date}#{model_id}", delta)
    bumps += 1
    if user_email:
        _bump_rollup(analytics_table, pk, f"DU#{date}#{user_email}", delta)
        bumps += 1
    return bumps


def backfill(
    env: str,
    app_name: str,
    rerank_model: str,
    price_per_call: float,
    apply: bool,
) -> Tuple[int, int, int]:
    session = boto3.session.Session()
    ssm = session.client("ssm")
    dynamodb = session.resource("dynamodb")

    audit_name = _resolve_table_name(ssm, env, app_name, "audit-logs-table")
    analytics_name = _resolve_table_name(ssm, env, app_name, "analytics-table")
    log.info("audit table:     %s", audit_name)
    log.info("analytics table: %s", analytics_name)
    log.info("rerank model:    %s", rerank_model)
    log.info("price per call:  $%.6f  (BEDROCK_RERANK_PRICE_PER_1K=%.4f)",
             price_per_call, price_per_call * 1000)
    log.info("mode:            %s",
             "APPLY (writes enabled)" if apply else "dry-run")

    audit_table = dynamodb.Table(audit_name)
    analytics_table = dynamodb.Table(analytics_name)

    price_str = str(round(price_per_call, 6))
    delta = Decimal(price_str)

    examined = 0
    audit_writes = 0
    rollup_writes = 0

    for item in _iter_unpriced_rerank_rows(audit_table, rerank_model):
        examined += 1
        if not apply:
            log.info(
                "[dry-run] would price PK=%s SK=%s ws=%s user=%s",
                item.get("PK", ""),
                (item.get("SK", "") or "")[:40],
                item.get("workspace_id", ""),
                item.get("user_email", ""),
            )
            audit_writes += 1
            # Estimate rollup writes (1-3 per audit row).
            ws = item.get("workspace_id") or ""
            if ws and ws != "SYSTEM":
                rollup_writes += 2 + (1 if item.get("user_email") else 0)
            continue

        if _update_audit_row(audit_table, item, price_str):
            audit_writes += 1
            try:
                rollup_writes += _bump_rollups_for_row(
                    analytics_table, item, delta, rerank_model,
                )
            except ClientError as e:
                log.error(
                    "rollup bump failed for PK=%s SK=%s: %s",
                    item.get("PK"), item.get("SK"), e,
                )

    return examined, audit_writes, rollup_writes


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env", default="dev")
    parser.add_argument("--app-name", default="outcome-ops-ai-assist")
    parser.add_argument("--model", default=DEFAULT_RERANK_MODEL,
                        help="Rerank model_id to backfill (default: configured rerank model)")
    parser.add_argument("--price", type=float, default=None,
                        help="Override per-call price in USD. Default derives from "
                             "BEDROCK_RERANK_PRICE_PER_1K env var or $2.00/1k.")
    parser.add_argument("--apply", action="store_true",
                        help="Actually write (default is dry-run)")
    args = parser.parse_args()

    price_per_call = (
        args.price if args.price is not None else DEFAULT_PRICE_PER_1K / 1000.0
    )

    t0 = time.time()
    examined, audit_writes, rollup_writes = backfill(
        env=args.env,
        app_name=args.app_name,
        rerank_model=args.model,
        price_per_call=price_per_call,
        apply=args.apply,
    )
    elapsed = time.time() - t0

    log.info("---")
    log.info("rerank rows examined:      %d", examined)
    log.info("audit rows updated:        %d", audit_writes)
    log.info("rollup rows bumped:        %d", rollup_writes)
    log.info("total cost backfilled:     $%.4f", audit_writes * price_per_call)
    log.info("elapsed:                   %.1fs", elapsed)
    if not args.apply:
        log.info("(dry-run; nothing was written)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
