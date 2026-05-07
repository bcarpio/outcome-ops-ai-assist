# OutcomeOps AI Assist — Features

A running catalogue of what the platform does. One line per feature.
Skim this when onboarding, when a customer asks "do you support X",
when prepping a demo, or before scoping a new request.

**When you ship a new feature, add a line here in the same commit.**
Cross-link the lambda(s), ADR, or operator guide that explains it.
Keep entries terse — this is the index, not the spec.

## Core RAG and chat

- **S3 Vectors knowledge base** — vector store at
  `{env}-outcome-ops-ai-assist-vectors`, index `knowledge-base`,
  1024-dim cosine. Not OpenSearch, not Pinecone; native AWS.
- **Titan Text Embeddings v2** for ingestion (1024-dim).
- **Cohere Rerank 3.5** post-retrieval reranker, `top_k * 4`
  over-fetch capped at 50, drop below `rerank_min_score=0.3`.
- **Anthropic Sonnet + Haiku via Bedrock** — Sonnet for advanced
  reasoning, Haiku as the RAG default. tfvars-swappable per customer.
  See `lambda/shared/audit.py` for `MODEL_PRICING`.
- **Streaming chat UI** — `lambda/chat` over Lambda Web Adapter,
  NDJSON event stream, citation rendering, file attachments.
- **Multi-workspace search** — chat can span workspaces the user has
  access to (via `chat/searchable-workspaces`).
- **Conversation history** — DynamoDB-backed conversations, share
  with workspaces, list / delete via `lambda/chat`.
- **System prompt customization per workspace** — the "custom voice"
  feature; admin-set, audited via `SYSTEM_PROMPT_UPDATED`/`_RESET`.

## Source integrations

Each integration follows a 3-Lambda pattern: `*-integration` (HTTP /
webhook entry), `*-scheduler` (EventBridge cron driver), `*-sync`
(SQS-driven worker that ingests).

- **GitHub** — ingests ADRs, READMEs, `docs/lambda-*.md`, and code
  maps. GitHub App OAuth. On-demand webhook + hourly cron.
- **Confluence** — ingests pages from configured spaces. Atlassian
  OAuth. On-demand + cron.
- **Jira** — ingests issues; also drives the code-gen trigger via
  SSM Automation Document. Cross-account IAM role + external ID.
- **Outlook** — ingests email threads via Microsoft Graph. Azure AD
  OAuth, refresh tokens KMS-encrypted in DDB
  (`lambda/outlook-integration/token_manager.py`).
- **Teams** — channel messages via Microsoft Graph. Azure AD OAuth.
- **SharePoint** — site documents via Microsoft Graph. Azure AD OAuth.
- **Direct file upload** — drag-drop files into a workspace; goes
  through `file-ingestion` → `process-documents` → vector store.
  `delete-worker` cleans up S3 + vectors when files / workspaces
  are removed.

## Code understanding (code maps)

Per-repo structural extraction stored as workspace-scoped vectors.
Used by chat as authoritative context (per recent commit hardening),
and by code-gen as the "where does this thing live" index.

- **Java** — Maven / Gradle backend; classes, methods, imports.
- **Python** — modules, classes, functions, imports.
- **TypeScript** — including React components.
- **C# / .NET** — see ADR-014 (code-unit discovery), ADR-015 (backend
  naming), ADR-016 (test discovery), ADR-017 (regex/filesystem
  parser, no parser library).
- **ABAP / SAP** (abapGit) — code-map extraction supported (ADR-014
  also covers SAP file-suffix discovery). Code-*generation* does
  **not** target ABAP today.
- **Incremental regeneration** — code maps refresh on commit-SHA
  delta, not full repo every time.
- **Workspace-scoped repo allowlist** — code-map vectors are
  partitioned by repo, repos are joined to workspaces via membership.

## Code generation pipeline (Jira → Code → PR → Analyze)

End-to-end loop that turns a Jira ticket into a reviewed PR with
tests, ADR-aware, with auto-fix retry.

- **`generate-code`** — dispatches to language backend (`python-lambda`,
  `java-spring`, `typescript-express`, `react`); selected via
  `.outcomeops.yaml` `code_generation.backend`.
- **Trigger paths** —
  (a) Jira Automation rule → SSM Document → `generate-code`,
  (b) GitHub label `approved-for-generation` (full ~$1-5),
  (c) GitHub label `approved-for-plan` (plan-only ~$0.01-0.02).
- **Async loop** — webhook returns < 10s; SQS FIFO drives
  plan-then-step execution with 4-attempt retry + DLQ salvage PR
  if all retries fail.
- **Truncation recovery** — kicks in on SQS retry when the
  `max_tokens=32000` ceiling is hit, splits the work.
- **`analyze-pr`** — AI-powered PR review against ADRs.
- **`process-pr-check`** — feedback loop on CI check results;
  pricing env-var-driven so customers can plug in negotiated rates.
- **`run-tests`** — EventBridge-driven (`OutcomeOps.CodeGeneration.Completed`)
  → emits `OutcomeOps.Tests.Completed`. Auto-fix steps include
  `adr_fixer.py`, `readme_fixer.py`, `tests_fixer.py`.
- **`handle-command`** — dispatches CLI / chat slash commands.
- **EventBridge bus** `${env}-${app}-bus` decouples the loop instead
  of Step Functions — cheaper, simpler, faster cold starts.

## Workspaces and access control

- **Workspaces** — units of access control + isolation. Each has
  members, repos, MCP servers, custom system prompt, monthly budget,
  and a private vector partition.
- **Roles** — `org_admin`, `workspace_admin`, `member`. Org admins
  see / manage everything; workspace admins manage one workspace's
  members, repos, prompt, and MCP assignments.
- **Workspace sharing** — a workspace can grant search-only
  visibility to another (search-shareable but not editable).
- **Per-workspace monthly USD budget** — set by org admins; rolls
  into analytics. Cost-alert Lambda fires on first threshold cross.
- **Document upload + retention** — files attach to a workspace,
  feed the vector store, are deletable by workspace admins.

## MCP servers

Native support for the Model Context Protocol so tools can be added
without re-deploying the platform.

- **Built-in MCPs** (deployed inside customer's account):
  - `mcp-server` — KB query, list, standards retrieval.
  - `mcp-snyk` — vulnerability scanning of generated code.
  - `mcp-outcomeops` — SonarQube tooling + KB.
- **Customer-added MCPs** — via the `/org/mcp-servers` UI page.
  OAuth 2.0 client credentials and JWT bearer auth supported.
- **Workspace assignment** — admins attach specific MCP servers to
  specific workspaces; chat / generate-code see only assigned MCPs.

## Authentication and identity

- **UI**: Azure AD OIDC via ALB OIDC headers
  (`x-amzn-oidc-data` / `-accesstoken` / `-identity`). Express proxy
  decodes and forwards `x-real-source-ip` / `x-real-user-agent` to
  every signed Lambda call.
- **MCP / programmatic**: OAuth 2.0 JWTs (Azure AD) or platform-issued
  API keys (`/api-keys`).
- **Lambda-to-Lambda**: SigV4 against Function URLs
  (`signedChatFetch`, `signedWorkspaceFetch`).

## Audit dashboard

See `docs/audit-dashboard.md` and ADR-018 for full details.

- **Single audit table, three categories** (`bedrock`, `admin`, `auth`)
  partitioned by workspace (base), user (GSI1), and category (GSI2).
- **19 admin action constants** — workspace / member / repo / MCP /
  API key / budget / system-prompt CRUD, plus `PRIVACY_MODE_UPDATED`
  and `DOCUMENT_INGESTED` from the PII-redaction feature, all wired
  through `shared/audit_writer.log_admin_action`.
- **Refusal capture** — chat Lambda's `detect_refusal` flags ToS
  refusals into audit; `audit-alert` Lambda SNSes on each one.
- **InfoSec drill** — click a refusal → "View user's chat history"
  → narrows page to that user's bedrock rows in 1h / 3h / 24h.
- **CSV / JSON export** — paginate-to-completion download for
  ticket / SIEM escalation. 5,000-row safety cap.
- **Source IP + User Agent on every row** — captured browser-side via
  proxy header forwarding.

## Cost tracking and analytics

- **Six rollup shapes** — `analytics-aggregator` writes
  `DT#<date>` (daily total), `DM#<date>#<model>`,
  `DU#<date>#<user>` for bedrock; `DA#<date>`,
  `DC#<date>#<action>`, `DCU#<date>#<user>` for admin.
- **Three pricing shapes** — Claude / Titan price per million tokens,
  Cohere Rerank prices per invocation, Comprehend Detect-PII prices
  per character scanned. All three live in `MODEL_PRICING`; per-char
  rows carry `char_count` so the analytics aggregator rolls them into
  the same DT / DM / DU shape as token-priced rows.
- **Workspace / Analytics / Budgets pages** —
  `ui/src/pages/Analytics.tsx`, `WorkspaceAnalytics.tsx`,
  `AnalyticsBudgets.tsx`. Cost by model, by workspace, by user;
  daily trend; week-over-week deltas.
- **Cost alert Lambda** — `analytics-cost-alert`, daily
  EventBridge cron (`cron(0 13 * * ? *)`), publishes first-cross
  threshold to SNS so finance / org-admin see it on day-of, not
  end-of-month.
- **Operator-tunable pricing** — every model price (Sonnet input /
  output, Haiku input / output, Embedding, Rerank-per-1k,
  Comprehend-per-1M-chars) is a tfvar so customers on negotiated AWS
  pricing override without a code change.

## Security and compliance

- **PII redaction at ingest** — every document going into a workspace's
  vector store runs through Amazon Comprehend Detect-PII first. Names,
  emails, phones, addresses, SSNs, passport / driver-license numbers,
  bank routing / account / PIN, all four major credit-card brands' card
  numbers / CVVs / expiries, passwords, and AWS access / secret keys
  get replaced with `[REDACTED:TYPE]` placeholders before embedding —
  so the LLM physically cannot reveal the raw value, by design.
  Verified end-to-end via MCP query against the live S3 Vectors index;
  see `docs/pii-redaction-verification.md`.
- **Per-workspace privacy mode** — workspace admins choose `STRICT`
  (default, redact) or `NONE` (raw text in vectors, for HR / customer-
  support workspaces where redaction would defeat the point) via
  Settings → General → Privacy. Audited via `PRIVACY_MODE_UPDATED`.
- **Org-level redaction skip list** — `pii_redaction_skip_types` tfvar
  tunes what STRICT means for the whole deployment. Default exempts
  `URL / IP_ADDRESS / MAC_ADDRESS / DATE_TIME` (load-bearing in dev /
  infra docs). Override to `[]` to redact everything Comprehend
  detects.
- **Org-level kill switch** — `pii_redaction_enabled` tfvar disables
  Comprehend invocations entirely; useful in regions without
  Comprehend Detect-PII or when redaction is handled out-of-band. A
  plan-time `terraform apply` precondition fails fast if redaction is
  enabled in an unsupported region.
- **Per-document audit receipt** — every `DOCUMENT_INGESTED` audit row
  carries a `redaction_summary` (`{"EMAIL": 3, "PHONE": 1, ...}`),
  the `mode`, and a `skipped_reason` when bypassed. Drawer panel on
  the audit dashboard surfaces the per-type counts for InfoSec.
- **AI Disclosure Modal** — required first-time disclosure capture
  per `docs/ai-disclosure-modal.md`. Tracks who consented, when,
  what version of the disclosure they saw.
- **DPIA** (`docs/dpia.md`) — GDPR / UK-DPA data-flow assessment for
  enterprise InfoSec sign-off.
- **Audit-driven SOC 2 / FedRAMP narrative** — every admin action +
  every model invocation captured with timestamp, actor, IP. ADR-018.
- **Vulnerability scanning** — `mcp-snyk` integration scans
  generated code for known CVEs / supply-chain risks.
- **Code-quality gates** — SonarQube MCP integration scans new code
  for smells / security hotspots.
- **Repo secret scanning** — `security-scanner` Lambda flags
  hardcoded secrets / risks in source.
- **Refusal alerting** — `audit-alert` Lambda publishes ToS refusals
  to SNS, special-cases `flagged_reason=test_refusal_trigger` with
  a `[TEST]` SNS subject prefix so on-call distinguishes drills.
- **KMS encryption** — SSM (GitHub token, OAuth refresh tokens,
  webhook secrets) all KMS-encrypted; tables (audit, analytics,
  workspaces, chat) optionally use a customer-managed CMK.
- **AWS Security Agent integration** — service-account session-cookie
  OIDC bypass for automated security tooling.

## Demo and operator tooling

- **Test refusal trigger** — type `1234567890` in any chat; the
  handler short-circuits, returns a synthetic refusal tagged
  `flagged_reason=test_refusal_trigger`. Lets operators drill the
  refusal pipeline without risking an account flag from a real
  ToS-violating prompt.
- **Demo data seeder** —
  `scripts/backfill-demo-audit-data.py --env prd --apply` writes
  ~$280 / 30-day / 4,000-query / 15-refusal / 130-admin-action
  realistic enterprise workload across 20+ fake users with global
  names. Re-runnable on any cadence (auto-cleans prior demo data
  + analytics rollups in window).
- **Backfill scripts** — `scripts/backfill-audit-gsi2.py`,
  `backfill-audit-source-ip.py`, `backfill-rerank-cost.py`,
  `backfill-analytics.py` for in-place audit / analytics
  remediation.
- **CLI** — `scripts/outcome-ops-assist` for ad-hoc KB queries
  (when MCP isn't available); `docs/cli-usage.md`.

## Infrastructure and deployment

- **Per-customer AWS account** — the platform deploys into the
  customer's own account; no shared multi-tenant control plane.
- **Internal-only by default** — ALB is internal; customer attaches
  via VPC peering / TGW / Direct Connect / VPN. Optional public
  ALB toggle for trial / dev.
- **Lambda packaging** — most lambdas use the standard zip + LWA
  pattern; code-gen Lambdas use ECR-hosted runtime containers
  (`lambda/runtime-layer`, `lambda/terraform-layer`) because they
  need git / make / dotnet at runtime.
- **License usage telemetry** —
  `prs-generated`, `repos-active` reported to
  `https://license.example.com`; tracked at work-completed
  not PR-created (`docs/license-usage-tracking.md`).
- **Standard AWS managed services** — DynamoDB, S3, S3 Vectors,
  SQS, SNS, EventBridge, KMS, SSM, ALB, Cognito-free OIDC. No EC2,
  no Kubernetes, no ECS for the data plane (only the UI runs on
  Fargate).

## See also

- `docs/architecture.md` — high-level system diagram.
- `docs/getting-started.md` — first deploy walkthrough.
- `docs/deployment-guide.md` — full deployment runbook.
- `docs/audit-dashboard.md` — audit page operator guide.
- `docs/adr/` — architectural decisions, including ADR-018 (audit),
  ADR-014 to 017 (code maps).
- `docs/roadmap.md` — what's coming next.
- `docs/backlog.md` — known gaps and tracked debt.
