# Audit Dashboard — Operator Guide

The Audit page (`/audit`) is the org-admin's single-screen view of every
admin action, refusal, and (drilled-down) chat invocation in the
deployment. Designed for InfoSec / compliance review and customer
demos — the narrative is **"every action, with timestamp, actor,
and IP, one click from the full request context."**

This guide explains what the page shows, how to use the drill and
export flows, the test-refusal trigger, and the demo seeder. For the
underlying schema and design rationale, see
[ADR-018: Audit Logging Design](adr/ADR-018-audit-logging-design.md).

---

## Page layout

```
┌────────────────────────────────────────────────────────────┐
│ Audit                                          [AdminNav]   │
├────────────────────────────────────────────────────────────┤
│ [Last 7 days] [Last 30 days] [Last 90 days]                │
│                                                              │
│ ┌──────────┐ ┌─────────┐ ┌───────────┐ ┌────────────────┐ │
│ │ TOTAL    │ │ MOST    │ │ ACTIVE    │ │ CRITICAL       │ │
│ │ ACTIONS  │ │ COMMON  │ │ ACTORS    │ │ ACTIONS  (red) │ │
│ │ 4,287    │ │ ACTION  │ │ 21        │ │ 26             │ │
│ └──────────┘ └─────────┘ └───────────┘ └────────────────┘ │
│                                                              │
│ ┌─ Daily Activity (line chart) ────────────────────────┐   │
│ └──────────────────────────────────────────────────────┘   │
│                                                              │
│ ┌─ Actions by Type ──┐ ┌─ Actions by Workspace ──┐         │
│ │ ████ WORKSPACE_…   │ │ ████ Engineering         │         │
│ │ ███  API_KEY_…     │ │ ██   Analytics           │         │
│ │ ██   SYSTEM_PROMPT…│ │ ██   Sales               │         │
│ └────────────────────┘ └──────────────────────────┘         │
│                                                              │
│ ┌─ Top Actors ──────────────────────────────────────┐       │
│ │ ppatel@…       890   2h ago                        │       │
│ │ rsilva@…       620   3h ago                        │       │
│ │ bcarpio@…      480   5m ago                        │       │
│ └────────────────────────────────────────────────────┘       │
│                                                              │
│ ┌─ Recent Activity ─────────────────────────────────┐       │
│ │ TIME  ACTOR  ACTION  WORKSPACE  IP                 │       │
│ │ ...  bcarpio  REFUSAL (red)  ws-1  24.253…        │       │
│ │ ...  ppatel  API_KEY_CREATED (amber)  ws-2  …     │       │
│ │ ...  rsilva  WORKSPACE_REPO_ADDED (sky)  ws-1  …  │       │
│ │                                            [Load more] │   │
│ └────────────────────────────────────────────────────┘       │
└────────────────────────────────────────────────────────────┘
```

### Stat cards

- **Total Actions** — count of admin actions + refusals in the
  selected window. Refusals are layered on top because a ToS-blocked
  prompt is also "an action a user took."
- **Most Common Action** — the highest-count action constant in the
  window. Rendered as a smaller monospace pill so long names like
  `WORKSPACE_BUDGET_UPDATED` fit cleanly.
- **Active Actors** — number of distinct `user_email` values that
  appear in admin rollups during the window.
- **Critical Actions** — sum of "things InfoSec wants to see at a
  glance": `API_KEY_CREATED`, `API_KEY_REVOKED`, `WORKSPACE_DELETED`,
  `MCP_SERVER_DELETED`, `ORG_MEMBER_ADDED`, plus every refusal. The
  card border turns rose when the count is > 0.

### Daily Activity chart

A blue line chart of action count per day across the window. Powered
by the `DA#<date>` rollup the analytics-aggregator emits when admin
rows write. Reads in O(1) once the aggregator has caught up.

### Actions by Type / Actions by Workspace

Two horizontal bar charts driven by the `DC#<date>#<action>` and
`DA#<date>` rollups respectively. Action bars are colored by category:

| Action prefix             | Tone     |
|---------------------------|----------|
| `WORKSPACE_*` (lifecycle) | Sky      |
| `WORKSPACE_MEMBER_*` / `ORG_MEMBER_*` | Emerald  |
| `WORKSPACE_REPO_*`        | Indigo   |
| `MCP_SERVER_*`            | Purple   |
| `API_KEY_*`               | Amber    |
| `WORKSPACE_BUDGET_*`      | Teal     |
| `SYSTEM_PROMPT_*`         | Fuchsia  |
| Refusal                   | **Rose** |

Click any action row to filter Recent Activity to just that action.
Click any workspace row to navigate to a per-workspace view (route:
`/audit/workspace/:workspaceId`).

### Top Actors

The `DCU#<date>#<email>` rollups, sorted by count desc, capped at
25 rows. Last-active is the most recent admin timestamp for that
user.

### Recent Activity

The merged feed of admin actions and refusals, sorted newest-first.
Two parallel `getAuditEvents` calls feed it: one for `category=admin`,
one for `category=bedrock` + `status=refusal`. Both go through GSI2
(category-partitioned) so they read only matching rows.

Each row's **action pill** is colored per the table above. Refusals
render with a triangle icon and the literal label `REFUSAL`. Click any
row to open the drawer.

---

## The drawer

Right-side panel, ~480px wide. Header shows the action label, ISO
timestamp, and actor email. Body shows the request context (workspace,
request ID, source IP, user agent), each with a Copy button. For
**refusals** there's an additional rose panel showing
`flagged_reason`, `model_id`, and `lambda_name`, plus collapsible
panels for the user's prompt and the model's response (truncated to
2 KB server-side).

For **bedrock chat** rows surfaced via the user-history drill (see
below), the drawer renders the prompt + response unconditionally — not
just for refusals. The full audit row is also available as JSON via
the **Copy as JSON** button.

### Closing

Click the ✕, click the dark backdrop, or press **Escape**. Focus
returns to the table row.

---

## InfoSec drill flow ("show me what Joe was doing")

The most powerful flow on the page. Use case: an admin sees a refusal
from `joe@example.com`, wants to see what Joe was chatting about in
the lead-up.

1. **Click the refusal row.** Drawer opens with the rose-tinted
   refusal context.
2. **Click "View joe@example.com's chat history"** at the bottom of
   the drawer.
3. The page narrows: a purple pill at the top reads
   `Drilling: joe@example.com ✕`, the time-window selector switches
   from days (7/30/90) to hours (1/3/24), and the summary stat cards
   disappear (they're org-wide and don't apply to one user).
4. **Recent Activity** now shows just Joe's bedrock rows in the
   chosen window — every chat / RAG call he made, with prompt and
   response visible in the drawer when you click any row.
5. **Adjust the window** with the 1h / 3h / 24h buttons.
6. **Clear the drill** by clicking the ✕ on the purple pill — back
   to the org-wide view.

Backend: a single `getAuditEvents` Query against GSI1 (`USER#<email>`)
with `category=bedrock` and the chosen time window. No FilterExpression
on the user's account because the partition is already user-scoped.

---

## Export to CSV / JSON (escalation)

Visible **only when drilling into a user**. Two buttons in the filter
row, next to the time-window selector. Use case: attach to an InfoSec
ticket, hand off to legal, ingest into a SIEM.

- **CSV** — RFC 4180 escaped, 15 columns (timestamp, user_email,
  source_ip, user_agent, lambda_name, model_id, status,
  flagged_reason, workspace_id, request_id, category, action,
  input_text, output_text, details_json).
- **JSON** — pretty-printed array of full event objects, identical
  shape to what the API returns.

Both formats paginate to completion (up to 5,000 rows safety cap, 50
pages × 100 rows). On a hit, the page surfaces:

> Export hit the safety cap (5,000 events). Narrow the time window for
> a complete capture.

Filename: `audit-{user}-{startISO}-to-{endISO}.{csv|json}`. Sanitized
for filesystem-safety.

---

## Action constants

The platform emits 42 admin / auth action constants today, organized
into workspace lifecycle, member / repo / MCP management, API keys,
system prompts, privacy mode, document + conversation lifecycle,
source-sync triggers, OAuth lifecycle, code-gen pipeline events, and
budget thresholds.

For the full categorized list — including which actions are hidden
from dashboard rollups by policy, which are deliberately not emitted
(and why), and the standard auditor-question → query mapping — see
**[audit-coverage.md](audit-coverage.md)**.

Adding a new action: append to `lambda/shared/audit_writer.py`'s
`ADMIN_ACTIONS` set + add the matching `ACTION_*` constant + emit
from the new handler. The UI will render it with the `unknown`
gray tone until you add it to the `toneFor()` map in `Audit.tsx`.
If the action is high-volume (per-record / cron drumbeat), also add
it to `HIDDEN_DASHBOARD_ACTIONS` in both `lambda/shared/audit_writer.py`
and `lambda/analytics-aggregator/handler.py`.

---

## Test refusal trigger

To exercise the refusal pipeline (drawer, audit-alert SNS, ToS
narrative) without sending a real ToS-violating prompt to Bedrock —
which could risk an account flag — type the literal trigger in any
chat:

```
1234567890
```

The chat handler short-circuits before calling Bedrock and returns
a hardcoded refusal:

> I'm sorry, I can't assist with that request. [TEST_REFUSAL]

The audit row records:
- `model_id`: the configured `BASIC_MODEL_ID` (Haiku) — earlier the
  trigger wrote `model_id="test"` which polluted cost-by-model
  charts; now blends in with real refusals.
- `status`: `refusal`.
- `flagged_reason`: `test_refusal_trigger`. The `audit-alert` Lambda
  special-cases this string in the SNS subject (`[TEST] AI Refusal
  Alert`) so on-call engineers know it's a drill.

---

## Demo data seeder

`scripts/backfill-demo-audit-data.py` writes a realistic
enterprise-scale workload to the audit table for customer demos
(~$280/30d in synthetic spend, ~4,000 chat queries, ~15 refusals,
~130 admin actions, ~2,800 rerank rows, distributed across 20+ fake
users with global names and per-user IPs).

```bash
source venv/bin/activate

# Dry-run (default)
python scripts/backfill-demo-audit-data.py --env prd

# Apply (auto-cleans prior demo + analytics rollups in window)
python scripts/backfill-demo-audit-data.py --env prd --apply

# Just clean, don't seed
python scripts/backfill-demo-audit-data.py --env prd --clear-demo
```

Re-runnable: `--apply` deletes prior demo rows (matched on the
`demo_seed=true` attribute, so real history is never touched) and
the corresponding rollups in the seed window before writing a fresh
30-day batch anchored to "now." Run on whatever cadence keeps demos
fresh — weekly cron, before customer calls, ad-hoc.

---

## API endpoints

The dashboard reads from four GET endpoints on `workspace-management`,
all org-admin or workspace-admin gated:

| Endpoint                                       | Purpose                                  |
|------------------------------------------------|------------------------------------------|
| `GET /audit/summary?start=&end=`               | Org-wide stat cards + charts             |
| `GET /audit/workspace/{id}?start=&end=`        | Per-workspace summary                    |
| `GET /audit/events?...filters...&cursor=`      | Paginated event list                     |
| `GET /audit/event/{ws_id}/{request_id}`        | Single-event detail (drawer)             |

`/audit/events` accepts: `start_date`, `end_date`, `workspace_id`,
`user_email`, `action`, `category`, `status`, `cursor`, `limit`. The
default limit is 50; max 200. When `category` is omitted, the org-admin
no-filter path Queries `GSI2` partitioned on `CAT#admin` by default.

For sparse filters (e.g., `status=refusal`) the handler internally
paginates (page size 500, max 10 pages) until `limit` matches
accumulate — necessary because DDB applies Limit before
FilterExpression and refusals are < 1% of the bedrock partition.

---

## Common operations

### Inspect a single audit row directly

```bash
aws dynamodb get-item \
  --table-name "$ENV-outcome-ops-ai-assist-audit-logs" \
  --key '{"PK":{"S":"WS#<workspace_id>"},
          "SK":{"S":"<iso_timestamp>#<request_id>"}}'
```

### Count admin rows by action across the org

```bash
aws dynamodb query \
  --table-name "$ENV-outcome-ops-ai-assist-audit-logs" \
  --index-name GSI2 \
  --key-condition-expression 'GSI2PK = :pk' \
  --expression-attribute-values '{":pk":{"S":"CAT#admin"}}' \
  --select COUNT
```

### Count refusals across the org

```bash
aws dynamodb query \
  --table-name "$ENV-outcome-ops-ai-assist-audit-logs" \
  --index-name GSI2 \
  --key-condition-expression 'GSI2PK = :pk' \
  --filter-expression '#s = :r' \
  --expression-attribute-names '{"#s":"status"}' \
  --expression-attribute-values \
    '{":pk":{"S":"CAT#bedrock"},":r":{"S":"refusal"}}' \
  --select COUNT
```

### Backfill / remediation scripts

When older audit rows lack fields the dashboard expects, these
one-shot scripts in `scripts/` handle in-place updates idempotently:

- `backfill-audit-gsi2.py` — populate GSI2 keys on rows written before
  the GSI was added.
- `backfill-audit-source-ip.py` — set `source_ip` on rows captured
  before the IP-extraction fix (use cautiously; only for the operator's
  own demo-prep envs).
- `backfill-rerank-cost.py` — set `cost_usd` and bump matching DT/DM/DU
  rollups for historical Cohere rerank rows that were recorded at $0
  before pricing was wired.

Each script has a `--dry-run` default and an `--apply` flag.

---

## Troubleshooting

### "Refusals went away from the audit page"

Check whether the deploy includes commit `712bea0` or later — the
filtered-Query pagination fix. Without it, sparse filters
(`status=refusal` against the bedrock partition) return zero matches
when the most-recent N items are all chat successes. Symptoms: drill
mode shows the refusal, but org-wide Recent Activity doesn't.

### "Source IP shows as `—` for chat refusals"

The chat Lambda's `main.py:_build_event` and the Express proxy's
`userHeaders()` helper must both be deployed (commit `b790375`). The
Lambda needs to read `x-real-source-ip`/`x-real-user-agent` from
forwarded headers; the proxy needs to set those headers on every
signed Lambda call. If only one half is deployed, IPs default to the
Fargate task's egress IP.

### "Cost-by-model shows a phantom `test` entry"

The pre-`9377ceb` chat handler tagged test-refusal rows with
`model_id="test"`. After the fix, future test refusals are tagged
with `BASIC_MODEL_ID` (Haiku) so they blend with real data. To clean
up an existing `DM#*#test` rollup, delete it directly from the
analytics table:

```bash
aws dynamodb delete-item \
  --table-name "$ENV-outcome-ops-ai-assist-analytics" \
  --key '{"PK":{"S":"WS#<workspace_id>"},
          "SK":{"S":"DM#<date>#test"}}'
```

### "Cost-by-model shows retired model IDs"

If `claude-3-5-haiku-20241022` or `claude-sonnet-4-5-20250929`
appear, those are stale rollups from real historical chat before the
model upgrade. Delete the matching `DM#*#<old_model>` analytics
rows (or run an ad-hoc cleanup). The audit rows for those calls
remain — accurate history of what was used at the time. Audit.py
keeps the old model IDs in `MODEL_PRICING` as backward-compat aliases
so cost still calculates correctly for any straggler audit rows.

---

## See also

- [ADR-018: Audit Logging Design](adr/ADR-018-audit-logging-design.md)
  — schema, GSI2 rationale, writer split, per-call pricing.
- [`refusal-detection.md`](refusal-detection.md) — the four-part
  contract behind the red refusal rows on the dashboard.
- `lambda/shared/audit.py` / `lambda/shared/audit_writer.py` — writer
  modules.
- `lambda/workspace-management/handler.py` — admin emission +
  `/audit/*` query endpoints.
- `ui/src/pages/Audit.tsx` — the page itself.
- `scripts/backfill-demo-audit-data.py` — demo seeder.
