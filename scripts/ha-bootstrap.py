#!/usr/bin/env python3
# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""HA bootstrap — back-fill secondary-region state from primary.

For customers who enabled HA AFTER running single-region for a while.
The dual-write replicator only fires on NEW writes; this script copies
everything already in primary into secondary so failover doesn't lose
history.

Run AFTER:
- ``terraform apply`` with ``secondary_region`` set (secondary resources
  must exist).
- Operator-populated SSM SecureStrings in the secondary region
  (``make ha-list-secrets`` reports what's left).

Run BEFORE:
- First failover test — otherwise secondary is empty and you'd lose
  everything on flip.

Idempotency:
- DDB PutItem uses ``ConditionExpression="attribute_not_exists(PK)"`` —
  re-running is safe; existing rows in secondary are not clobbered.
- S3 CopyObject re-writes the same key — safe; matches the primary.
- S3 Vectors PutVectors with the same key overwrites — safe; matches
  the primary.

What it copies:
- 7 dual-replicated DDB tables (workspaces, audit-logs, chat,
  analytics, code-maps, symbol-graph, code-map-pending-work,
  code-map-failed-files).
- The knowledge_base S3 bucket (primary -> primary's name; secondary
  bucket has a ``-secondary`` suffix).
- The S3 Vectors index (re-puts every vector in secondary's index).

Usage:

    AWS_PROFILE=ops \\
        ENV=prd APP=outcome-ops-ai-assist \\
        PRIMARY=us-west-2 SECONDARY=us-east-1 \\
        ./scripts/ha-bootstrap.py [options]

Options:
    --tables ws,audit          Comma-separated table short-names to bootstrap.
                               Default: all. Names: workspaces, audit-logs,
                               chat, analytics, code-maps, symbol-graph,
                               pending-work, failed-files.
    --skip-ddb                 Skip all DDB tables.
    --skip-s3                  Skip the KB bucket copy.
    --skip-vectors             Skip S3 Vectors copy.
    --dry-run                  Print what would be copied without writing.
    --max-ddb-items N          Cap DDB items per table (debugging). Default: unlimited.
    --batch-size N             DDB PutItem batch parallelism. Default: 25.

Exit codes:
    0  success (all requested resources copied or dry-run completed)
    1  partial failure (some items failed; see stderr)
    2  setup error (missing env var, region unreachable, etc.)
"""

from __future__ import annotations

import argparse
import os
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError


# Shared boto3 config with adaptive retries. "adaptive" mode tracks
# throttling responses and dynamically slows down requests to stay
# under the table's actual ceiling — much smoother than the default
# fixed exponential backoff. max_attempts=10 gives us room for sustained
# bursts (audit-logs at ~66k items + 25-way parallelism hits the
# on-demand burst ceiling repeatedly). Applied to every boto3 client
# and resource in this script.
BOTO_CONFIG = Config(
    retries={"max_attempts": 10, "mode": "adaptive"},
    # Default read timeout (60s) is fine; keep connect_timeout snappy.
    connect_timeout=5,
    read_timeout=60,
)

# Throttling error codes that warrant an explicit application-level
# retry on top of boto3's SDK-level retry budget. We hit these when
# sustained writes exceed the on-demand burst capacity faster than
# adaptive mode can ramp down.
_THROTTLE_CODES = frozenset({
    "ThrottlingException",
    "ProvisionedThroughputExceededException",
    "RequestLimitExceeded",
    "TransactionConflictException",
})


# ---------------------------------------------------------------------------
# Env-var handling
# ---------------------------------------------------------------------------


def _env(name: str, fallback: Optional[str] = None) -> str:
    val = os.environ.get(name, fallback or "").strip()
    if not val:
        print(f"ERROR: {name} env var is required", file=sys.stderr)
        sys.exit(2)
    return val


# ---------------------------------------------------------------------------
# Table inventory: short-name -> (table-name suffix, has_ttl, primary-key shape)
# Keys map to dual-replicated table names; the script uses these to compute
# both primary and secondary table names.
# ---------------------------------------------------------------------------


TABLE_INVENTORY: Dict[str, str] = {
    "workspaces": "workspaces",
    "audit-logs": "audit-logs",
    "chat": "chat",
    "analytics": "analytics",
    "code-maps": "code-maps",
    "symbol-graph": "symbol-graph",
    "pending-work": "code-map-pending-work",
    "failed-files": "code-map-failed-files",
}


def _table_name(env: str, app: str, suffix: str) -> str:
    return f"{env}-{app}-{suffix}"


def _kb_bucket_primary(env: str, app: str, account_id: str) -> str:
    return f"{env}-{app}-kb-{account_id}"


def _kb_bucket_secondary(env: str, app: str, account_id: str) -> str:
    return f"{env}-{app}-kb-{account_id}-secondary"


def _vector_bucket(env: str, app: str) -> str:
    # S3 Vectors names are unique PER REGION; same name works in both regions.
    return f"{env}-{app}-vectors"


def _vector_index() -> str:
    return "knowledge-base"


# ---------------------------------------------------------------------------
# DDB bootstrap
# ---------------------------------------------------------------------------


def _scan_paginated(table) -> List[dict]:
    """Pull every item from a DDB table via paginated Scan."""

    items: List[dict] = []
    scan_kwargs: Dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return items


def _put_item_idempotent(table, item: dict) -> Tuple[bool, Optional[str]]:
    """PutItem with ``attribute_not_exists(PK)``. Returns (wrote_new, error).

    Belt-and-suspenders retry on top of boto3's adaptive SDK retries:
    if a throttling error escapes the SDK budget (10 attempts), we
    retry up to 5 more times here with full-jitter exponential
    backoff. ConditionalCheckFailedException is the expected
    "already-present" signal and returns (False, None) on the first
    attempt — never retried.
    """

    max_app_retries = 5
    for attempt in range(max_app_retries + 1):
        try:
            table.put_item(
                Item=item,
                ConditionExpression="attribute_not_exists(PK)",
            )
            return True, None
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ConditionalCheckFailedException":
                return False, None
            if code in _THROTTLE_CODES and attempt < max_app_retries:
                # Full-jitter backoff: random in [0, base * 2**attempt),
                # capped at 30s. The randomization spreads concurrent
                # workers so they don't synchronize into a stampede.
                delay = random.uniform(
                    0.0, min(30.0, 0.5 * (2 ** attempt))
                )
                time.sleep(delay)
                continue
            return False, code
    return False, "exhausted-retries"


def _bootstrap_table(
    short_name: str,
    primary_region: str,
    secondary_region: str,
    env: str,
    app: str,
    *,
    dry_run: bool,
    max_items: Optional[int],
    parallelism: int,
) -> Tuple[int, int, int]:
    """Bootstrap one DDB table primary -> secondary.

    Returns (scanned, wrote_new, skipped_already_present)."""

    suffix = TABLE_INVENTORY[short_name]
    table_name = _table_name(env, app, suffix)

    primary_ddb = boto3.resource("dynamodb", region_name=primary_region, config=BOTO_CONFIG)
    secondary_ddb = boto3.resource("dynamodb", region_name=secondary_region, config=BOTO_CONFIG)
    primary_table = primary_ddb.Table(table_name)
    secondary_table = secondary_ddb.Table(table_name)

    print(f"  scanning primary {table_name} ({primary_region})...")
    items = _scan_paginated(primary_table)
    if max_items is not None:
        items = items[:max_items]
    print(f"    {len(items)} items found")

    if dry_run:
        print(f"    DRY-RUN: would write {len(items)} items to {table_name} ({secondary_region})")
        return len(items), 0, 0

    wrote_new = 0
    skipped = 0
    errors = 0
    error_codes: Dict[str, int] = {}

    def _do_one(item: dict) -> Tuple[bool, Optional[str]]:
        return _put_item_idempotent(secondary_table, item)

    if not items:
        return 0, 0, 0

    progress_every = max(1, len(items) // 20)
    written_so_far = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_do_one, item): item for item in items}
        for fut in as_completed(futures):
            written_so_far += 1
            wrote, err = fut.result()
            if err is not None:
                errors += 1
                error_codes[err] = error_codes.get(err, 0) + 1
            elif wrote:
                wrote_new += 1
            else:
                skipped += 1
            if written_so_far % progress_every == 0:
                print(
                    f"    progress: {written_so_far}/{len(items)} "
                    f"(new={wrote_new} skipped={skipped} errors={errors})"
                )

    summary = f"    done: scanned={len(items)} wrote_new={wrote_new} already_present={skipped}"
    if errors:
        summary += f" errors={errors} {error_codes}"
    print(summary)
    return len(items), wrote_new, skipped


# ---------------------------------------------------------------------------
# S3 KB bucket bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_s3_kb(
    primary_region: str,
    secondary_region: str,
    env: str,
    app: str,
    *,
    account_id: str,
    dry_run: bool,
    parallelism: int,
) -> Tuple[int, int]:
    """Copy every object from primary's KB bucket to secondary's."""

    src_bucket = _kb_bucket_primary(env, app, account_id)
    dst_bucket = _kb_bucket_secondary(env, app, account_id)

    print(f"  listing {src_bucket} ({primary_region})...")
    primary_s3 = boto3.client("s3", region_name=primary_region, config=BOTO_CONFIG)
    secondary_s3 = boto3.client("s3", region_name=secondary_region, config=BOTO_CONFIG)

    keys: List[str] = []
    paginator = primary_s3.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=src_bucket):
        for obj in page.get("Contents", []) or []:
            keys.append(obj["Key"])
    print(f"    {len(keys)} objects found")

    if dry_run:
        print(f"    DRY-RUN: would copy {len(keys)} objects to {dst_bucket}")
        return len(keys), 0

    def _copy(key: str) -> Tuple[bool, Optional[str]]:
        try:
            secondary_s3.copy_object(
                CopySource={"Bucket": src_bucket, "Key": key},
                Bucket=dst_bucket,
                Key=key,
            )
            return True, None
        except ClientError as exc:
            return False, exc.response.get("Error", {}).get("Code", "unknown")

    copied = 0
    errors = 0
    progress_every = max(1, len(keys) // 20)
    written_so_far = 0
    with ThreadPoolExecutor(max_workers=parallelism) as pool:
        futures = {pool.submit(_copy, k): k for k in keys}
        for fut in as_completed(futures):
            written_so_far += 1
            ok, err = fut.result()
            if ok:
                copied += 1
            else:
                errors += 1
                print(f"    copy failed for {futures[fut]}: {err}", file=sys.stderr)
            if written_so_far % progress_every == 0:
                print(
                    f"    progress: {written_so_far}/{len(keys)} "
                    f"(copied={copied} errors={errors})"
                )

    print(f"    done: copied={copied} errors={errors}")
    return copied, errors


# ---------------------------------------------------------------------------
# S3 Vectors bootstrap
# ---------------------------------------------------------------------------


def _bootstrap_vectors(
    primary_region: str,
    secondary_region: str,
    env: str,
    app: str,
    *,
    dry_run: bool,
) -> Tuple[int, int]:
    """Re-emit every vector from primary's index into secondary's."""

    bucket = _vector_bucket(env, app)
    index = _vector_index()

    primary_s3v = boto3.client("s3vectors", region_name=primary_region, config=BOTO_CONFIG)
    secondary_s3v = boto3.client("s3vectors", region_name=secondary_region, config=BOTO_CONFIG)

    print(f"  listing vectors in {bucket}/{index} ({primary_region})...")

    next_token: Optional[str] = None
    total = 0
    copied = 0
    errors = 0
    batch_size = 100
    while True:
        list_kwargs: Dict = {
            "vectorBucketName": bucket,
            "indexName": index,
            "maxResults": batch_size,
            "returnData": True,
            "returnMetadata": True,
        }
        if next_token:
            list_kwargs["nextToken"] = next_token
        resp = primary_s3v.list_vectors(**list_kwargs)
        vectors = resp.get("vectors", []) or []
        total += len(vectors)
        if not vectors:
            break

        if not dry_run:
            try:
                secondary_s3v.put_vectors(
                    vectorBucketName=bucket,
                    indexName=index,
                    vectors=vectors,
                )
                copied += len(vectors)
            except ClientError as exc:
                errors += len(vectors)
                print(
                    f"    put_vectors batch failed (size={len(vectors)}): "
                    f"{exc.response.get('Error', {}).get('Code', 'unknown')}",
                    file=sys.stderr,
                )

        next_token = resp.get("nextToken")
        if not next_token:
            break

    if dry_run:
        print(f"    DRY-RUN: would copy {total} vectors")
        return total, 0
    print(f"    done: copied={copied} errors={errors} total_seen={total}")
    return copied, errors


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--tables",
        default="",
        help="Comma-separated DDB table short names. Default: all.",
    )
    parser.add_argument("--skip-ddb", action="store_true")
    parser.add_argument("--skip-s3", action="store_true")
    parser.add_argument("--skip-vectors", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-ddb-items", type=int, default=None)
    parser.add_argument("--batch-size", type=int, default=25)
    args = parser.parse_args()

    env = _env("ENV")
    app = _env("APP", "outcome-ops-ai-assist")
    primary = _env("PRIMARY")
    secondary = _env("SECONDARY")

    # Resolve account id from STS (the secondary bucket name includes it).
    sts = boto3.client("sts", region_name=primary, config=BOTO_CONFIG)
    account_id = sts.get_caller_identity()["Account"]

    print(f"HA bootstrap for {env}-{app}")
    print(f"  primary  : {primary}")
    print(f"  secondary: {secondary}")
    print(f"  account  : {account_id}")
    if args.dry_run:
        print("  MODE: DRY-RUN (no writes)")
    print()

    selected_tables: List[str]
    if args.tables.strip():
        selected_tables = [t.strip() for t in args.tables.split(",") if t.strip()]
        unknown = [t for t in selected_tables if t not in TABLE_INVENTORY]
        if unknown:
            print(f"ERROR: unknown table(s) {unknown}; valid: {list(TABLE_INVENTORY)}", file=sys.stderr)
            return 2
    else:
        selected_tables = list(TABLE_INVENTORY)

    start = time.time()
    exit_code = 0

    # DDB tables
    if not args.skip_ddb:
        print("=== DynamoDB ===")
        for short_name in selected_tables:
            print(f"[{short_name}]")
            try:
                _bootstrap_table(
                    short_name,
                    primary,
                    secondary,
                    env,
                    app,
                    dry_run=args.dry_run,
                    max_items=args.max_ddb_items,
                    parallelism=args.batch_size,
                )
            except ClientError as exc:
                print(f"  ERROR: {exc}", file=sys.stderr)
                exit_code = 1
        print()

    # S3 KB bucket
    if not args.skip_s3:
        print("=== S3 (knowledge-base bucket) ===")
        try:
            _, errors = _bootstrap_s3_kb(
                primary,
                secondary,
                env,
                app,
                account_id=account_id,
                dry_run=args.dry_run,
                parallelism=args.batch_size,
            )
            if errors > 0:
                exit_code = 1
        except ClientError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            exit_code = 1
        print()

    # S3 Vectors
    if not args.skip_vectors:
        print("=== S3 Vectors ===")
        try:
            _, errors = _bootstrap_vectors(
                primary, secondary, env, app, dry_run=args.dry_run
            )
            if errors > 0:
                exit_code = 1
        except ClientError as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            exit_code = 1
        print()

    elapsed = time.time() - start
    print(f"=== Done in {elapsed:.1f}s — exit={exit_code} ===")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
