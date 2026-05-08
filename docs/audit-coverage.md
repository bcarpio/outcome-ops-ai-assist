# Audit Coverage — What the Platform Records

The complete reference for every event OutcomeOps AI Assist writes to
the audit table. Use this when an auditor asks *"what gets logged?"*,
when scoping a SOC 2 / HIPAA / GxP narrative, when wiring a SIEM, or
when deciding whether a new feature needs a new action constant.

For *how to use* the audit dashboard (drill flow, drawer, export, demo
seeder), see [`audit-dashboard.md`](audit-dashboard.md). For the
schema design rationale and GSI partitioning, see
[ADR-018](adr/ADR-018-audit-logging-design.md).

---

## TL;DR

- **One DynamoDB table** — `${env}-outcome-ops-ai-assist-audit-logs`
- **Three categories** — `bedrock` (model invocations), `admin`
  (operator actions), `auth` (identity events)
- **42 admin/auth action constants** declared today; 28 surface on the
  dashboard, 14 are queryable but suppressed from rollup cards by
  policy (high-volume noise)
- **Every row carries** timestamp, actor (`user_email`), source IP,
  user agent, request ID, workspace ID, category, and either an
  `action` (admin/auth) or model-invocation fields (bedrock)
- **Two writer modules** — `lambda/shared/audit.py` for bedrock rows
  (per-call pricing), `lambda/shared/audit_writer.py` for admin/auth
  rows
- **Tamper-evident by design** — DDB streams + per-customer KMS +
  least-privilege IAM (only the writers and dashboard-reader can see
  the table)

---

## Schema

Every audit row shares the same primary key and GSI shape:

| Field        | Notes                                                                |
|--------------|----------------------------------------------------------------------|
| `PK`         | `WS#<workspace_id>` — workspace partition for org-admin queries      |
| `SK`         | `<iso_timestamp>#<request_id>` — sortable + uniquely identifying     |
| `GSI1PK`     | `USER#<user_email>` — for "show me everything user X did"            |
| `GSI1SK`     | `<iso_timestamp>`                                                    |
| `GSI2PK`     | `CAT#bedrock` / `CAT#admin` / `CAT#auth` — category-scoped queries   |
| `GSI2SK`     | `<iso_timestamp>`                                                    |
| `category`   | `bedrock` / `admin` / `auth`                                         |
| `timestamp`  | ISO 8601 UTC                                                         |
| `request_id` | Lambda request ID (or composite for multi-step events)               |
| `user_email` | The actor; `system@outcomeops` for cron-driven events                |
| `source_ip`  | Captured browser-side, forwarded via Express proxy `x-real-source-ip`|
| `user_agent` | `x-real-user-agent`                                                  |
| `lambda_name`| Emitting Lambda                                                      |

Category-specific fields are layered on top:

- **bedrock** — `model_id`, `input_tokens`, `output_tokens`,
  `cost_usd`, `status` (`success` / `refusal` / `error`),
  `flagged_reason`, `input_text`, `output_text`, `char_count`
  (Comprehend per-char rows).
- **admin** / **auth** — `action` (one of the constants below),
  `details` (JSON object with action-specific context — e.g.,
  `{"workspace_id": "...", "old_value": "...", "new_value": "..."}`).

---

## Category 1 — `bedrock` (model invocations)

Every Bedrock call from any Lambda lands here. Written by
`lambda/shared/audit.py` (see `MODEL_PRICING` for the three pricing
shapes: per-token, per-call, per-character).

| What it captures                                  | When it fires                                          |
|---------------------------------------------------|--------------------------------------------------------|
| Sonnet / Haiku chat invocations                   | Every `lambda/chat` request that reaches Bedrock       |
| RAG retrieval rerank                              | Every Cohere Rerank invocation                         |
| Document embedding (Titan v2)                     | Every `process-documents` ingest call                  |
| Comprehend Detect-PII calls                       | Every PII-redaction pass at ingest                     |
| Code-gen plan + step model calls                  | Every `generate-code` plan / step LLM round-trip       |
| `analyze-pr` PR-review model calls                | Every PR analysis                                      |
| `process-pr-check` feedback-loop model calls      | Every CI failure → fix iteration                       |
| Tests-fixer / ADR-fixer / README-fixer model calls| Every auto-fix iteration                               |
| Refusals (status=`refusal`)                       | When `detect_refusal` flags ToS-blocked output         |
| Test refusals (`flagged_reason=test_refusal_trigger`) | When `1234567890` literal trigger fires            |

Refusal rows carry the user's prompt + the model's response (truncated
2 KB server-side) so the InfoSec drill can show what was blocked
and why.

---

## Category 2 — `auth` (identity events)

Identity-only events that don't fit the admin-action shape.

| Action               | Source                                            | Visible? |
|----------------------|---------------------------------------------------|----------|
| `LOGIN_SUCCEEDED`    | First `workspace-management` request per user-day | Hidden   |
| `API_KEY_USED`       | Every API-key-authenticated workspace + MCP call  | Hidden   |
| `MCP_JWT_VALIDATED`  | (Forward-looking — not emitted today; see below)  | —        |

`LOGIN_SUCCEEDED` deduplicates per user-per-day via an in-process LRU
cache so the row fires once on the user's first request of the day,
not on every page load.

`API_KEY_USED` and `MCP_JWT_VALIDATED` are hidden from dashboard
rollups (would dominate the cards at MCP polling volume) but remain
fully queryable via `?action=API_KEY_USED&user_email=...`.

---

## Category 3 — `admin` (operator actions)

Everything an operator does that changes platform state. Written by
`lambda/shared/audit_writer.py`. Organized below by domain.

### Workspace lifecycle

| Action                   | Endpoint                              |
|--------------------------|---------------------------------------|
| `WORKSPACE_CREATED`      | `POST /workspaces`                    |
| `WORKSPACE_UPDATED`      | `PATCH /workspaces/{id}`              |
| `WORKSPACE_DELETED`      | `DELETE /workspaces/{id}`             |
| `WORKSPACE_BUDGET_UPDATED` | `PUT /workspaces/{id}/budget`       |
| `WORKSPACE_SHARING_UPDATED` | `PUT /workspaces/{id}/searchable-by` |

### Member management

| Action                     | Endpoint                                      |
|----------------------------|-----------------------------------------------|
| `WORKSPACE_MEMBER_ADDED`   | `POST /workspaces/{id}/members`               |
| `WORKSPACE_MEMBER_REMOVED` | `DELETE /workspaces/{id}/members/{email}`     |
| `ORG_MEMBER_ADDED`         | `POST /org/members`                           |
| `ORG_MEMBER_REMOVED`       | `DELETE /org/members/{email}`                 |

### Repo management

| Action                    | Endpoint                              |
|---------------------------|---------------------------------------|
| `WORKSPACE_REPO_ADDED`    | `POST /workspaces/{id}/repos`         |
| `WORKSPACE_REPO_REMOVED`  | `DELETE /workspaces/{id}/repos/{name}`|

### MCP server management

| Action                          | Endpoint                                |
|---------------------------------|-----------------------------------------|
| `MCP_SERVER_CREATED`            | `POST /mcp/servers`                     |
| `MCP_SERVER_UPDATED`            | `PATCH /mcp/servers/{id}`               |
| `MCP_SERVER_DELETED`            | `DELETE /mcp/servers/{id}`              |
| `MCP_SERVER_ASSIGNED_TO_WORKSPACE` | `POST /workspaces/{id}/mcp-servers`  |
| `MCP_SERVER_UNASSIGNED_FROM_WORKSPACE` | `DELETE /workspaces/{id}/mcp-servers/{server_id}` |
| `MCP_TOOL_INVOKED` *(hidden)*   | Every tool call from the MCP server     |

### API keys

| Action            | Endpoint                          |
|-------------------|-----------------------------------|
| `API_KEY_CREATED` | `POST /api-keys`                  |
| `API_KEY_REVOKED` | `DELETE /api-keys/{key_id}`       |

### System prompts (custom voice)

| Action                  | Endpoint                                  |
|-------------------------|-------------------------------------------|
| `SYSTEM_PROMPT_UPDATED` | `PUT /workspaces/{id}/system-prompt`      |
| `SYSTEM_PROMPT_RESET`   | `DELETE /workspaces/{id}/system-prompt`   |

### Privacy / security

| Action                   | Source                                       |
|--------------------------|----------------------------------------------|
| `PRIVACY_MODE_UPDATED`   | `PUT /workspaces/{id}/privacy-mode`          |
| `DOCUMENT_INGESTED` *(hidden)* | Every document the PII-redaction pipeline processes; row carries `redaction_summary` (`{"EMAIL": 3, ...}`), `mode`, `skipped_reason` |

### Document + conversation lifecycle

| Action                    | Source                                     |
|---------------------------|--------------------------------------------|
| `DOCUMENT_DELETED` *(hidden)* | `DELETE /workspaces/{id}/documents/{key}` |
| `CONVERSATION_DELETED` *(hidden)* | `DELETE /chat/conversations/{id}`     |

### Source-sync triggers (hourly cron)

All hidden from dashboard rollups (hourly drumbeat × N connections
would drown out everything else) but queryable for compliance pulls
like *"every external read in Q3."*

| Action               | Source Lambda                |
|----------------------|------------------------------|
| `REPO_SYNCED`        | `github-sync/handler.py`     |
| `CONFLUENCE_SYNCED`  | `confluence-sync/handler.py` |
| `JIRA_SYNCED`        | `jira-sync/handler.py`       |
| `OUTLOOK_SYNCED`     | `outlook-sync/handler.py`    |
| `TEAMS_SYNCED`       | `teams-sync/handler.py`      |
| `SHAREPOINT_SYNCED`  | `sharepoint-sync/handler.py` |

### OAuth lifecycle

| Action                          | Source                                    |
|---------------------------------|-------------------------------------------|
| `OAUTH_CONNECTION_ESTABLISHED`  | All five `*-integration` callback handlers (GitHub, Confluence, Outlook, Teams, SharePoint) |

### Code-generation pipeline events

| Action                          | Source                                    |
|---------------------------------|-------------------------------------------|
| `PR_ANALYZED` *(hidden)*        | `analyze-pr/handler.py` — every AI-reviewed PR |
| `CODE_GENERATED`                | `generate-code/step_executor.py` — fired alongside the EventBridge completion event |
| `CODE_GEN_DLQ_SALVAGE_PR_CREATED` | `generate-code/dlq_handler.py` — when 4-attempt retry exhausts and DLQ salvage opens a partial-progress PR |
| `TESTS_RAN` *(hidden)*          | `run-tests/handler.py` finally block      |

### Cost / budget

| Action                      | Source                                        |
|-----------------------------|-----------------------------------------------|
| `BUDGET_THRESHOLD_CROSSED`  | `analytics-cost-alert` daily cron — fires the first time the workspace + org budget thresholds cross |

---

## Hidden vs visible

The `HIDDEN_DASHBOARD_ACTIONS` set in `lambda/shared/audit_writer.py`
(mirrored in `lambda/analytics-aggregator/handler.py`) lists actions
that write the audit row but skip the rollup cards.

**Why hide:** these actions are either high-volume drumbeats
(sync crons, MCP tool calls) or per-record events (per-document, per-PR)
that would dominate the dashboard the same way `DOCUMENT_INGESTED` did
before we added the hide rule. The audit row is still written for
forensic / right-to-deletion / compliance-query purposes — operators
just have to filter `?action=...` explicitly to see them.

Currently hidden (14):

```
DOCUMENT_INGESTED, API_KEY_USED, MCP_TOOL_INVOKED,
LOGIN_SUCCEEDED, REPO_SYNCED, CONFLUENCE_SYNCED,
JIRA_SYNCED, OUTLOOK_SYNCED, TEAMS_SYNCED, SHAREPOINT_SYNCED,
DOCUMENT_DELETED, CONVERSATION_DELETED, PR_ANALYZED, TESTS_RAN
```

---

## Deliberately not emitted

Action constants declared but with no current emit site. Documented
here so a future contributor doesn't think they're missing.

| Constant                    | Status                                                   |
|-----------------------------|----------------------------------------------------------|
| `DOCUMENT_DOWNLOADED`       | No download endpoint exists today. Constant declared forward-looking; emit when / if one is added. |
| `OAUTH_TOKEN_REFRESHED`     | Token refresh fires hourly per connection — would dominate the dashboard. Deferred. If re-enabled, also add to `HIDDEN_DASHBOARD_ACTIONS`. |
| `MCP_JWT_VALIDATED`         | MCP server today uses API keys (covered by `API_KEY_USED`). Constant ready for when Azure AD JWT auth is added. |
| `LOGIN_FAILED`              | ALB OIDC rejects unauthenticated requests *before* they reach our code — structurally invisible. ALB access logs (S3) are the alternate source for failed-login analysis. |

---

## What auditors typically ask for

Every one of these is a single DDB Query against the audit table —
no scans, no cross-table joins.

| Question                                              | Query                                                           |
|-------------------------------------------------------|-----------------------------------------------------------------|
| "Every action user X took in date range"              | GSI1 `USER#X` + `timestamp` range                               |
| "Every external pull in Q3"                           | GSI2 `CAT#admin` + filter `action IN (REPO_SYNCED, CONFLUENCE_SYNCED, ...)` |
| "Every login event by user X"                         | GSI1 `USER#X` + `category=auth`                                 |
| "Every refusal in date range"                         | GSI2 `CAT#bedrock` + filter `status=refusal`                    |
| "Every cross-workspace data-share grant"              | GSI2 `CAT#admin` + filter `action=WORKSPACE_SHARING_UPDATED`    |
| "Every PR our AI reviewed"                            | GSI2 `CAT#admin` + filter `action=PR_ANALYZED`                  |
| "Every budget threshold cross by workspace"           | GSI2 `CAT#admin` + filter `action=BUDGET_THRESHOLD_CROSSED`     |
| "Every document touching workspace W"                 | base table `WS#W` + filter `action IN (DOCUMENT_INGESTED, DOCUMENT_DELETED)` |
| "Every privacy-mode change in the org"                | GSI2 `CAT#admin` + filter `action=PRIVACY_MODE_UPDATED`         |
| "Every system-prompt change for compliance review"    | GSI2 `CAT#admin` + filter `action IN (SYSTEM_PROMPT_UPDATED, SYSTEM_PROMPT_RESET)` |

---

## Export for SIEM / GRC tooling

Two paths, depending on whether the customer wants point-in-time
queries or a continuous feed.

**Continuous feed (Kinesis + OCSF) — preferred for SIEM.** Set
`audit_stream_export_enabled = true` to provision a Kinesis Data
Stream that receives every audit-log INSERT as an OCSF v1.3.0
envelope. Customer attaches their own Splunk / Datadog / Sumo / AWS
Security Lake / Firehose-to-S3 consumer and applies FilterCriteria to
pick which events to ingest. Full schema, FilterCriteria patterns, and
consumer IAM in [`audit-stream-export.md`](audit-stream-export.md).

**Point-in-time pull (REST endpoint).** The `/audit/events` endpoint
accepts `start_date`, `end_date`, `workspace_id`, `user_email`,
`action`, `category`, `status`, `cursor`, `limit`. Pagination follows
the standard cursor pattern. The Audit dashboard offers CSV / JSON
download (5,000-row safety cap) when drilled into a single user. For
larger pulls — into a GRC tool (Drata, Vanta, Hyperproof) or for an
ad-hoc compliance pull — drive the endpoint directly with a service-
account API key + cursor pagination.

---

## See also

- [`audit-dashboard.md`](audit-dashboard.md) — operator's guide to the
  `/audit` page (drill flow, drawer, export, demo seeder).
- [`audit-stream-export.md`](audit-stream-export.md) — SIEM export
  over Kinesis (OCSF schema + FilterCriteria patterns + consumer IAM).
- [`refusal-detection.md`](refusal-detection.md) — four-part refusal-
  classification contract that makes the `status=refusal` count
  meaningful at scale.
- [ADR-018](adr/ADR-018-audit-logging-design.md) — schema design,
  GSI2 rationale, writer split, per-call pricing.
- `lambda/shared/audit.py` — bedrock-row writer (per-call pricing).
- `lambda/shared/audit_writer.py` — admin/auth-row writer + the
  `ACTION_*` constants + `HIDDEN_DASHBOARD_ACTIONS` set.
- `lambda/analytics-aggregator/handler.py` — DDB-streams consumer that
  rolls audit rows into the dashboard cards.
