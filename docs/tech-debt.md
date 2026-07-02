# Technical Debt

## Code Maps: Oversized Test File Batches

**Discovered:** 2026-02-26
**Severity:** Low
**Component:** `process-repo-summary` / `process-batch-summary`

The repo summary pipeline batches files by group for code-map generation. Test file groups (e.g., `batch_type: "tests", group_name: "unit"`) can contain 60+ files in a single batch. When `process-batch-summary` tries to summarize all files at once, the concatenated content exceeds Bedrock's input limits, causing the Lambda to fail and messages to land in the code-maps DLQ.

**Impact:** Code-map summaries for large test directories are never generated. Non-blocking since test summaries are low-value for KB search.

**Fix:** Cap `file_paths` per batch in `process-repo-summary` (e.g., max 20 files per batch) and split oversized groups into multiple messages.

## Teams File Sync: No Delta, Full Re-Processing Every Hour

**Discovered:** 2026-03-19
**Severity:** Medium
**Component:** `teams-sync`, `file-ingestion`

The Teams file sync re-processes ALL files every hourly sync cycle, even if they haven't changed. This creates unnecessary API calls to Microsoft Graph, redundant S3 uploads, and re-embedding in the vector database.

**Root cause chain:**
1. `teams-sync/handler.py` `_sync_channel_files()` (lines 569-611) does a full enumeration of all files in each channel's SharePoint folder with no delta filtering
2. Each hourly sync generates a new `sync_id`, which changes the SQS `MessageDeduplicationId`, defeating cross-hour deduplication
3. `file-ingestion/handler.py` (lines 914-979) always downloads and uploads to S3 without checking if the file already exists or has changed
4. Every S3 upload triggers `process-documents`, which re-embeds the file in the vector database

**Impact:** Wasted compute, unnecessary Microsoft Graph API calls (risk of throttling at scale), redundant vector embeddings. Not functionally broken but scales poorly.

**Comparison:** Confluence filters pages by `last_sync_at` modification time. SharePoint uses drive delta tokens for files and `lastModifiedDateTime` filtering for pages. Teams messages correctly use Microsoft's delta API. Only Teams files lack delta sync.

**Fix (two layers):**
1. **Teams sync (primary):** Use the SharePoint drive delta API for channel files (Teams stores files in SharePoint under the hood) or filter files by `lastModifiedDateTime` >= `last_sync_at`, matching the Confluence/SharePoint pattern
2. **File ingestion (safety net):** Before uploading to S3, compare the file's etag or content hash against the existing S3 object. Skip upload if unchanged

## Audit: Bedrock Pricing Env Vars Wired in Terraform but Ignored by audit.py

**Discovered:** 2026-05-03
**Severity:** Medium
**Component:** `lambda/shared/audit.py`, `terraform/lambda-chat.tf`, `terraform/lambda-code-generation.tf`

Pricing is exposed as Terraform variables in `prd.tfvars` and propagated to the relevant Lambdas as env vars:

```hcl
# terraform/prd.tfvars
bedrock_advanced_input_price  = 3.0
bedrock_advanced_output_price = 15.0
bedrock_basic_input_price     = 1.0
bedrock_basic_output_price    = 5.0

# terraform/lambda-chat.tf:61-64 (and lambda-code-generation.tf)
BEDROCK_ADVANCED_INPUT_PRICE  = tostring(var.bedrock_advanced_input_price)
BEDROCK_ADVANCED_OUTPUT_PRICE = tostring(var.bedrock_advanced_output_price)
BEDROCK_BASIC_INPUT_PRICE     = tostring(var.bedrock_basic_input_price)
BEDROCK_BASIC_OUTPUT_PRICE    = tostring(var.bedrock_basic_output_price)
```

But `lambda/shared/audit.py:61-79` ignores those env vars and uses hardcoded Python constants:

```python
ADVANCED_PRICING = {"input": 3.00, "output": 15.00}
BASIC_PRICING    = {"input": 0.80, "output": 4.00}
EMBEDDING_PRICING= {"input": 0.02, "output": 0.0}
```

These drive `_calculate_cost()`, which materializes `cost_usd` onto every audit row at write time. Historical rows are not rewritten if pricing changes - that is correct enterprise audit behavior.

**Why this matters:**

1. The Terraform plumbing is dead code from audit.py's perspective. The numbers happen to match for `ADVANCED` today, but `BASIC` already disagrees: tfvars says `input=1.0/output=5.0`, audit.py says `input=0.80/output=4.00`. Whichever invocation path counts the cost, only one of those is actually used, so dashboard numbers depend on which constant table wins.
2. Updating pricing requires a code change + Lambda deploy, even though the operator already updated `prd.tfvars`.
3. New Lambdas that import `shared.audit` inherit the hardcoded values silently.

**Fix:**

1. In `audit.py`, read `BEDROCK_ADVANCED_INPUT_PRICE` / `BEDROCK_ADVANCED_OUTPUT_PRICE` / `BEDROCK_BASIC_INPUT_PRICE` / `BEDROCK_BASIC_OUTPUT_PRICE` (and an embedding pair if added) at module load, fall back to current constants if unset, log a warning when falling back.
2. Reconcile the BASIC mismatch: pick whichever value is actually being charged and update the other source so they agree.
3. Make sure every Lambda that uses `shared.audit` has these env vars wired in its Terraform module - currently only `lambda-chat.tf` and `lambda-code-generation.tf` set them; modules like `process-batch-summary`, `query-kb`, `process-documents`, `process-repo-summary`, `analytics-cost-alert`, etc. do not.
4. Add a unit test that verifies `_calculate_cost()` honors the env vars.

**Future enhancement (not part of this debt):** per-org pricing overrides editable from the org-admin UI, since different enterprise customers negotiate different Bedrock rates / EDPs. Track separately if needed.

**Resolved 2026-05-03** (commit 5050303 + follow-up):

- `lambda/shared/audit.py` and `lambda/process-pr-check/check_handlers/bedrock_utils.py` both read `BEDROCK_{ADVANCED,BASIC,EMBEDDING}_{INPUT,OUTPUT}_PRICE` env vars at module load.
- `lambda/chat/handler.py`, `lambda/ask-claude/handler.py`, and `lambda/generate-code/utils.py` were already env-var-driven; verified.
- New Terraform variables `bedrock_embedding_input_price` / `bedrock_embedding_output_price`.
- Pricing env vars wired into every Terraform module whose Lambda imports a pricing-aware module: `lambda-knowledge-base.tf`, `lambda-pr-analysis.tf`, `lambda-file-ingestion.tf`, plus embedding-pricing parity on `lambda-chat.tf`, `lambda-code-generation.tf`, `lambda-query.tf`.
- BASIC mismatch (prd.tfvars 1.0/5.0 vs constants 0.80/4.00) is resolved at deploy time: tfvars wins.


## Grok 4.3 fabricates aggregate headers + pattern claims on structural graph questions

**Discovered:** 2026-07-01
**Severity:** High — blocks Grok as a customer-shippable basic-tier default
**Component:** `chat` / prompt discipline (ADR-032 no-model-authored-aggregates rule, ADR-027 v1.2 divergence discipline)

The Claude/GPT-5.5 divergence that ADR-027 v1.2 (commit `2d180d1`, 2026-06-19) closed **does not extend to Grok 4.3**. Live-verified against `xai.grok-4.3` on prd (us-west-2) via the basic-tier swap.

**Repro query (structural / graph-backed):**

> "who calls require_auth?"

The graph tool returns 94 callers across 24 modules. Claude and GPT-5.5 both correctly render the result without authoring aggregates. Grok 4.3 authors three separate aggregates that either don't match the enumerated data OR aren't in the graph result at all.

**Fabrication #1 — aggregate module count.**

Grok's response opens: *"require_auth is called from 40 distinct Lambda handler modules in the workspace."*

Actual count from Grok's own enumerated list below the header: 4 core + 20 integrations = **24 modules, not 40**. This is verbatim the banned example from the 2026-06-04 regression write-up (`"40 distinct Lambda handler modules ... 94 direct call sites"`) — Grok generates the exact string pattern the prompt rule was hardened against.

**Fabrication #2 — pattern claim.**

Grok's response asserts: *"The graph lists exactly two call sites per integration handler."*

Verified against the same graph result Claude quoted in the same session:
- `github-integration/handler.py` — **7 sites** (lines 1439, 1468, 1490, 1530, 1620, 1660, 1699)
- `notion-integration/handler.py` — **1 site** (line 1427)
- `servicenow-integration/handler.py` — **1 site** (line 1288)

The "exactly two" claim is factually wrong; Grok invented a uniform pattern that happens to match the modal case (asana, box, clio, etc.) and generalized it into a rule.

**Fabrication #3 — pseudo-reconciliation math.**

Grok's response includes: *"the total_known count across all modules is the number of definitions above; the per-module call-site counts match the 'Direct callers (N sites)' figures shown for each definition."*

Model-authored reasoning about how aggregate counts should reconcile. `total_known` is not a field the graph tool returns; Grok invented a variable name and reasoned about its shape. Exactly what ADR-032's no-model-authored-aggregates rule prohibits.

**Why this matters right now:**

Prior in-session bake-off (same session as this filing) had Grok 4.3 grounding cleanly on grounded-code-explanation queries (magic-link auth flow) — no fabrication, ~6x lower output cost than Claude Sonnet 4.6, plausibly a customer-shippable basic-tier default. This structural-question failure retracts that recommendation. Grok cannot ship as a customer-facing basic default until the aggregate-fabrication rule tightens to cover its output shape.

**Fix shape:**

Per `[[feedback_prompt_rules_few_shot]]` discipline (established when closing the Claude/GPT-5.5 divergence): the correct fix is **not** per-model branching. It is an ADR-027 v1.3 rule-wording + few-shot tightening pass that adds a Grok-triggering example to the anti-aggregate prompt rule.

1. **Add a Grok-shaped BANNED example to ADR-027's few-shot block.** The current examples were tuned against Claude and GPT-5.5 output shapes; Grok emits a different surface (`"called from N distinct modules"` opener plus `"exactly two per handler"` pattern generalization). Add both shapes as explicit BANNED examples. Same commit shape as `2d180d1`.
2. **Tighten the wording of the rule itself.** The current rule prohibits authoring aggregate totals; extend it to prohibit authoring per-item uniformity claims (e.g., "each X has Y items") when the enumerated data shows any variance. This is the underlying discipline; Grok slips past the current wording because its fabrication is a shape (per-item uniformity) the rule does not name.
3. **Add a positive example of correct rendering** — how Claude/GPT-5.5 handle the same query, with the per-module counts enumerated and no header aggregate. Positive few-shots closed the earlier Claude/GPT-5.5 divergence and are the pattern likely to close this one.

Options 1 + 2 + 3 together are one prompt-rule commit. No code change, no per-model branch, no revert. Deploy path is `make build-platform-layer` + terraform apply, same as any prompt update.

**Interim posture until fixed:**

- Grok basic on prd stays in place for continued eval — the failure mode is now characterized and understood, and the maintainer sandbox is the right place to shake it out.
- Grok basic is **NOT** customer-shippable as a basic-tier default until this ships. Any customer demo of the Grok chat surface should route to advanced (Claude Sonnet 4.6) instead.
- Do NOT branch per-model in the prompt path to work around it (per feedback discipline).

## PR-check handlers query the KB workspace-blind

**Discovered:** 2026-07-01
**Severity:** High — quality of `ADR_COMPLIANCE`, `BREAKING_CHANGES`, `ARCHITECTURAL_DUPLICATION`, `LICENSE_COMPLIANCE`, `TEST_COVERAGE` depends on which workspace's docs happen to rank highest globally
**Component:** `lambda/process-pr-check/check_handlers/` (all 6) + `lambda/query-kb/handler.py`

The six PR-check handlers call `query_knowledge_base(lambda_name, query, top_k)`
with **no workspace or repo filter**. `query-kb` supports a `repo` filter
(handler.py:201, 216-233) — but the check handlers don't pass one.

The vector-store keys are already workspace-scoped:
`{workspace_id}#{provider}#{repo}#{doc_type}#{identifier}`. But without a
workspace filter on retrieval, the query searches the whole S3 Vectors
index and the reranker returns whatever ranks highest globally — regardless
of which workspace the PR belongs to.

**Live repro (2026-07-01):**

Ran ADR_COMPLIANCE against the same handler file (`lambda/health-check/handler.py`)
in two workspaces:

- **GitHub side** (outcome-ops-ai-assist-private repo, ingested into the
  Engineering workspace which also has `outcomeops-adrs` connected):
  reranker returned ~6,336 input tokens of context including
  ADR-004 lambda handler standards. Claude found 3 real ADR-004
  violations — `logging.getLogger(__name__)` instead of root logger,
  missing `exc_info=True`, wrong CORS header value. **All three are
  correct findings.**

- **Azure DevOps side** (ooai-license-server repo, ingested into the
  Azure DevOps workspace which does NOT have `outcomeops-adrs` connected):
  reranker returned ~3,202 input tokens of a different context set.
  Claude judged the file "follows ADR standards." **The three real
  violations were missed** because ADR-004 content wasn't in the
  top-K.

Same file, same handler code, same Claude model. Different check
quality driven purely by which docs ranked highest globally at
retrieval time.

**Why this matters more for customers than for the maintainer:**

A Fortune 500 running the platform in their own AWS account will have:

- **Their own ADRs / standards** ingested into a shared workspace
- **Their own application code** ingested into per-team workspaces
- **Optionally, "parent org" standards** the platform lets them
  cross-reference

Today the platform:

1. Scopes vectors by workspace on WRITE (correct)
2. Ignores workspace scope on READ during PR-check retrieval (bug)

Effect for a customer: an ADR from Team A's workspace could influence a
check on Team B's PR just because it happens to rank higher for the
query. Or (as in the ADO smoke above), a workspace without ingested ADR
docs gets much weaker checks than one with them.

**Design question (needs an ADR before implementation, not just a code fix):**

1. **Which workspace scopes a PR check?** The workspace the repo is
   connected to (`WS#{ws}#GITHUB_REPO#{repo}` for GitHub,
   `WS#{ws}#AZDO_REPO#{slug}` for ADO). `analyze-pr` already receives
   `repository`; a `repo → workspace` resolution already exists in the
   codebase.

2. **What about parent-org / global standards?** Some customers want
   one "standards" workspace consulted from all app workspaces.
   Workspace metadata already has a `search_allowed_workspaces` field
   (visible in the DDB scan). The ADR check could search the PR's
   workspace + any workspaces listed in `search_allowed_workspaces`.

3. **Repo-level filter vs workspace-level filter.** `query-kb` accepts
   `repo`. Workspace-level (all of that workspace's ingested repos) is
   probably the right grain — the customer's ADR repo may live in a
   different repo from their code repo. Repo-level filter would be too
   narrow for the cross-repo standards case.

**Fix shape (once the ADR lands):**

1. **Add `workspace_id` to `CheckJob`.** Propagates through SQS the
   same way `provider` / `connection_id` do. Populated by `analyze-pr`
   via the `repo → workspace` resolution that already happens for
   audit-log emission.
2. **Add `workspace_ids: List[str]` (plural) to the CheckJob** if step
   2 of the design question comes back "search parent-org too."
3. **Widen `check_handlers/*/query_knowledge_base()`** to accept +
   forward `workspace_ids` to query-kb.
4. **Widen `query-kb` filter** to accept `workspace_ids` list and
   push the filter down to the S3 Vectors query (its key prefix is
   the workspace, so this is exact-prefix filtering — cheap).

**Interim posture until fixed:**

- The maintainer's own dogfood (running against `outcome-ops-ai-assist-private`
  in the Engineering workspace) works well because that workspace has
  `outcomeops-adrs` connected and the ADR content ranks high globally
  anyway. GitHub PR-check quality is good today by lucky retrieval alignment.
- ADO PR-check quality against customer repos will be weaker until this
  ships. Every customer engagement should ingest their standards workspace
  BEFORE relying on ADR_COMPLIANCE / BREAKING_CHANGES / ARCHITECTURAL_DUPLICATION
  findings, and the check comments should be treated as advisory pending
  the scoping fix.
- Do NOT paper over this by hardcoding a workspace_id or repo filter in
  the check handlers — the design decision is the customer-facing scoping
  model, not a shortcut.
