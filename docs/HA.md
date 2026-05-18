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

Every `make ha-*` target takes `ENV=<env>` and auto-resolves the rest
from `terraform/${ENV}.tfvars`:

- `PRIMARY` ← the tfvars `aws_region` value
- `SECONDARY` ← the tfvars `secondary_region` value
- `APP` ← the tfvars `app_name` value (or the default
  `outcome-ops-ai-assist` if not set)

So most invocations need only `ENV` plus your `AWS_PROFILE` (which
selects the AWS credentials — typically configured in `~/.aws/config`
and not specified per-command):

```bash
AWS_PROFILE=ops ENV=prd make ha-status
```

Override any auto-resolved value by passing it explicitly:

```bash
AWS_PROFILE=ops ENV=prd PRIMARY=us-west-2 SECONDARY=us-east-1 \
    make ha-status
```

The `ha-replay-dlq` target additionally needs `REGION` and `QUEUE`
(targets one specific DLQ); those are called out per-section.

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
  AWS_PROFILE=ops ENV=prd make ha-list-secrets
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
AWS_PROFILE=ops ENV=prd make ha-bootstrap
```

Or scope it down to specific resources for iterative testing:

```bash
AWS_PROFILE=ops ENV=prd ARGS="--tables workspaces,audit-logs --dry-run" \
    make ha-bootstrap

AWS_PROFILE=ops ENV=prd ARGS="--skip-s3 --skip-vectors --tables workspaces" \
    make ha-bootstrap
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
AWS_PROFILE=ops ENV=prd make ha-status
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
   AWS_PROFILE=ops ENV=prd make ha-status
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
   AWS_PROFILE=ops ENV=prd make ha-status
   ```
   Should show "exactly one region active" and no split-brain.

5. **No client changes needed** — chat / MCP URLs in the secondary
   region were already serving local-region data.

## Failback procedure

Same procedure in reverse, after replication has caught up:

1. Confirm in-sync + empty DLQs:
   ```bash
   AWS_PROFILE=ops ENV=prd make ha-status
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
issue is fixed. `ha-replay-dlq` targets ONE specific queue at a time
(via `REGION` and `QUEUE`) since each DLQ has a different replay
direction:

```bash
AWS_PROFILE=ops ENV=prd \
    REGION=us-east-1 \
    QUEUE=prd-outcome-ops-ai-assist-replication-applier-dlq \
    ARGS="--dry-run" make ha-replay-dlq
```

Inspect dry-run output, then drop `ARGS="--dry-run"` to actually
replay. Pick `REGION`+`QUEUE` from:

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
AWS_PROFILE=ops ENV=prd make ha-status
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
  AWS_PROFILE=ops ENV=prd make ha-list-secrets
  ```
  and `aws ssm put-parameter --overwrite` each value that's still at
  `PLACEHOLDER_CONFIGURE_ME`.

## Compute-plane HA (full service continuity)

Replicating data alone is not enough — to keep the product *usable*
during a primary outage the compute plane (Lambdas + Fargate + ALB +
DNS) must also exist in the secondary region. The compute plane
deploys in lockstep with the data plane: when `secondary_region` is
set in tfvars, terraform creates a complete second copy of every
runtime resource.

### What runs in the secondary region

- **UI Fargate** (`ui-fargate-secondary.tf`) — Fargate Spot cluster
  with the same React+Express image as primary. Env vars point at the
  secondary's own Function URLs. `THIS_REGION` is injected so the UI
  knows which region served it.
- **All 9 *-integration Lambdas** (`lambda-integrations-secondary.tf`)
  — github / confluence / jira / outlook / teams / sharepoint /
  onedrive / onenote / database. Each has its own Function URL +
  SSM URL parameter so the OAuth-callback handlers in either region
  can answer.
- **chat-streaming + workspace-management** (`lambda-chat-workspace-secondary.tf`)
  — symbol-graph reader IAM is inlined per-region.
- **Writer Lambdas** (`lambda-writers-secondary.tf`) — file-ingestion,
  process-documents (with S3 event notification on secondary's
  uploads bucket), delete-worker, process-batch-summary,
  process-repo-summary.
- **10 schedulers + hourly EventBridge rules** (`lambda-schedulers-secondary.tf`).
- **9 sync workers** (`lambda-sync-workers-secondary.tf`).
- **Background Lambdas** (`lambda-background-secondary.tf`) —
  audit-alert, analytics-aggregator, analytics-cost-alert.
- **MCP-outcomeops + MCP-sonarqube + security-scanner Fargates**
  (`fargate-secondary.tf`) — Cloud Map private DNS namespace
  per-region.
- **Regional SNS topics** for audit + cost alerts. Same subscription
  emails reconfigured by terraform — operators will see double-sub
  confirmations during initial apply.

### Passive-mode gating

The secondary region's resources EXIST and serve traffic, but
non-trivial mutating work is gated by `is_active_region()` (a thin
AppConfig-backed check in `lambda/shared/active_region.py`) so the
passive region does not produce duplicate side effects.

What's gated:

- **Schedulers** (e.g. github-scheduler, jira-scheduler) return early
  when passive — only the active region's hourly tick fires sync
  workers.
- **audit-alert + analytics-cost-alert** return early when passive —
  only the active region's copy publishes SNS alerts. (Without this,
  every refusal would fire two emails.)
- **audit-stream-publisher** returns early when passive — only the
  active region exports OCSF to Kinesis. (Without this, customer
  SIEMs would see doubled events per audit row.)
- **All admin mutation routes** in `workspace-management` and each
  `*-integration` lambda return HTTP 403 via
  `shared.region_guard.guard_active()` when called against a passive
  region. The blocked-mutation attempt is itself recorded as an
  audit row with action `ADMIN_MUTATION_BLOCKED_PASSIVE` so operators
  can see who hit a bad URL during a flip.

What's NOT gated (intentionally runs in both regions):

- **chat-streaming + workspace-management reads** — `/api/chat`,
  `/api/conversations/...`, `/api/audit/...`, `/api/analytics/...`,
  `find_callers`, `list_workspaces`, etc. Reads serve from whichever
  region the URL hits. Users hitting the secondary during a flip see
  their conversation history because dual-write kept it in sync.
- **analytics-aggregator** — both regions process their own
  replicated audit-logs DDB stream into their own analytics-table so
  per-region dashboards stay consistent regardless of which region
  is active.
- **replication-applier** — the inverse of dual-write; both regions
  always need the receive-side wired up.

### UI passive-mode signal

The UI fetches `/api/region/role` at app load (cached for the
session) and renders an amber top-of-page banner when served by the
passive region:

> **Read-only mode.** This UI is served by the passive region
> (us-east-2). Admin actions are disabled. Chat, search, audit
> viewing, and analytics continue to work.

The most prominent mutation buttons (`Workspaces`/New, `Connect
GitHub`, per-repo Sync, per-repo Remove) auto-disable via
`<MutationButton>` and swap their hover tooltip to an explainer. The
remaining mutation buttons across the integration tabs still render
but are caught by the backend's `region_guard` 403 — the user sees a
clear error toast, not silent data corruption.

### DNS / hostname behavior across failover

Two stable region-pinned hostnames + one canonical hostname:

- `outcomeops1.<domain>` — always points at the primary region's ALB.
- `outcomeops2.<domain>` — always points at the secondary region's ALB.
- `outcomeops.<domain>` — canonical hostname; alias points at
  whichever region is currently `active`.

The two region-pinned hostnames are admin escape hatches — they let
the operator hit a specific region's UI even when AppConfig is in a
weird state. Day-to-day traffic uses the canonical hostname.

OAuth redirects use the canonical hostname only, so failover (flip
AppConfig + flip canonical DNS A-record) does not require
re-registering any OAuth app or restarting any session. The ACM cert
on each ALB has SANs covering both that region's pinned hostname AND
the canonical hostname, so SNI keeps working when the canonical alias
flips.

### Cross-region image / layer replication

- **ECR** uses `aws_ecr_replication_configuration` so primary-built
  images appear in the secondary region's ECR within minutes. The
  Fargate services in either region pull from their own region's
  registry.
- **Lambda layers** (license layer + platform_ca CA-bundle layer) are
  published per-region. The `outcomeops_license_layer_arn_secondary`
  tfvars value is the secondary-region ARN published by the license
  server's per-account push (see ADR-019 for the license-layer
  topology).

### Failover order across regions

For a clean failover, flip in this order:

1. **Flip AppConfig** in both regions (the becoming-passive region
   first, then the becoming-active region — see "Failback procedure"
   above).
2. **Flip the canonical DNS A-record** in Route 53 from the old-active
   region's ALB to the new-active region's ALB. The two region-pinned
   hostnames never change.
3. **Run `make ha-status`** to verify exactly one region is `active`,
   no split-brain, no DLQ backlog.

Both AppConfig flips and the DNS flip are load-bearing: AppConfig
gates the schedulers + region_guard, DNS routes user traffic. Skipping
the DNS flip leaves the canonical hostname pointing at a passive UI
that will refuse mutations — the read-side will still work but the
operator UX is confusing.

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
