# OutcomeOps AI Assist — Features

A running catalogue of what the platform does. One line per feature.
Skim this when onboarding, when a customer asks "do you support X",
when prepping a demo, or before scoping a new request.

**What the platform is, in one line:** a grounded **PR review +
organizational knowledge base** that runs entirely in the customer's
own AWS account — every review is judged against *their* ADRs and
standards, with an audit trail. Code generation is an **optional**
capability on top of the same knowledge base, not the headline: most
customers adopt review + KB first, and many prefer to run codegen
through their own coding agent (the MCP server exposes the governed
KB — `query_knowledge_base`, `read_source`, symbol graph — to any
agent they already use). Order this doc accordingly: review + KB lead.

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
- **Anthropic Claude via Bedrock — model-per-tier is a tfvar, not
  hard-coded.** `bedrock_advanced_model_id` / `bedrock_basic_model_id`
  pick the advanced and basic tiers. prd ships Sonnet 5.5 (advanced) +
  Haiku 5.5 (basic), but any Claude model the customer has Bedrock
  access to drops into either tier by changing the tfvar — e.g. Opus 5.5
  or Fable as the advanced tier — no code change. Per-tier prices
  (`bedrock_advanced_input_price`/`_output_price`, `bedrock_basic_*`) are
  tfvars too. Embedding-model swap is architectural (vector dimensions),
  not a tfvar flip. See `lambda/shared/audit.py` for `MODEL_PRICING`.
- **OpenAI GPT-5.5 / 5.4 / Codex via Bedrock Mantle** — same
  `shared.model_client.invoke()` entry point routes anthropic prefixes
  to the Converse API and openai prefixes to the Mantle Responses API
  (bearer-token-authed, long-lived key from SSM). `_DISPATCH` in
  `lambda/shared/model_client.py` is the routing table; unknown
  prefixes raise loudly so silent fallback never invalidates
  attribution claims. ADR-035.
- **Line-citation grounding** — `find_symbol_at_line` (MCP + shared)
  resolves a `file.py:544`-style reference to the enclosing symbol
  before answering, so citations point at the actually-relevant
  definition rather than the syntactically-closest one. ADR-031.
- **List-completeness contract** — chat NEVER fabricates aggregate
  totals ("N lambdas total") for list-shaped answers. Either every
  member is enumerated from retrieved chunks or the count is not
  claimed. Model-authored-aggregates are banned by prompt discipline
  + response validation. ADR-032.
- **Dual-channel retrieval + per-lane rerank** — code-map channel and
  documentation channel retrieve independently, rerank per lane, then
  merge for synthesis. Prevents doc-drift from dominating grounded
  code answers. Closes the "the docs say X but the code does Y" moat
  violation. ADR-033.
- **read_source adjudicator** — platform-agnostic tool exposing
  `read_this_file_at_this_SHA` to the model. Any citation the model
  wants to make can be verified against source-of-truth git state
  (GitHub + Azure DevOps + GitLab supported). ADR-030 (v1.1 adds ADO
  dispatch; GitLab via ADR-050).
- **Streaming chat UI** — `lambda/chat` over Lambda Web Adapter,
  NDJSON event stream, citation rendering, file attachments.
- **Multi-workspace search** — chat can span workspaces the user has
  access to (via `chat/searchable-workspaces`).
- **Conversation history** — DynamoDB-backed conversations, share
  with workspaces, list / delete via `lambda/chat`.
- **System prompt customization per workspace** — the "custom voice"
  feature; admin-set, audited via `SYSTEM_PROMPT_UPDATED`/`_RESET`.

## PR review and governance

Grounded, auditable PR/MR review judged against the customer's own
ingested standards — the most-adopted capability. Runs in the
customer's account; no diff leaves their boundary.

- **`analyze-pr` + `process-pr-check`** — the orchestrator fetches the
  diff + changed files, selects applicable checks by changed-file
  glob, posts an initial status comment, and fans check jobs to an SQS
  FIFO queue ordered per PR (`MessageGroupId =
  pr-{owner}-{repo}-{pr_number}`); the worker runs the analysis, posts
  result comments, and persists them (`PK=PR#<n>`, `SK=CHECK#<type>`).
- **Six grounded checks** (verdicts PASS / WARN / FAIL): ADR
  compliance, breaking changes, architectural duplication, README
  freshness, test coverage, license/copyleft (ADR-008). ADR
  compliance, duplication, and license checks query the KB for the
  relevant standards and judge the diff against the *retrieved ADRs*,
  not hardcoded rules (ADR-007).
- **Symbol-graph structural impact** — `diff_symbol_resolver` +
  `structural_review.build_impact_block` resolve mutated symbols and
  query the symbol-graph reader for callers/importers **as of the PR
  base SHA**, fold the impact into the reviewer prompt, and post a
  `structural_impact` comment. Honest empty-sets: a symbol with zero
  callers renders "no known callers" rather than being omitted or
  fabricated — audit-grade. Fail-soft + disclosed on an
  unavailable/stale graph, never silently wrong.
- **Cross-SCM parity** — GitHub PRs, Azure DevOps PRs, and GitLab MRs
  (ADR-050 v1.2) get the same checks and the same collapsed
  `<details>` comments **upserted in place per push** (one per check,
  PATCHed/PUT, never duplicated). `shared/pr_metadata.py` maps ADO
  `changeType` → GitHub status vocab so BREAKING_CHANGES behaves
  identically across platforms.
- **Enterprise scale** — symbol lookups use the `target-name-index`
  GSI (scales with matches, not workspace size); global workspaces
  (`is_global`) inject org-wide standards into every check.
- Triggered by a customer-installed CI workflow (GitHub Action via
  OIDC; ADO/GitLab equivalents) on PR/MR open + synchronize;
  AI-generated PRs are analyzed automatically.

## Source integrations

Each integration follows a 3-Lambda pattern: `*-integration` (HTTP /
webhook entry), `*-scheduler` (EventBridge cron driver), `*-sync`
(SQS-driven worker that ingests).

- **GitHub** — ingests ADRs, READMEs, `docs/lambda-*.md`, and code
  maps. GitHub App OAuth. On-demand webhook + hourly cron. GHES
  (Enterprise Server) base URL configurable via `github_base_url`
  (API `{base}/api/v3`, raw via contents API; App registered on the
  instance) — not yet certified against a live GHES.
- **Confluence** — ingests pages from configured spaces. Atlassian
  OAuth. On-demand + cron.
- **Jira** — ingests issues; also drives the code-gen trigger via
  SSM Automation Document. Cross-account IAM role + external ID.
- **Outlook** — ingests email threads via Microsoft Graph. Azure AD
  OAuth, refresh tokens KMS-encrypted in DDB
  (`lambda/outlook-integration/token_manager.py`).
- **Teams** — channel messages via Microsoft Graph. Azure AD OAuth.
- **SharePoint** — site documents via Microsoft Graph. Azure AD OAuth.
- **OneDrive** — user drive files via Microsoft Graph. Azure AD OAuth.
- **OneNote** — notebook pages via Microsoft Graph. Azure AD OAuth.
- **Azure DevOps (Repos)** — source code + ADRs + docs. Entra ID
  OAuth via a dedicated app registration (separate from M365 app).
  Powers ADR-030 v1.1's platform-agnostic `read_source` dispatch.
- **Azure DevOps Boards** — work items alongside GitHub Issues / Jira.
  Shares the ADO OAuth config with Repos.
- **GitLab** — source code + ADRs + docs as a third code host
  (gitlab.com; self-managed via `gitlab_base_url`). ONE group-owned
  OAuth app per deploy; per-user delegated tokens (`api` scope —
  narrowest GitLab scope that can write an MR note), so users enroll
  only projects they can already access. Nested subgroups (variable-
  depth slugs) supported everywhere via DDB `GITLAB_REPO#` lookup,
  never slash-count. Cached single-flight token refresh (GitLab
  rotates refresh tokens per use). Multiple GitLab instances per deploy
  (gitlab.com + self-managed together) via the `gitlab_instances` tfvar
  list -- connections are instance-scoped; same-bare-name enrollment
  across instances into one workspace is refused (key-collision guard).
  `enable_gitlab_integration` + `gitlab_client_id` (or `gitlab_instances`)
  tfvars. ADR-050 (v2.0 multi-instance).
- **Salesforce** — Account / Contact / Opportunity records +
  embedded recent Task / Event activity + attached files (Notes and
  Attachments via ContentVersion). Shared Connected App OAuth per
  deploy (env-SSM pattern like Confluence / Jira / M365); sandbox
  vs prod routing selectable at connect time. ADR-044.
- **ServiceNow** — KB articles + article attachments. Env-SSM
  shared OAuth (owner-registered app; customers reuse via callback
  URL registration).
- **Database** — introspect Postgres, MySQL, MSSQL schemas as
  workspace context. Schemas-only scope, auto-discover tables, SNS
  drift notifications on schema change. SSM-backed BYO CA bundles
  for internal CAs. `docs/database-integration.md`.
- **Direct file upload** — drag-drop files into a workspace; goes
  through `file-ingestion` → `process-documents` → vector store.
  `delete-worker` cleans up S3 + vectors when files / workspaces
  are removed.
- **Force-Textract on `*_attachment` sources** — every user-uploaded
  binary (Salesforce / ServiceNow / Confluence / Jira attachment)
  gets Textract OCR regardless of the PyPDF2-density heuristic, so
  proposal PDFs, contracts, SOWs, and image-heavy documents don't
  lose their tables / signatures / diagrams to naive text extraction.
  Native code-repo PDFs keep the fast path.

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

### Structural retrieval (symbol graph)

A parallel structural-retrieval index that augments RAG with
deterministic caller / importer / implementer answers. Extracted
during existing code-map regen runs, stored in DynamoDB
(`${env}-${app}-symbol-graph`), queried in parallel with vector
RAG by `chat`, `analyze-pr`, and `generate-code`. Honestly returns
empty sets — "no callers found in this workspace" — instead of
fabricating a list, which is the audit unlock for the regulated
buyer. See `docs/symbol-graph-validation.md` for the operator
validation + migration runbook, `docs/adr/ADR-020-abap-parser-strategy.md`
for the ABAP hybrid Tree-sitter + regex decision, and
`docs/adr/ADR-021-gsi-naming-symbol-graph.md` for the GSI naming
convention.

- **`callers(X)`** — every direct caller of a symbol, with file path
  and line.
- **`importers(X)`** — every importer of a package or subpath
  (e.g. `@acme-corp/shared-utils/http2`); exact-match and prefix /
  wildcard modes.
- **`implementers(I)`** — every class implementing an interface.
- **`definitions(X)`** — where a symbol is defined.

Operator kill-switch: `GRAPH_RETRIEVAL_ENABLED` (default `"true"`)
gates the graph path per Lambda; one-touch rollback without redeploy.

#### Per-repo extraction config (`.outcomeops.yaml`)

Repositories opt in / out and tune extraction via the
`ingestion.symbol_graph` block of the repo's `.outcomeops.yaml`. All
keys are optional; the defaults match the platform-wide behavior so
repos that don't ship a yaml get the same coverage as today.

```yaml
ingestion:
  symbol_graph:
    # Set to false to disable extraction entirely for this repo.
    # The handler logs a single "symbol-graph disabled by config"
    # line per batch so operators can see why a repo has no facts.
    enabled: true

    # Glob patterns to include BEYOND the ADR-009 default set.
    # Empty list = use only the default (lambda/**, src/**, etc.).
    # Useful for pulling a vendored or generated tree back into
    # scope despite the default exclude list.
    include:
      - "generated/sdk/**"

    # Glob patterns to exclude beyond the default ADR-009 list.
    # Exclude wins over include.
    exclude:
      - "lambda/tests/**"
      - "**/legacy-vendor-pinned/**"

    # Skip files larger than this many bytes before parse.
    # Defaults to 512 KB — matches the Tree-sitter parse-cost knee
    # in our benchmarks. Set to 0 to disable the cap.
    max_file_bytes: 524288

    # Allow-list of languages to extract. Omit / null means every
    # registered extractor runs. Use to silence an extractor for
    # a polyglot repo (e.g., disable c# extraction on a JS-heavy
    # mono).
    languages:
      - python
      - typescript
      - java
      - go
      - csharp
      - abap
```

The config is Pydantic-validated at extraction time
(`lambda/shared/repo_config.py:SymbolGraphConfig`). Invalid values
fail loudly with a `ValidationError` log line; the extractor
gracefully degrades to the default permissive filter when the yaml
is missing or unparseable.

Operators verify the filter from CloudWatch logs on the
`process-batch-summary` Lambda: each rejected file logs the reason
at debug level; a disabled repo emits a single info-level
"symbol-graph disabled by config" line per batch.

#### Bare-name fuzzy resolution

Users in chat type bare names ("which lambdas call `require_auth`?")
while the graph stores canonical IDs
(`python:lambda.auth.handler::require_auth`). A third GSI on the
symbol-graph table — `target-name-index`, partitioned by
`(workspace_id, bare_name)` — backs a single-partition Query
that maps bare names to the set of matching canonicals. Scales
with the number of matches per bare name, not with workspace
size, so the resolver works at the regulated-customer scale of
50–100 repos per workspace where a Scan-based approach silently
truncates. ADR-021 governs the GSI naming.

When a bare name resolves to multiple canonicals (every
integration Lambda defining its own local `require_auth`,
cross-module copies, etc.) the chat handler runs `callers()`
against each canonical and merges the results, so the answer to
"who calls X" is a complete cross-codebase inventory partitioned
by which local copy each call site targets — not a "X is defined
in N places" disambiguation prompt the model can't answer with.

#### Routing transparency in chat UI

Every chat response renders a routing chip above the sources panel
that surfaces what the symbol-graph router decided and which
retrievals fired. Eliminates the "did this answer come from the
graph or just RAG?" question regulated buyers ask during audits.

The chip shows:
- **Intent**: one of `structural` / `hybrid` / `pattern` /
  `dependency` / `unknown` — the symbol-graph router's
  classification of the question shape.
- **Retrievals fired**: `RAG: N chunks` always; `Graph: <relation>
  (N results)` when the graph was queried; `Graph: skipped` when
  the router chose RAG-only (pattern / dependency questions).
  `<relation>` is `callers` / `importers` / `implementers` /
  `definitions` depending on which symbol-graph query ran.
- **Symbol candidate**: the bare name the router extracted from
  the question, when one was found.
- **Graph age**: the worst-case staleness across queried graph
  partitions when the answer would be sensitive to it (e.g.,
  "graph age: 23m").

Graph results land in the chat metadata frame as
`graph_sources[]`, distinct from the RAG `sources[]`, so the
sources panel renders them with a different visual treatment
(emerald tint vs. neutral). The user can tell at a glance which
file/line citations are graph-grounded (deterministic — the
symbol-graph table says X calls Y) vs vector-grounded
(probabilistic — the chunk that matched the question contained
text about Y).

## Code generation (optional)

An optional capability layered on the same knowledge base — not the
primary draw. Turns a Jira ticket (or a labelled issue) into a
plan+code+tests+PR, ADR-grounded, with auto-fix retry. Customers can
leave it off entirely and use review + KB alone, or point their own
coding agent at the governed KB via the MCP server instead of using
this pipeline. Backends ship for Python, Java, TypeScript, and React
(C# is code-map extraction only, not generation).

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
- **PR review** on the generated PR runs automatically — see the
  "PR review and governance" section above (`analyze-pr` /
  `process-pr-check`, the six grounded checks, symbol-graph impact).
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
- **Global workspaces** — org-admin-set bidirectional scope flag
  (`is_global=true`). Content in a global workspace shows up in
  every user's chat retrieval + every PR check's standards
  retrieval, AND global-workspace members get implicit read access
  on every other workspace when querying from the global one. The
  same flag applies to MCP servers, mirroring the workspace pattern.
  Rendered in the workspace-create / MCP-create UI for org admins
  only.
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

See `docs/audit-coverage.md` for the full list of every event the
platform records, `docs/audit-dashboard.md` for the operator's guide
to the `/audit` page, and ADR-018 for schema design.

- **Single audit table, three categories** (`bedrock`, `admin`, `auth`)
  partitioned by workspace (base), user (GSI1), and category (GSI2).
- **42 admin / auth action constants** spanning workspace lifecycle,
  member / repo / MCP management, API keys, system prompts, privacy
  mode, document + conversation lifecycle, source-sync triggers,
  OAuth lifecycle, code-gen pipeline events, and budget thresholds.
  28 surface on the dashboard; 14 are queryable but hidden from
  rollup cards by policy (high-volume drumbeats like sync crons /
  per-document ingest / per-PR review).
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
- **Operator-tunable pricing** — every model price (advanced-tier input
  / output, basic-tier input / output, Embedding, Rerank-per-1k,
  Comprehend-per-1M-chars) is a tfvar so customers on negotiated AWS
  pricing override without a code change.
- **Bedrock application inference profiles for per-workload cost
  attribution** — chat / code-maps / code-gen / pr-analyzer each
  wrap their models (Haiku / Sonnet / Titan-embed / Cohere-rerank)
  in application inference profiles tagged with a customer-
  configurable tag key (`bedrock_workload_tag_name`, defaults to
  `Purpose`). Every Bedrock call routes through the workload profile
  via `shared.model_client.invoke(workload=..., tier=...)`, so
  Cost Explorer group-by the workload tag with `Service=Amazon
  Bedrock` splits spend per workflow. Answers "which internal
  workflow is driving our Bedrock cost?" natively rather than by
  guess. ADR-016 v1.2 / v1.3 / v1.4 in outcomeops-adrs;
  see `terraform/bedrock-inference-profiles.tf`.

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
- **SIEM export via Kinesis (OCSF v1.3.0)** — opt-in tfvar
  (`audit_stream_export_enabled`) provisions a Kinesis Data Stream and
  publisher Lambda that re-emits every audit-log INSERT as an OCSF
  hybrid envelope (standard fields populated for Security Lake / Splunk
  OCSF apps, full raw payload preserved under `unmapped`). Customer
  security teams attach their own Splunk / Datadog / Sumo / Security
  Lake / Firehose consumer and filter via standard event-source-mapping
  FilterCriteria. CMK flows from `enable_cmk_encryption`. See
  `docs/audit-stream-export.md`.
- **Vulnerability scanning** — `mcp-snyk` integration scans
  generated code for known CVEs / supply-chain risks.
- **Code-quality gates** — SonarQube MCP integration scans new code
  for smells / security hotspots.
- **Repo secret scanning** — `security-scanner` Lambda flags
  hardcoded secrets / risks in source.
- **Refusal alerting** — `audit-alert` Lambda publishes ToS refusals
  to SNS, special-cases `flagged_reason=test_refusal_trigger` with
  a `[TEST]` SNS subject prefix so on-call distinguishes drills.
  Refusal classification itself is a four-part contract (system-prompt
  guidance + counter-balance + regex pattern + length cap) so the
  `status=refusal` count stays meaningful at scale; see
  `docs/refusal-detection.md`.
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
- **Fargate Spot master switch** — `fargate_enable_spot` tfvar
  gates FARGATE_SPOT across every ECS service (UI proxy + MCP
  servers, both regions). Default FALSE: 100% on-demand out of the
  box so interactive chat/MCP tasks never spot-reclaim (safe-default
  philosophy, like `ui_alb_internal`). Sandbox deploys opt in with
  true; per-service `*_spot_weight` vars tune the ratio.
- **Per-service Fargate task sizing** — every ECS service's cpu/memory
  is an independent tfvar: `ui_fargate_cpu`/`_memory`, `mcp_server_cpu`/
  `_memory` (OutcomeOps MCP), `mcp_snyk_cpu`/`_memory`,
  `mcp_sonarqube_cpu`/`_memory`. Cost-sensitive deploys right-size down
  (256 = 0.25 vCPU); heavy enterprise load scales up. No hardcoded task
  dimensions remain.
- **HA active/passive multi-region** — `secondary_region` tfvar
  provisions every stateful resource in both regions with cross-
  region replication driven by EventBridge; per-region AppConfig
  flag chooses which is active. Operators flip via the AppConfig
  console during failover (no tfvars edit, no terraform apply).
  Deliberate rejection of DDB global tables after the Oct 2025
  us-east-1 outage. ADR-022. `docs/HA.md`.
- **Code-map batch splitter + merge fan-in** — process-repo-summary
  splits oversized code-map units into shards, dispatches each to
  process-batch-summary, and merges the results back. Closed the
  15-minute Lambda timeout crisis on large repos. Shard-suffixed
  vector keys guard the orphan-cleanup sweep from deleting in-flight
  shard summaries mid-merge.
- **License layer v2.0.0 (desired-state snapshot)** — per-Lambda
  layer that reports `repos-active` via `report_repos_snapshot()`
  from every integration's connect / disconnect handler + an hourly
  heartbeat scheduler. Replaces the older `track_repo_active`
  event-based model (which drifted on missed events). License
  server sees the ground truth, not a running tally.
- **AWS Marketplace SaaS listing** — customers can subscribe via
  AWS Marketplace; metering + entitlement events flow through the
  standard Marketplace SNS topics (limited release, 2026-05-30).

## See also

- `docs/architecture.md` — high-level system diagram.
- `docs/getting-started.md` — first deploy walkthrough.
- `docs/deployment-guide.md` — full deployment runbook.
- `docs/audit-coverage.md` — every event the platform records.
- `docs/audit-dashboard.md` — audit page operator guide.
- `docs/audit-stream-export.md` — SIEM export over Kinesis (OCSF).
- `docs/refusal-detection.md` — how the four-part refusal-classification
  contract works and why it's hard to do well.
- `docs/adr/` — architectural decisions, including ADR-018 (audit),
  ADR-014 to 017 (code maps).
- `docs/roadmap.md` — what's coming next.
- `docs/backlog.md` — known gaps and tracked debt.
