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

## References

- [docs/HA.md](../HA.md) — operator runbook
- `terraform/appconfig.tf` — per-region AppConfig role flag
- `terraform/eventbridge-replication.tf` — bus + cross-region rule + DLQ
- `terraform/lambda-replication-applier.tf` — replicator Lambda
- `lambda/shared/dual_write.py` — writer-side helper
- `lambda/shared/active_region.py` — scheduler-side helper
- `lambda/replication-applier/handler.py` — applier
- `scripts/ha-status` — operator status CLI
- `scripts/ha-replay-dlq` — DLQ drain CLI
- `scripts/ha-list-secrets` — SSM SecureString population check
