# ADR-018: Audit Logging Design

## Status: Accepted

## Context

OutcomeOps AI Assist deploys into customer AWS accounts and serves
Fortune 500 enterprises. Those customers' InfoSec, compliance, and
SOC 2 / FedRAMP review processes require defensible answers to
questions like:

- Who created or revoked an API key, when, and from what IP?
- Which user added or removed a workspace member?
- Which prompt did a user submit just before the safety layer
  refused them?
- Across the org, how many critical actions happened in the last 30 days?

Before this design landed, the platform wrote two flavors of audit row
to a single DynamoDB table (`AUDIT_TABLE`):

1. **Bedrock invocations** via `lambda/shared/audit.py` — full prompt
   and response captured, refusal detection wired through, but no
   `source_ip` and no admin-action context.
2. **System prompt updates** via an inline `_write_audit_log` helper
   in `workspace-management/handler.py` — narrow, only two call sites,
   no IP capture, easy to forget when adding new admin endpoints.

Every other admin-and-security-relevant action was untracked: workspace
CRUD, member CRUD, repo CRUD, MCP server CRUD, **API key CRUD**, budget
changes. No row carried IP address. There was no audit UI.

This blocked the InfoSec narrative ("we log every action with timestamp,
actor, and IP") and created real audit gaps for any customer who
needed compliance answers.

### Constraints

- **Single-tenant per deployment.** OutcomeOps deploys into the
  customer's own AWS account, so we don't need a multi-tenant SaaS-style
  admin/user split. One `AUDIT_TABLE` with a category discriminator
  suffices.
- **Established codebase conventions.** PK/SK pattern with `WS#` /
  `USER#` / `LAMBDA#` prefixes, ISO-8601 timestamps in SK,
  `secrets.token_hex(8)` request IDs, 365-day TTL via epoch seconds.
- **DynamoDB schema flexibility.** No new tables, no migration —
  the new design is purely additive attributes on existing rows plus
  a new GSI.
- **ADR-009 Decimal handling.** Numeric fields summed downstream must
  be `Decimal`-compatible strings.

## Decision

The audit table is the single source of truth for both Bedrock
invocations and admin actions. Rows are discriminated by a `category`
attribute, partitioned in three indices for the three primary access
patterns, rolled up by the analytics-aggregator into pre-computed
counters, and exposed via four GET endpoints to a dedicated React
Audit dashboard with a drill-into-user flow and CSV/JSON export.

### Key Points

- **Single table, three categories.** Every audit row carries
  `category ∈ {bedrock, admin, auth}`. `bedrock` covers chat / RAG /
  codegen / refusal / rerank. `admin` covers the 17 mutating workspace
  / member / repo / MCP / API-key / budget / system-prompt actions.
  `auth` is reserved for SSO callbacks (not in v1).

- **Three indices, three access patterns.** The base table partitions
  by workspace (`PK=WS#<id>`); GSI1 partitions by user
  (`GSI1PK=USER#<email>`); GSI2 partitions by category
  (`GSI2PK=CAT#<category>`). The audit dashboard's "all admin actions
  org-wide" query reads GSI2; the "drill into Joe's chat history"
  query reads GSI1; per-workspace rollups live on the base table.

- **Two writer modules, one schema.** `shared/audit.py:log_audit_entry`
  writes Bedrock rows; `shared/audit_writer.py:log_admin_action` writes
  admin/auth rows. They share row shape and key conventions. Splitting
  the writers keeps each focused — Bedrock cares about model_id, tokens,
  cost, refusal detection; admin cares about action constants, details
  payload, and request-context attribution. Both are non-fatal: an
  audit-write failure logs a warning and never breaks the user request.

- **Action vocabulary frozen at 17 SCREAMING_SNAKE_CASE constants.**
  Listed in `shared/audit_writer.ADMIN_ACTIONS`. Adding a new action is
  a one-line addition to the constants module + a label entry in the
  UI's tone map; no migration. Changing one breaks any saved
  aggregator output and dashboard filters.

- **Browser IP / UA captured end-to-end.** The Express UI proxy forwards
  `x-real-source-ip` + `x-real-user-agent` (set from `req.ip` after
  `app.set("trust proxy", true)`) to every signed Lambda Function URL
  call. The Lambda's `main.py:_build_event` reads those headers (with
  `x-forwarded-for` / `user-agent` fallbacks) and populates
  `requestContext.http.sourceIp`/`userAgent` on the synthesized v2
  event. Both `extract_request_context` (admin) and the chat handler's
  request-context lookup read from there.

- **Pricing is operator-tunable per env.** Token-based models
  (Sonnet, Haiku, Titan Embed) take per-1M-token prices via env vars
  driven by `tfvars`. Per-call models (Cohere Rerank) take a per-1k
  search-query price. `_calculate_cost` handles both shapes; the
  unified MODEL_PRICING dict is the single source of pricing truth.

- **Aggregator is read-time-O(1).** `analytics-aggregator` is a
  DynamoDB Streams consumer that emits six rollup row shapes:
  - Bedrock: `DT#<date>` daily totals, `DM#<date>#<model>` by model,
    `DU#<date>#<user>` by user.
  - Admin: `DA#<date>` daily total, `DC#<date>#<action>` by action,
    `DCU#<date>#<user>` by user.
  Idempotency via a `processed_requests` string-set on every rollup
  item — stream replays don't double-count.

### Schema (canonical row shapes)

**Admin row**:
```json
{
  "PK": "WS#<workspace_id>",
  "SK": "<iso_timestamp>#<request_id>",
  "GSI1PK": "USER#<email>",
  "GSI1SK": "<iso_timestamp>#<request_id>",
  "GSI2PK": "CAT#admin",
  "GSI2SK": "<iso_timestamp>#<request_id>",
  "category": "admin",
  "action": "API_KEY_CREATED",
  "workspace_id": "ws_abc",
  "user_email": "brian@example.com",
  "source_ip": "203.0.113.42",
  "user_agent": "Mozilla/5.0 ...",
  "request_id": "<hex>",
  "timestamp": "2026-05-04T18:42:11.123456+00:00",
  "details": { "key_id": "kid_xyz", "name": "ci-bot" },
  "ttl": 1746388931
}
```

**Bedrock row** (existing shape, extended with category + GSI2 + IP):
```json
{
  "PK": "WS#<workspace_id> | WS#SYSTEM",
  "SK": "<iso_timestamp>#<request_id>",
  "GSI1PK": "USER#<email> | LAMBDA#<name>",
  "GSI1SK": "<iso_timestamp>#<request_id>",
  "GSI2PK": "CAT#bedrock",
  "GSI2SK": "<iso_timestamp>#<request_id>",
  "category": "bedrock",
  "lambda_name": "chat",
  "model_id": "us.anthropic.claude-sonnet-4-6",
  "input_text": "...",
  "output_text": "...",
  "input_tokens": 1234,
  "output_tokens": 567,
  "cost_usd": "0.0234",
  "duration_ms": 1500,
  "status": "success | error | refusal",
  "flagged_reason": "",
  "user_email": "brian@example.com",
  "workspace_id": "ws_abc",
  "source_ip": "203.0.113.42",
  "user_agent": "Mozilla/5.0 ...",
  "timestamp": "2026-05-04T18:42:11.123456+00:00",
  "ttl": 1746388931
}
```

### Why GSI2 (and not just FilterExpression)

The most common dashboard query is "show me all admin actions org-wide,
newest first, in a date range." Without GSI2 that's a Scan +
FilterExpression, which DynamoDB applies in two stages: read up to
`Limit` items by partition order, then evict non-matches. With chat /
RAG / batch-summary writes dominating the table by 10–100×, the first
50–500 items in any partition are likely all bedrock successes; a
sparse `category=admin` filter then evicts everything and returns zero
matches.

GSI2 partitions by category directly, so the same query becomes a
single Query against `CAT#admin` reading only admin rows — O(matches)
instead of O(table-size). The same partition pattern works for refusals
(`CAT#bedrock` Query with `FilterExpression: status=refusal`, with
internal pagination to find the sparse matches), and for any future
`CAT#auth` rollup.

### Why a per-call cost shape

Cohere Rerank 3.5 on Bedrock prices per "search query" (one rerank
invocation = one search query, regardless of doc count up to platform
limits) — not per token. `_calculate_cost` learned a second pricing
shape (`{"per_call": $/invocation}`) alongside the existing
`{"input": $/1M, "output": $/1M}`, and `MODEL_PRICING.get(model_id)`
hands back whichever fits. Adding more per-call models in the future
(e.g., Titan Image, model embedding via cross-encoder) is one line in
MODEL_PRICING.

### Example: emitting an admin row from a handler

```python
from shared.audit_writer import ACTION_API_KEY_CREATED, log_admin_action

def create_api_key(user_email, body, audit_ctx=None):
    # ... validation, persistence ...
    ctx = audit_ctx or {}
    log_admin_action(
        AUDIT_TABLE,
        ACTION_API_KEY_CREATED,
        workspace_id="",
        user_email=user_email,
        source_ip=ctx.get("source_ip", ""),
        user_agent=ctx.get("user_agent", ""),
        details={
            "key_id": key_id,
            "name": request.name,
            "expires_at": expires_at,
        },
    )
    return _response(201, response_body)
```

The `audit_ctx` dict is extracted once per Lambda invocation in
`lambda_handler` via
`audit_ctx = extract_request_context(event)` and threaded through to
every mutating handler. Helpers default to `None` so partial wiring or
unit tests work without rebuilding the whole event.

### Example: querying admin actions org-wide

```python
response = audit_table.query(
    IndexName="GSI2",
    KeyConditionExpression="GSI2PK = :pk AND GSI2SK BETWEEN :s AND :e",
    ExpressionAttributeValues={
        ":pk": "CAT#admin",
        ":s": "2026-04-01",
        ":e": "2026-05-01~",
    },
    ScanIndexForward=False,
    Limit=50,
)
```

For sparse filter cases (e.g., `category=bedrock` + `status=refusal`),
the handler internally paginates with `_paginated_filtered_query`
(page size 500, max 10 pages) until `target_limit` matches accumulate.
DDB's Limit-before-FilterExpression behavior would otherwise return 0
when the first page is dominated by non-matches.

## Consequences

### Positive

- **One row per action.** The audit table is the canonical record;
  rollups are just precomputed views of it. A misbehaving aggregator
  doesn't lose data.
- **Three primary access patterns covered by indices, not by Scan.**
  Per-workspace, per-user, and per-category queries are all O(matches).
  Even on a multi-million-row table, the dashboard renders in well under
  a second.
- **Writer-side ergonomics.** Adding a new admin endpoint is one
  function call to `log_admin_action(...)` plus a constant. No new
  endpoints, no aggregator changes, no UI changes (the action shows
  up in the by-type chart automatically; pill colors fall back to
  `unknown` until a tone is assigned).
- **InfoSec demo lands in 90 seconds.** Total Actions card, daily
  trend, top actors, and a click-to-drawer with IP + UA + request_id +
  copy buttons closes the "every action with timestamp, actor, IP"
  narrative in one screen.
- **Pricing is operator-controlled, not code-controlled.** Customers
  on negotiated AWS pricing override `BEDROCK_*_PRICE` in tfvars
  without a code change.

### Tradeoffs

- **GSI2 hot partition risk.** `CAT#admin` and `CAT#bedrock` are each
  single shared partitions. DynamoDB on-demand sustains ~1000
  WCU/partition. Even the noisiest enterprise won't hit 1000 admin
  actions/sec; bedrock could in theory if RAG fan-out goes wild, but
  on-demand auto-scales. If hot partitioning ever bites, shard via
  `CAT#bedrock#<hash>` with a backfill — not pre-built.
- **Sparse-filter pagination cost.** When the dashboard queries
  `CAT#bedrock` for refusals, the handler may scan up to 5,000 items
  (10 × 500-row pages) per request before giving up. With ~$0.25 per
  million RCU on on-demand, that's ~$0.00125 per dashboard load. Trivial
  for an admin dashboard; would matter for a customer-facing feature.
- **Two writer modules.** `shared/audit.py` (Bedrock) and
  `shared/audit_writer.py` (admin) are separate. New developers have
  to know which to call. Mitigation: `audit_writer.log_admin_action`
  is the only entry-point for admin rows and the action constants are
  enumerated there; misuse fails fast at import.
- **`status` and `flagged_reason` are bedrock-only on the envelope.**
  The handler's `_audit_event_from_item` projects them only when set;
  admin rows never have them. UI code that conditions on
  `event.status === 'refusal'` is correct precisely because admin rows
  never set `status`.

### When you might reconsider this

- A customer requires **per-IP rollups** ("alert on >100 actions from
  one IP per hour"). Add a `DI#<date>#<ip>` rollup shape in the
  aggregator without changing the audit row schema.
- A customer requires **separate retention** for admin vs bedrock
  (e.g., 7-year SOC 2 hold on admin, 90-day GDPR on bedrock). Either
  set TTL per-category at write time, or split into two tables and
  fan out streams. The current 365-day TTL is the same for both.
- A customer requires **encryption-at-rest with a customer-managed
  KMS key** beyond AWS-managed defaults. The DDB table already
  supports the `local.data_cmk_arn` toggle; flip it on.

## Implementation

### Starting today (already shipped this session)

1. `lambda/shared/audit_writer.py` with `log_admin_action` + 17 action
   constants.
2. `terraform/dynamodb-audit.tf` adds GSI2 (`GSI2PK=CAT#<category>`).
3. `lambda/workspace-management/handler.py` wires `audit_ctx` through
   13 mutating endpoints + 4 audit query endpoints; `lambda_handler`
   bootstraps `extract_request_context(event)`.
4. `lambda/analytics-aggregator/handler.py` branches on `category`
   to emit DA/DC/DCU rollups alongside the existing DT/DM/DU.
5. `lambda/shared/audit.py` extended to set GSI2 keys + accept
   `source_ip`/`user_agent` so chat / RAG / refusal rows attribute
   correctly.
6. UI: `ui/src/pages/Audit.tsx` with stat cards, daily trend, by-type
   bars, by-workspace bars, top actors, recent activity, drawer,
   user-history drill (1h/3h/24h), CSV/JSON export.
7. Demo seeder at `scripts/backfill-demo-audit-data.py` for repeatable
   customer demos (~$270/30d in seeded data with a realistic
   80/20 Haiku/Sonnet mix and ~70% rerank coverage).

### Next phases

1. **`auth` category wiring.** SSO callbacks (Azure AD OIDC) emit
   `LOGIN_SUCCESS` / `LOGIN_FAILURE` / `LOGOUT` admin rows under
   `CAT#auth`. Same writer (`log_admin_action(category="auth", ...)`),
   same dashboard surface.
2. **Per-IP aggregation.** Add `DI#<date>#<ip>` rollup so InfoSec can
   filter "all activity from one IP in 24h" without scanning the base
   table.
3. **Real-time refusal alerting beyond SNS.** The audit-alert Lambda
   already SNS's on every refusal; future work routes to
   PagerDuty / Slack via Lambda fan-out, with a demo-seed skip so
   bulk seeding doesn't page operators.
4. **External SIEM forwarding.** Stream consumer that ships every new
   audit row to the customer's Splunk / Datadog / OpenSearch.
   Out of scope for v1 because the customer's CMK + retention
   posture varies; we'd need a per-customer config surface.

## Related ADRs

- ADR-001: Create ADRs (this is one).
- ADR-007: Documentation-driven decisions — this ADR exists because
  the audit-logging design has decisions that won't be reconstructible
  from the code alone (why GSI2, why the writer split, why per-call
  pricing).
- ADR-009: Decimal handling — `cost_usd` strings; rollup ADD-updates
  use `Decimal`.
- ADR-013: Lambda test import structure — the audit tests follow this.

## References

- `lambda/shared/audit.py` — Bedrock writer + pricing.
- `lambda/shared/audit_writer.py` — admin writer + action constants.
- `lambda/workspace-management/handler.py` — admin emission +
  `/audit/*` query endpoints.
- `lambda/analytics-aggregator/handler.py` — DA/DC/DCU + DT/DM/DU.
- `terraform/dynamodb-audit.tf` — table + GSI1 + GSI2.
- `ui/src/pages/Audit.tsx`, `ui/src/components/AuditEventDrawer.tsx`,
  `ui/src/lib/auditExport.ts` — dashboard, drawer, export.
- `scripts/backfill-demo-audit-data.py`,
  `scripts/backfill-audit-gsi2.py`,
  `scripts/backfill-audit-source-ip.py`,
  `scripts/backfill-rerank-cost.py` — demo seed + remediation.
- `docs/audit-dashboard.md` — operator guide.

Version History:

- v1.0 (2026-05-05): Initial decision documenting the audit-logging
  expansion: categories, GSI2 partitioning, writer split,
  request-context threading, per-call pricing, the dashboard, and
  the drill / export flows.

<!-- Confluence sync -->
