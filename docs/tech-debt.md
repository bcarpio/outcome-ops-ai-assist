# Technical Debt

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
**Severity:** Low — no current customer demand for Grok as a basic-tier default; sandbox eval only. Re-raise if a prospect asks for Grok cheaper-than-Claude economics on structural queries.
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

## `ask-claude` is a model-specific resource name in a model-agnostic platform

**Discovered:** 2026-07-30
**Severity:** Low (cosmetic, procurement-facing)
**Component:** `terraform/lambda-query.tf` (`module.ask_claude_lambda`), the SSM ARN parameter it publishes

`terraform/lambda-query.tf:15` deploys `${environment}-${app_name}-ask-claude`
(e.g. `prd-outcome-ops-ai-assist-ask-claude`) and publishes its ARN to an SSM
parameter at `/<env>/<app_name>/lambda/ask-claude-arn`. The Lambda's *code* is
model-agnostic (it dispatches via the shared model client / tfvars-driven model
selection), but the **resource name** bakes in a specific vendor model family.

**Why this matters:**

- The platform is sold as model-agnostic. A procurement or architecture reviewer
  grepping the deployed infrastructure for `claude` gets a hit on a function name
  — which is exactly the wrong signal for a customer that has standardized away
  from a given model vendor. The naming contradicts the positioning even though
  the behavior does not.
- It is purely cosmetic — no functional impact — but it is the kind of detail
  that snags a model-agnostic story during a review.

**Why it is not a drive-by fix:**

- Renaming the Terraform resource / `function_name` is a destroy/create (or a
  `terraform state mv` in every workspace's state) — the destroy/create path
  takes the chat-synthesis Lambda offline for a few minutes, so it is a
  deliberate, scheduled change, not an incidental rename.
- Downstream consumers read the ARN from the SSM parameter path above; a rename
  has to update the parameter name and every reader in lockstep.

**Suggested fix:** rename to a model-neutral identifier (e.g. `rag-answer` /
`chat-synthesis`) before any public marketing push of the model-agnostic story;
sequence it as a planned maintenance-window change. See also the related entry in
`docs/backlog.md`.

