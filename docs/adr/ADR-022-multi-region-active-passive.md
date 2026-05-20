# ADR-022: Multi-Region Active/Passive HA via Application-Layer Dual-Write

## Status: Accepted

## Context

The OutcomeOps AI Assist platform deploys into a customer's AWS
account, and regulated-industry customers (financial services,
healthcare, public sector) require resilience to a single-region AWS
outage. The October 2025 us-east-1 control-plane incident affected
managed-replication services across the partition, which informed the
trade-offs below.

The platform's stateful surfaces:
- **DynamoDB**: workspaces, audit-logs, chat-conversations, analytics,
  code-maps, symbol-graph, code-map-pending-work, code-map-failed-files.
- **S3**: knowledge-base bucket (workspace documents).
- **S3 Vectors**: embedding index for RAG retrieval.
- **SQS**: ingest queues, integration sync queues, DLQs.
- **AppConfig**: feature flags and the HA active-region role flag itself.
- **EventBridge**: cross-region replication transport.

## Decision

Adopt an **active/passive multi-region** topology with
**application-layer dual-write** as the replication mechanism, gated
on a per-region AppConfig role flag that operators flip during
failover. NOT DynamoDB global tables; NOT S3 Cross-Region Replication.

### Key Points

- **Decision 1**: Application-layer dual-write via EventBridge,
  NOT managed cross-region replication.

  Each write goes through `shared.dual_write` which performs the local
  write through boto3 first, then emits a versioned EventBridge
  envelope to a local replication bus. A cross-region rule on that bus
  forwards events to the other region's bus, where a small applier
  Lambda replays the operation against local DDB / S3 / S3 Vectors.

  Why not global tables: the Oct 2025 us-east-1 outage demonstrated
  that managed cross-region replication services have shared control
  planes whose failure modes propagate across regions. Application-
  layer dual-write keeps the replication logic in code we control and
  on a transport (EventBridge) whose retry + DLQ semantics are
  observable + replayable from operator tooling.

- **Decision 2**: Per-region AppConfig flag, NOT global flag.

  Each region's AppConfig holds a single `active` / `passive` value
  describing what THAT region is. Failover is two independent
  operator-driven flips (set secondary to `active`, set primary to
  `passive`). Both-passive is a valid intermediate state; both-active
  is split-brain and `make ha-status` flags it via the
  `activated_at` timestamps.

  Why not a global flag: a global flag introduces the question "what
  if the flag service is down in one region during failover?" — the
  per-region design sidesteps it. Operators flip each region
  independently through the AWS AppConfig console; no terraform run
  during failover.

- **Decision 3**: Synchronous-local, asynchronous-remote write
  semantics.

  Writers commit locally first, then best-effort emit the replication
  event. An emit failure is logged and swallowed; the local write is
  the source of truth. EventBridge's own retry policy (185 attempts /
  24h max event age) handles transient failures; permanent failures
  land in the DLQ + the depth alarm pages.

  Why: a synchronous cross-region write would couple every customer
  request's latency to inter-region network health and would propagate
  cross-region failures into user-facing requests. Asynchronous
  replication means the active region keeps serving even if the
  passive is down — replication catches up when it recovers.

  Trade-off: ~5 minutes of replication lag is normal under load. A
  failover flip with lag <5 min loses at most one scheduler tick's
  worth of work, which the next tick on the new-active reconciles.

- **Decision 4**: Local-region reads.

  Chat, MCP, find_callers, list_workspace_documents, and the audit
  dashboard all read from whichever region's URL the user hits. They
  do NOT consult AppConfig before serving a request.

  Why: failover is operator-driven and rare; users hitting a region's
  URL should get an answer from that region's data, which the
  operator can audit locally without cross-region trace plumbing. If
  the active region is hot and the passive is N minutes behind, users
  in the passive region see slightly stale data — acceptable for the
  regulated-audit use case where "answers from data you can audit
  locally" is more valuable than "answers from the latest data
  anywhere."

- **Decision 5**: Idempotency via existing ConditionExpression
  patterns; audit-log SK uniqueness via random request_id +
  `attribute_not_exists(SK)` guard.

  Most platform writers go through helpers (`shared.sha_truth`,
  `shared.symbol_graph_writer`) that already include
  `attribute_exists` / `attribute_not_exists` guards. The applier's
  replay collides on these conditions and the
  ConditionalCheckFailedException is treated as "already applied" —
  the natural idempotency the platform already had carries over to
  replay.

  Audit-log put_items historically had no condition. We added
  `attribute_not_exists(SK)` and rely on the 8-byte random request_id
  in the SK for uniqueness so legitimate distinct rows never collide
  but replays hit CCF.

### Example Implementation

```python
# Writer (any Lambda that touches a dual-replicated resource):
from shared import dual_write

dual_write.ddb_put_item(
    table_name=WORKSPACES_TABLE,
    item={...},
    ConditionExpression="attribute_not_exists(SK)",
)
# 1. Local PutItem with the CE.
# 2. EventBridge PutEvents emit to local replication bus.
# 3. Cross-region rule forwards to other region.
# 4. Other region's applier Lambda replays the operation; CE protects
#    against double-apply on EventBridge retry.

# Scheduler (any cron-driven Lambda):
from shared.active_region import is_active_region

def lambda_handler(event, context):
    if not is_active_region():
        return {"statusCode": 200, "body": "passive-region-skip"}
    # ... normal work
```

## Consequences

**Positive:**
- Failover survives any single-region AWS outage; the passive region
  serves chat / MCP independently and becomes the new active with a
  human-flipped AppConfig deployment.
- Replication transport is observable (EventBridge metrics + DLQs) and
  replayable (`make ha-replay-dlq`).
- Single-region deployments are unaffected — every secondary resource
  is gated on `secondary_region != ""`, and the dual_write emit is a
  no-op when `REPLICATION_EVENT_BUS_NAME` is unset.

**Negative:**
- Doubles AWS resource count when HA is on (every DDB table / S3
  bucket / SQS queue / KMS key gets a regional twin). Acceptable
  cost: empty DDB tables, S3 buckets, and SQS queues are effectively
  free.
- Operators must populate SecureString secrets in BOTH regions
  manually. `make ha-list-secrets` enumerates what's left.
- ~5 minutes of replication lag is the steady-state expectation. Apps
  with lower-lag requirements would need a different design.
- Same Bedrock model required in both regions. Operator pre-deploy
  check is the gate; terraform doesn't validate model availability.

## Alternatives Considered

- **DynamoDB Global Tables**: rejected. The Oct 2025 us-east-1
  control-plane outage demonstrated that managed replication services
  share control planes whose failure modes cross region boundaries.
  Application-layer dual-write keeps replication logic in code we
  control and on a transport we can replay from.
- **S3 Cross-Region Replication**: rejected for the KB bucket.
  Introduces eventual-consistency we don't want under failover, and
  the asynchronous nature means a fresh write in the primary may not
  be visible in secondary at failover moment. Dual-write + cross-region
  GetObject at apply time keeps the failover semantics under
  application control.
- **Cross-region SQS access**: rejected. SQS has no native cross-region
  access; each region's writers must write to local queues. Hence the
  symmetric queue topology with the replicator-applier feeding the
  passive region's tables.
- **Single-region with Aurora/Snowflake replication**: rejected as a
  base architecture; some customers may layer it on top of the
  application-managed HA but the platform itself doesn't depend on it.

## Addendum (2026-05-20): Passive is fully passive

This addendum supersedes a sub-decision recorded in the
**2026-05-18 Compute-plane HA addendum** below:

> Original (2026-05-18): "Backend mutation guard via
> `shared.region_guard.guard_active()` on every admin mutation route
> in workspace-management and the 9 *-integration Lambdas."

That enumeration deliberately excluded the chat handler. The
exclusion was justified on the reasoning that chat memory writes are
*user-facing* (not admin), and dual-write would replicate them
transparently — gating would "defeat the purpose of HA." The exclusion
is **superseded as of 2026-05-20**.

### What changed and why

Live testing on 2026-05-19 surfaced two issues:

1. **The exclusion was aspirational.** A code audit revealed that
   chat (and ~25 other handlers covering ~130 write call sites) wrote
   directly via boto3 `table.put_item` etc., never through
   `shared.dual_write`. The "chat memory replicates transparently"
   claim was never true; the secondary region only held whatever
   `scripts/ha-bootstrap.py` had snapshot-copied. The follow-up plan
   (`plan/dual-write-completion`, commits `6b7c982`, `95d914e`,
   `0dd309e`, `95ea4aa`, `55747b2`) routes every handler through
   `dual_write` to actually replicate.

2. **Ungated chat would require bidirectional replication.** The
   original reasoning conflated two access patterns. End users hit
   the **canonical URL** `outcomeops.<domain>`, which DNS-flips to
   whichever region is currently active. The only callers on the
   regional hostnames (`outcomeops1` / `outcomeops2`) are operators
   using documented escape hatches for verification — they don't
   need to write chats, they need to read. Keeping chat ungated
   would force the system to support writes originating in either
   region, which means replay-idempotent `ConditionExpression` on
   every chat write, concurrent-write conflict resolution
   (browser tabs in both regions racing on the same conversation),
   and audit/analytics/spend tracking all inheriting that
   bidirectional complexity.

Gating chat collapses the architecture to **one-way replication**
(active → passive) for every handler. Mid-failover, the old-active
region (now `passive` per AppConfig but still receiving traffic
until DNS catches up) returns 403 from `region_guard`, correctly
preventing split-brain. The 403 window is seconds-to-minutes; users
retry, DNS settles, they reach the new active. This is the SAME
behavior already shipped for workspace-management mutations and the
9 integration handlers — chat now behaves identically.

### Updated gated-routes list

Replaces the 2026-05-18 enumeration:

- **workspace-management** — all admin mutation routes (workspaces,
  members, repos, org members, MCP-server CRUD, API keys, system
  prompts, budgets, privacy mode).
- **9 *-integration handlers** — github, confluence, jira, outlook,
  teams, sharepoint, onedrive, onenote, database. Connect, update
  selected scopes, trigger sync, disconnect.
- **chat** *(new in this addendum)* — `PUT /conversations/{id}/share`,
  `DELETE /conversations/{id}`, and the streaming chat path
  (`handle_chat_stream`, which writes new conversation rows + each
  message). Read routes (`GET /conversations`, `GET
  /conversations/{id}`, `GET /shared`) stay ungated.

Blocked attempts on any gated route return HTTP 403 with body
`{"error": "region_passive", ...}` and emit an audit row with action
`ADMIN_MUTATION_BLOCKED_PASSIVE`. For the chat streaming path the
403 is delivered as an NDJSON error event (the streaming envelope's
in-band failure shape) rather than an HTTP status — the audit row is
identical.

### Implementation commits

Chunks 01–05 of the dual-write-completion follow-up:

- `6b7c982` workspace-management writes through `dual_write` + the
  unit-scoped test conftest fixture that lets existing handler-layer
  mocks intercept dual_write writes.
- `95d914e` 9 integration handlers through `dual_write`.
- `0dd309e` sync workers + file-ingestion through `dual_write`
  (plus the `dynamodb` / `s3_client` module-global cases in the
  conftest resolver).
- `95ea4aa` process-pr-check + mcp-server (Fargate container)
  through `dual_write`. Analytics + security-scanner stay direct as
  per-region derived state (documented in commit body).
- `55747b2` chat handler through `dual_write` AND `region_guard` on
  the three mutation entry points, with new unit-test coverage in
  `lambda/tests/unit/test_chat_region_guard.py`.

After this addendum, **every handler writing replicable state goes
through `dual_write`** and **every user-facing mutation route is
gated by `region_guard`**. The only intentional exclusions are
documented in the chunk-04 commit body (per-region derived state).

## Addendum (2026-05-18): Compute-plane HA

The initial ADR scoped HA to the **data plane** (DDB / S3 / S3 Vectors
replication + AppConfig role flag). For full service continuity the
**compute plane** (Lambdas + Fargate + ALB + DNS) must also be
replicated. This addendum documents that extension; the data-plane
decisions above are unchanged.

### Decisions

- **Replicate every runtime resource into the secondary region** when
  `secondary_region` is set in tfvars. The complete list is enumerated
  in [docs/HA.md → Compute-plane HA](../HA.md#compute-plane-ha-full-service-continuity);
  it covers UI Fargate, all 9 *-integration Lambdas, chat-streaming +
  workspace-management, writer Lambdas, schedulers, sync workers,
  background Lambdas (audit-alert, analytics-aggregator,
  analytics-cost-alert), and the MCP + scanner Fargates with a Cloud
  Map private DNS namespace per-region.

- **Passive-mode gating via `is_active_region()`** for resources whose
  side effects would double otherwise:
    - Schedulers, audit-alert, audit-stream-publisher, and
      analytics-cost-alert return early when passive.
    - analytics-aggregator runs in BOTH regions because each region
      maintains its own analytics-table for region-local dashboards.
    - replication-applier runs in BOTH regions (it's the receive side
      of dual-write).

- **Backend mutation guard via `shared.region_guard.guard_active()`**
  on every admin mutation route in workspace-management and the 9
  *-integration Lambdas. Blocked attempts return HTTP 403 and emit an
  audit row with action `ADMIN_MUTATION_BLOCKED_PASSIVE`. The audit
  trail makes a flip-window mis-click visible after the fact.

- **UI passive-mode signal via `/api/region/role`** — the UI fetches
  region role at app load and renders a sticky amber banner when
  passive. The most prominent mutation buttons auto-disable via a
  `<MutationButton>` wrapper. Belt-and-suspenders: the UI hides the
  button, the backend returns 403 if anyone hits the API directly.

- **DNS topology**: two stable region-pinned hostnames
  (`outcomeops1.<domain>`, `outcomeops2.<domain>`) as admin escape
  hatches, plus a canonical hostname (`outcomeops.<domain>`) whose
  Route 53 A-record alias is flipped during failover. The canonical
  hostname is the only one OAuth apps know about, so failover does NOT
  require re-registering any OAuth integration. Each ALB's ACM cert
  carries SANs for both its region-pinned hostname and the canonical
  hostname.

- **ECR cross-region replication** via
  `aws_ecr_replication_configuration` so primary-built UI / MCP /
  scanner images appear in the secondary region within minutes. Each
  region's Fargate services pull from their own region's registry.

- **Failover is a three-step operator action**: flip both AppConfig
  role flags AND flip the canonical DNS A-record. All three are
  load-bearing; skipping any of them leaves the system in a
  half-flipped state. Procedure detail lives in [docs/HA.md](../HA.md).

### Why these particular gates

Replication carries audit writes between regions. Without
`is_active_region()` on audit-alert, every refusal would trigger TWO
SNS emails. Without it on analytics-cost-alert, every daily roll-up
would email twice. Without it on audit-stream-publisher, customer
SIEMs would see every event twice. Schedulers are gated so cron-style
work runs in one place — running github-scheduler in both regions
would double every PR-analysis trigger.

analytics-aggregator is INTENTIONALLY ungated: it materializes
audit-rows into per-day rollups in DDB, which we want available in
the local region's dashboards regardless of which region is currently
active. Both regions process their own DDB stream into their own
analytics-table.

### Out of scope (for this addendum)

- **Multi-region KMS for OAuth tokens** — OAuth access/refresh tokens
  are encrypted in the primary region's KMS key; the secondary region
  cannot decrypt without an `aws_kms_replica_key`. Failover today
  works for everything except actively-mid-flight OAuth refresh in
  the now-active region. Tracked separately as plan-ha chunk 13.

- **Kinesis cross-region stream for OCSF export** — the OCSF
  audit-stream-publisher is currently disabled in the owner's prd
  (`audit_stream_export_enabled = false`). When a customer turns on
  SIEM export AND HA, a Kinesis stream must be created in the
  secondary region and `audit-stream-publisher` ungated. That's a
  future chunk.

## References

- [docs/HA.md](../HA.md) — operator runbook
- `terraform/appconfig.tf` — per-region AppConfig role flag
- `terraform/eventbridge-replication.tf` — bus + cross-region rule + DLQ
- `terraform/lambda-replication-applier.tf` — replicator Lambda
- `lambda/shared/dual_write.py` — writer-side helper
- `lambda/shared/active_region.py` — scheduler-side helper
- `lambda/shared/region_guard.py` — mutation-route guard
- `lambda/shared/audit_writer.py` — `ADMIN_MUTATION_BLOCKED_PASSIVE` action
- `lambda/replication-applier/handler.py` — applier
- `ui/src/lib/regionRole.ts` — UI region-role client
- `ui/src/components/RegionBanner.tsx` — passive-mode banner
- `ui/src/components/MutationButton.tsx` — auto-disabling button
- `scripts/ha-status` — operator status CLI
- `scripts/ha-replay-dlq` — DLQ drain CLI
- `scripts/ha-list-secrets` — SSM SecureString population check
- `scripts/ha-bootstrap` — add-later initial-sync script
