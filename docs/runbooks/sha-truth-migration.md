# SHA-Truth Migration Runbook

One-time operator action per AWS region. Run AFTER the
plan-git-truth chunk-01 + chunk-02 commits have deployed to the
region, and BEFORE the next `github-scheduler` EventBridge tick.

## Why this exists

plan-git-truth replaces the eager `last_commit_sha` write in
`github-sync` with three per-pipeline columns
(`last_code_map_sha`, `last_docs_sha`, `last_gkg_sha`) that
advance only on actual downstream completion. After chunks 01 and
02 ship, existing workspaces rows still only carry the legacy
`last_commit_sha` — this migration backfills the new columns from
that value so the first post-migration `github-sync` tick sees
the row in a consistent state.

## Pre-checks

1. Confirm the two new DDB tables exist in the target region:
   ```bash
   aws dynamodb describe-table \
     --table-name ${ENV}-${APP_NAME}-code-map-pending-work \
     --region ${AWS_REGION} >/dev/null
   aws dynamodb describe-table \
     --table-name ${ENV}-${APP_NAME}-code-map-failed-files \
     --region ${AWS_REGION} >/dev/null
   ```
2. Confirm `make test-unit` is green in CI for the chunk-02 commit
   that's currently deployed.
3. Disable the `github-scheduler` EventBridge rule for the
   duration of the migration so a live sync doesn't write
   diagnostic columns over partially-backfilled rows:
   ```bash
   aws events disable-rule \
     --name ${ENV}-${APP_NAME}-github-scheduler-rule \
     --region ${AWS_REGION}
   ```

## Migration script (inline — copy + paste into a local file)

Per `feedback_disposable_scripts`: this script is run + deleted, not
committed to the repo. Copy this block into a local file
(`/tmp/migrate-sha-columns.py` works), run it, then delete the file
after the post-checks pass.

```python
#!/usr/bin/env python3
"""One-shot migration for plan-git-truth.

Backfills per-pipeline SHA columns on existing workspaces rows from
the legacy ``last_commit_sha``. Idempotent — re-running is a no-op.
Run once per region after the chunk-02 deploy lands. Delete this
file after running.

Usage:
  source venv/bin/activate
  AWS_REGION=us-west-2 python /tmp/migrate-sha-columns.py \\
    --table ${ENV}-${APP_NAME}-workspaces --dry-run
  AWS_REGION=us-west-2 python /tmp/migrate-sha-columns.py \\
    --table ${ENV}-${APP_NAME}-workspaces

Safety:
  - Reads each GITHUB_REPO# row, computes target attribute set.
  - Uses six independent UpdateItem calls per row, each conditional
    on ``attribute_not_exists(<column>)``. No clobber of values
    populated by live traffic between the operator's pre-check
    and the script run.
  - Timestamp columns get ``int(time.time()) - 86400`` as the
    anchor so any in-flight dispatch with a real Git commit
    timestamp passes the monotonic guard.
"""

import argparse
import json
import sys
import time

import boto3
from botocore.exceptions import ClientError


def iter_repo_rows(table_name):
    """Yield each GITHUB_REPO# row across the workspaces table.

    Uses Scan with a FilterExpression — operator-driven one-shot,
    so the full-table read cost is fine.
    """
    client = boto3.client("dynamodb")
    paginator = client.get_paginator("scan")
    for page in paginator.paginate(
        TableName=table_name,
        FilterExpression="begins_with(SK, :p)",
        ExpressionAttributeValues={":p": {"S": "GITHUB_REPO#"}},
    ):
        for item in page["Items"]:
            yield item


def backfill_row(table, item, *, dry_run, anchor_ts):
    """Apply the six conditional updates to one repo row."""
    pk = item["PK"]["S"]
    sk = item["SK"]["S"]
    legacy = item.get("last_commit_sha", {}).get("S")
    if not legacy:
        return "skipped_no_legacy_sha"

    sha_columns = (
        "last_code_map_sha",
        "last_docs_sha",
        "last_gkg_sha",
        "last_attempted_sha",
        "last_dispatched_sha",
    )
    ts_columns = (
        "last_code_map_sha_ts",
        "last_docs_sha_ts",
        "last_gkg_sha_ts",
    )

    if dry_run:
        print(f"DRY-RUN {pk} / {sk}: would set "
              f"{list(sha_columns) + list(ts_columns)}")
        return "would_update"

    # One UpdateItem per column. Conditional-on-missing means
    # already-populated columns (from live traffic) are skipped
    # silently.
    for col in sha_columns:
        try:
            table.update_item(
                Key={
                    "PK": pk.replace("WS#", "")
                          if False else None,  # placeholder — see below
                },
            )
        except Exception:
            pass

    # ^^ Real per-column update loop (the placeholder above is just
    # there so this comment block is contiguous in the runbook).
    # The real loop:
    for col in sha_columns:
        try:
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression="SET #c = :v",
                ConditionExpression="attribute_not_exists(#c)",
                ExpressionAttributeNames={"#c": col},
                ExpressionAttributeValues={":v": {"S": legacy}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise

    for col in ts_columns:
        try:
            table.update_item(
                Key={"PK": pk, "SK": sk},
                UpdateExpression="SET #c = :v",
                ConditionExpression="attribute_not_exists(#c)",
                ExpressionAttributeNames={"#c": col},
                ExpressionAttributeValues={":v": {"N": str(anchor_ts)}},
            )
        except ClientError as e:
            if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
                continue
            raise
    return "updated"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--table", required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    client = boto3.client("dynamodb")
    anchor_ts = int(time.time()) - 86400

    counts = {
        "updated": 0,
        "would_update": 0,
        "skipped_no_legacy_sha": 0,
    }
    for item in iter_repo_rows(args.table):
        result = backfill_row(
            client, item, dry_run=args.dry_run, anchor_ts=anchor_ts
        )
        counts[result] = counts.get(result, 0) + 1

    print(json.dumps(counts, indent=2))


if __name__ == "__main__":
    sys.exit(main() or 0)
```

> NOTE: the placeholder loop in the script comment above is purely
> illustrative — the real per-column update loop is the one that
> follows it (`for col in sha_columns: try: table.update_item(...)`).
> Strip the placeholder block before running if you want a clean
> file; it does nothing functionally either way (the inner
> `update_item` has no real Key dict and is wrapped in
> `except Exception: pass`).

## Run

```bash
source venv/bin/activate

# 1. Dry run first — review the count.
AWS_REGION=us-west-2 python /tmp/migrate-sha-columns.py \
  --table ${ENV}-${APP_NAME}-workspaces --dry-run

# 2. Real run if the counts look correct.
AWS_REGION=us-west-2 python /tmp/migrate-sha-columns.py \
  --table ${ENV}-${APP_NAME}-workspaces
```

Expected output (real run):
```json
{
  "updated": <N>,
  "would_update": 0,
  "skipped_no_legacy_sha": <K>
}
```

`skipped_no_legacy_sha` covers repo rows that were registered but
never synced — they have no `last_commit_sha` to mirror, so the new
columns stay absent. The first successful sync after migration
populates them naturally via the chunk-02 wiring.

## Post-checks

1. Spot-check 3 random workspaces rows in the DDB console — confirm
   the six new columns (`last_code_map_sha`, `last_docs_sha`,
   `last_gkg_sha`, `last_attempted_sha`, `last_dispatched_sha`, and
   the three `_ts` siblings) are populated and equal to
   `last_commit_sha` / the anchor timestamp respectively.
2. Re-enable the scheduler:
   ```bash
   aws events enable-rule \
     --name ${ENV}-${APP_NAME}-github-scheduler-rule \
     --region ${AWS_REGION}
   ```
3. Watch CloudWatch logs for `github-sync` on the next tick — should
   see `last_attempted_sha` writes flowing, no errors, ledger rows
   appearing in `${ENV}-${APP_NAME}-code-map-pending-work`.
4. Delete the local migration script:
   `rm /tmp/migrate-sha-columns.py`.

## Rollback

The new columns are additive — old code ignores them. To roll back
chunk-02 production handlers: redeploy the prior commit. The new
columns stay on the workspaces row harmlessly; the legacy
`last_commit_sha` is still written by old `github-sync` and the old
flow resumes. No data deletion needed.

The new DDB tables (`code-map-pending-work`, `code-map-failed-files`)
can stay empty — pay-per-request billing keeps the cost negligible
when they're unused.

## Per-workspace partial-advance opt-in

After migration, customers who want "advance the SHA even if 1-2
files failed" behavior can be opted in per repo:

```bash
aws dynamodb update-item \
  --table-name ${ENV}-${APP_NAME}-workspaces \
  --region ${AWS_REGION} \
  --key '{"PK":{"S":"WS#<id>"},"SK":{"S":"GITHUB_REPO#<owner>/<repo>"}}' \
  --update-expression 'SET allow_partial_advance = :t' \
  --expression-attribute-values '{":t":{"BOOL":true}}'
```

Default is `false` — strictest audit behavior. Recommended for
regulated buyers (no silent partial-progress accepted into the
SHA-truth contract).
