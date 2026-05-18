# High Availability (Active/Passive Multi-Region)

This document describes the OutcomeOps AI Assist HA architecture and the
operator runbook for failover, failback, and incident response.

## Overview

- **Active/passive across two AWS regions** — one region serves
  ingestion + scheduled work; the other stands by with replicated
  state.
- **Application-layer dual-write via EventBridge** — writers PutEvents
  to a local replication bus; a cross-region rule forwards events to
  the other region; the receiving region's applier Lambda replays the
  write locally. NOT DynamoDB global tables (see ADR-022 for why).
- **Operator-driven failover via AppConfig** — each region's local
  AppConfig holds an `active`/`passive` role flag. Failover is a
  human-flipped AppConfig deployment, not a terraform apply.
- **Local-region reads** — chat / MCP / find_callers / list_workspace_documents
  serve from whichever region the URL hits. Reads do not check
  AppConfig.

## Operator env vars

Every `make ha-*` target reads the same set of env vars. Either prefix
each invocation (as shown throughout this doc) or export them once at
the top of your shell session:

```bash
export AWS_PROFILE=ops
export ENV=prd
export APP=outcome-ops-ai-assist
export PRIMARY=us-west-2
export SECONDARY=us-east-1
```

Some targets read additional vars (e.g. `ha-replay-dlq` needs `REGION`
and `QUEUE`); those are called out per-section.

## Setup

### Required tfvars

```hcl
aws_region                   = "us-west-2"
secondary_region             = "us-east-1"        # set this to enable HA
initial_region_role_primary  = "active"           # default
initial_region_role_secondary = "passive"         # default
```

When `secondary_region = ""` (the default), single-region behavior is
preserved — no secondary resources are created and no events are
emitted.

### Pre-deploy operator checks

- **Bedrock model availability** — the same `bedrock_advanced_model_id`
  and `bedrock_embedding_model_id` must be available in BOTH regions.
  Verify in the AWS Bedrock console under model access before setting
  `secondary_region`. If a model is unavailable in the secondary, pick
  a different secondary region or a different model.
- **S3 Vectors availability** — S3 Vectors is still rolling out across
  regions. `terraform apply` fails loudly if the chosen
  `secondary_region` doesn't support it. Pick a different secondary.

### Post-deploy operator tasks

- **Populate SecureString parameters** — terraform creates GitHub App
  + OAuth client secrets with `PLACEHOLDER_CONFIGURE_ME` and
  `lifecycle.ignore_changes=[value]`. Operators must `aws ssm
  put-parameter --overwrite` each region's values manually.
- Run the list-secrets helper to see what's left to populate:

  ```bash
  AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
      PRIMARY=us-west-2 SECONDARY=us-east-1 \
      make ha-list-secrets
  ```

### Day-0 HA vs. add-later HA

**Day-0 (HA enabled at first deploy):** secondary tables/buckets are
created empty AND no data exists in primary yet, so the dual-write
transport keeps both regions in lockstep from the first write. No
bootstrap needed.

**Add-later (HA enabled against an existing single-region deployment):**
primary already holds months of data; secondary tables/buckets are
created empty by terraform. The dual-write transport only fires on
NEW writes — anything already in primary will NEVER appear in
secondary on its own. Failover at that point would lose history. Run
the bootstrap script ONCE before any failover test:

```bash
AWS_PROFILE=ops \
    ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    make ha-bootstrap
```

Or scope it down to specific resources for iterative testing:

```bash
AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    ARGS="--tables workspaces,audit-logs --dry-run" make ha-bootstrap

AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    ARGS="--skip-s3 --skip-vectors --tables workspaces" make ha-bootstrap
```

The bootstrap is idempotent — re-running it is safe; existing rows in
secondary are not clobbered (DDB PutItem uses
`ConditionExpression="attribute_not_exists(PK)"`; S3 CopyObject
re-writes the same key; S3 Vectors PutVectors overwrites with the same
key). You can run it multiple times if a region was unreachable mid-run.

## Day-to-day operation

- The **active** region runs:
  - All scheduled Lambdas (github-scheduler, jira-scheduler, etc.)
  - The license-heartbeat-scheduler
  - Writer Lambdas that fire from scheduled work
- The **passive** region runs:
  - The replicator-applier Lambda (replays cross-region events)
  - Read-side surfaces — chat / MCP / API — when users hit that
    region's URL
- **Both regions** run:
  - EventBridge replication transport (only the active direction
    actually carries traffic at any moment)
  - DLQs + CloudWatch DLQ-depth alarms

### Check status

```bash
AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    make ha-status
```

Reports:
- AppConfig role per region (with split-brain detection)
- Per-(workspace, repo) SHA replication lag
- DLQ depths across all 4 replication queues
- Verdict line: SAFE TO FLIP / DLQ messages / replication lagging /
  split-brain / partial visibility

Exit codes are machine-readable for scripts (see `scripts/ha-status`
docstring).

## Failover procedure

1. **Verify safety**:
   ```bash
   AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
       PRIMARY=us-west-2 SECONDARY=us-east-1 \
       make ha-status
   ```
   Confirm: verdict line says `SAFE TO FLIP`. If DLQ has messages or
   replication is lagging, investigate first.

2. **Flip the secondary region's AppConfig role to `active`**:
   - AWS Console → AppConfig → Applications → `${env}-${app}-ha` →
     Environment `${env}` → Profile `active-region` → Create new
     hosted configuration version with content:
     ```json
     {"schema_version": 1, "role": "active", "activated_at": "<now iso8601>"}
     ```
   - Start deployment with the `${env}-${app}-ha-instant` strategy.

3. **Flip the primary region's AppConfig role to `passive`** (if
   primary is reachable):
   - Same procedure in the primary region's AppConfig.
   - If primary is unreachable (real outage), skip — `make ha-status`
     will flag the split-brain when primary recovers, and you do this
     step after the fact.

4. **Confirm clean state**:
   ```bash
   AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
       PRIMARY=us-west-2 SECONDARY=us-east-1 \
       make ha-status
   ```
   Should show "exactly one region active" and no split-brain.

5. **No client changes needed** — chat / MCP URLs in the secondary
   region were already serving local-region data.

## Failback procedure

Same procedure in reverse, after replication has caught up:

1. Confirm in-sync + empty DLQs:
   ```bash
   AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
       PRIMARY=us-west-2 SECONDARY=us-east-1 \
       make ha-status
   ```
2. Flip the becoming-passive (currently active) region to `passive`
   FIRST.
3. Flip the becoming-active (currently passive) region to `active`.

Order matters: flipping the new-passive first minimizes the
split-brain window. If both regions briefly read `passive`, no
scheduled work runs for the in-between minutes — that's fine, the
next tick after re-activation catches up.

## Troubleshooting

### DLQ has messages

`make ha-status` exits with code 2. Drain the DLQ once the underlying
issue is fixed (note this target needs extra `REGION` and `QUEUE`
vars on top of the standard set):

```bash
AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    REGION=us-east-1 \
    QUEUE=prd-outcome-ops-ai-assist-replication-applier-dlq \
    ARGS="--dry-run" make ha-replay-dlq
```

Inspect dry-run output, then drop `ARGS="--dry-run"` to actually
replay. The `REGION`/`QUEUE` pair targets a specific DLQ; pick from:

- `${ENV}-${APP}-replication-to-secondary-dlq` (in PRIMARY) —
  events the primary couldn't deliver to secondary's bus.
- `${ENV}-${APP}-replication-to-primary-dlq` (in SECONDARY) —
  events the secondary couldn't deliver to primary's bus.
- `${ENV}-${APP}-replication-applier-dlq` (in either region) —
  events the applier Lambda failed to apply locally.

### AppConfig flags disagree (split-brain)

`make ha-status` exit code 3: both regions read `active`.

- Compare `activated_at` timestamps. The region with the OLDER
  timestamp should be flipped to `passive`.
- If only one region's `activated_at` was set programmatically (via a
  failover) and the other was the original terraform-deployed value,
  flip the original-default one.

### Both regions passive

Exit code 4. No scheduler is firing anywhere. Acceptable for short
maintenance windows; otherwise flip the intended-active region to
`active`.

### One region's AppConfig unreachable

Exit code 5. The reachable region's role still applies. Schedulers in
the unreachable region fail-closed (no work runs there) until
AppConfig recovers. Once it does, re-run:

```bash
AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
    PRIMARY=us-west-2 SECONDARY=us-east-1 \
    make ha-status
```

### Lambda errors after failover

Common causes:
- **Bedrock model unavailable**: operator forgot the pre-deploy check.
  Either roll back the failover or update tfvars to a model the new
  region supports and re-apply.
- **IAM gaps**: a writer Lambda is now running in the previously-passive
  region but its IAM was scoped to primary-only ARNs. Symptom:
  AccessDeniedException in CloudWatch. Fix: extend the IAM resources
  list to include the now-active region's ARNs.
- **SecureString placeholder**: an OAuth secret was never populated in
  the now-active region. Run:
  ```bash
  AWS_PROFILE=ops ENV=prd APP=outcome-ops-ai-assist \
      PRIMARY=us-west-2 SECONDARY=us-east-1 \
      make ha-list-secrets
  ```
  and `aws ssm put-parameter --overwrite` each value that's still at
  `PLACEHOLDER_CONFIGURE_ME`.

## Caveats

- **Replication lag** — ~5 minutes is normal under steady-state load.
  Schedulers fire hourly; a flip with lag <5 min loses at most one
  scheduler tick's worth of work, which the next tick reconciles.
- **License-server is single-region** — paid tiers (annual) fail-open
  during license-server outages so chat / MCP keep working. PoC / pilot
  tiers fail-hard.
- **Cross-region failover is operator-driven, not automatic** — AWS
  outages rarely localize cleanly (Oct 2025 us-east-1 control-plane
  affected services in non-us-east regions). Human in the loop matters
  for regulated-audit deployments.

## See also

- [ADR-022: Multi-region active/passive architecture](adr/ADR-022-multi-region-active-passive.md)
- `terraform/appconfig.tf`, `terraform/eventbridge-replication.tf`
- `lambda/shared/active_region.py`, `lambda/shared/dual_write.py`
- `lambda/replication-applier/handler.py`
