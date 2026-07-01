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

## Code Maps: Hardcoded `main` Branch in File Listing

**Discovered:** 2026-05-03
**Severity:** Medium
**Component:** `process-repo-summary`

`list_repository_files()` in `lambda/process-repo-summary/handler.py:355` hardcodes the branch name in the GitHub trees API URL:

```python
url = f"{GITHUB_API_URL}/repos/{repo_project}/git/trees/main?recursive=1"
```

Any repo whose default branch is not `main` (e.g., `master`, `develop`, `trunk`) will 404 here, and `process-repo-summary` will fail to discover files for that repo.

**Impact:** F500 customers commonly have pre-2020 repos still defaulting to `master`. For those repos, code-map generation silently fails at the file-listing step. Affects every backend (Python, Java, TypeScript, React, ABAP, C#) - this is not language-specific.

**Inconsistency:** The per-backend change-detection code already does this correctly. For example, `csharp_backend.py:494` (`_get_default_branch`) and the equivalents in the other backends call `GET /repos/{repo}/` and read `default_branch` before fetching commit/compare data. So one half of the pipeline resolves the branch dynamically and the other half hardcodes `main`.

**Fix:**
1. Extract the existing `_get_default_branch` logic into a shared helper (or move it onto the base backend) so the handler can call it once per repo.
2. Replace the hardcoded `main` in `list_repository_files()` with the resolved default branch.
3. Pass the resolved branch through to `detect_changes()` so the backend doesn't re-resolve it.
4. Add a unit test covering a repo with `default_branch: "master"`.

**Workaround until fixed:** Only point the platform at repos whose default branch is `main`.

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


## Chat retrieval can't reach dependency manifests (JSON / lockfiles)

**Discovered:** 2026-06-29
**Severity:** Medium
**Component:** `chat` / dual-channel retrieval (ADR-033)

`search_kb_dual_channel` in `lambda/chat/handler.py` filters every turn through two type-scoped lanes: `CODE_MAP_TYPES` (`handler-group-summary`, `infrastructure-summary`, the rest of the `*-summary` types) and `DOC_TYPES` (`adr`, `markdown`, `html`, `pdf`, `yaml`, `pptx`, `docx`). Files stored under any other type — notably `json` — are filtered out of BOTH lanes. Even when the router classifies a turn as `intent=dependency`, the dependency manifests are unreachable.

**Impact:** Dependency / CVE / package-version questions return "no relevant documents found" against repos whose `package.json` / `package-lock.json` are correctly ingested. Verified live for the shape "CVE-XXXX-XXXXX in `<pkg>` — which apps run a vulnerable version": router output was `intent=dependency, symbol=<pkg>`, graph correctly skipped (a package name is not a code symbol), dual-channel RAG returned 0 chunks — despite 88 `package*.json_chunk_*` vectors existing for the repo in the index.

**Root cause:** ADR-033 dual-channel retrieval (shipped 2026-06-03) introduced the per-lane type filters to stop docs-heavy prose from out-ranking code-maps for structural questions. The comment beside the constants in `lambda/chat/handler.py` is explicit:

> *"Other types (json, text, unknown, conversation_memory, no-type) remain absent from the dual-channel filters. Those are raw build data / lookup files; not retrieval-relevant for structural or narrative answers."*

Correct for "how does X work" questions; silently kills dependency-question retrieval as a side effect. Pre-ADR-033 the single combined lane had no type filter, so dependency-manifest chunks competed on cosine similarity and could win when the query was dependency-shaped — which is why the same question shape worked before ADR-033 and stopped working after.

**Fix shape (options, in increasing surgical precision):**

1. Add `"json"` to `DOC_TYPES`. Smallest change. Reintroduces the docs-vs-code-maps rerank fairness problem ADR-033 was solving — but only when JSON ranks high enough to push a code-map out of the docs lane, which on most question shapes it won't.
2. Add a third lane keyed on `type_filter=("json",)` (and any future dependency-manifest types) that fires ONLY when the router returns `intent=dependency`. Doesn't touch the existing two-lane fairness; dependency questions get a dedicated budget; normal questions are unchanged. Likely the right long-term answer.
3. In the `intent=dependency` branch of `lambda/chat/handler.py` (around the graph-skip-no-symbol path), fire a one-off unfiltered `search_kb` for the turn before the dual-channel pair. Smallest blast radius; bypasses the lane filters for that one intent without a permanent lane.

Option 2 is the right shape long-term; option 3 ships fastest if the dependency-question surface is small enough not to need a permanent lane. Either way the `CODE_MAP_TYPES` / `DOC_TYPES` rationale comment should be updated when this lands so the next reader doesn't read "json deliberately excluded" without the "but only for non-dependency turns" caveat.

## Dual-channel 50/50 lane budget starves multi-domain questions

**Discovered:** 2026-06-29
**Severity:** Medium
**Component:** `chat` / dual-channel retrieval (ADR-033) interacting with list-completeness discipline (ADR-032)

`search_kb_dual_channel` and the per-lane rerank in `lambda/chat/handler.py` both impose a **rigid 50/50 split** between the code-maps lane and the docs lane:

```python
code_map_k    = max(1, fetch_k // 2)
doc_k         = max(1, fetch_k - code_map_k)
# ...
code_map_top_n = max(1, request.top_k // 2)
doc_top_n      = max(1, request.top_k - code_map_top_n)
```

With the default `top_k=10`, the final context is **always** exactly 5 code-maps + 5 docs after rerank, regardless of which side has the answer. Pre-ADR-033 the single combined lane returned the top-10 overall — for a docs-heavy question it could surface 8 docs + 2 code-maps, and for a code-heavy question the inverse.

**Impact:** Questions that legitimately need a docs-heavy or code-heavy mix (architectural reviews that span both source code and adjacent standards/ADRs, hybrid-deployment questions that need a codebase summary + several compliance docs) get half their natural retrieval depth in whichever lane the question favors. The remaining slots go to lower-relevance hits from the opposite lane and crowd out the right ones.

The starvation compounds with ADR-032's list-completeness / no-fabrication discipline: when the model sees thin context for one side of a multi-domain question, it correctly refuses to synthesize an answer rather than guess. The combined effect is a regression where the same question that previously got a useful answer now gets a refusal.

**Live repro (2026-06-29):**

> *Query A* (terse): "Review our SAP codebase and produce an ADR covering hybrid deployment risks..."
> → 4 sources, model refuses ("I don't have SAP ABAP codebase maps...")
>
> *Query B* (same intent, explicit naming): "Review our `abap-platform-refscen-flight` portal... Based on our SAP codebase and the ISO 27001, 27017, 27018 standards..."
> → 14 sources, model produces the answer

Same workspace, same SHA, same intent. The 50/50 lane ceilings + refusal-on-thin-context interaction is what differs.

**Fix shape:**

1. **Adaptive budget redistribution.** If one lane returns fewer candidates than its quota (the live case here — code-maps lane only had 2 useful hits), redistribute the unused budget to the other lane before rerank. Smallest behavior change; preserves ADR-033's fairness goal while not throwing slots away.
2. **Intent-aware split.** Use the router's intent classification to bias the split: `pattern` / `dependency` → more docs lane; `structural` → more code-maps lane; `hybrid` → keep 50/50. Already classified per turn; the signal is wasted today.
3. **Soft quota with relevance floor.** Rerank both lanes together but enforce a minimum of N from each side (e.g., 2 code-maps minimum, 2 docs minimum), letting the rest flow by absolute rerank score. Recovers most of the pre-ADR-033 behavior while still guarding against full lane starvation.

Option 1 is the smallest possible fix and addresses the specific repro above (one lane was returning fewer than its quota, slots were wasted on noise). Option 2 is the right long-term shape — the router's intent classification is already accurate enough to drive this. Option 3 is what was effectively in place pre-ADR-033 with a minimum-floor added; worth considering as an alternative to option 2 if the intent classifier ever drifts.

Worth doing alongside the JSON-lane fix in the entry above — both are symptoms of the dual-channel split being too rigid for queries it wasn't designed against.

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
