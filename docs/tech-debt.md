# Technical Debt

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

## `ui_private_subnet_ids` is misnamed — it accepts public OR private subnets

**Discovered:** 2026-07-31
**Severity:** Low (cosmetic / clarity — mitigated by docs)
**Component:** `terraform/variables.tf` (`ui_private_subnet_ids`,
`ui_private_subnet_ids_secondary`), consumed by `ui-fargate.tf`,
`ui-fargate-secondary.tf`, `mcp-outcomeops.tf`, `mcp-snyk.tf`,
`mcp-sonarqube.tf`, `fargate-secondary.tf`

The variable name says "private," but the subnets it takes may be **public or
private** — the actual outbound model is governed by `ui_fargate_assign_public_ip`
(true = public subnets on an IGW, no NAT; false = private subnets with
NAT/Transit Gateway/Direct Connect). A customer with public subnets (the
cheap, no-NAT path) is steered wrong by the name. The description, validation
message, tfvars example, and getting-started were clarified (commit `9be3b5d`),
but the **name itself** still misleads.

**Why it is deferred, not a drive-by rename:** renaming `ui_private_subnet_ids`
→ `ui_subnet_ids` touches 7 `.tf` files plus the `_secondary` variant, the
`prd.tfvars.example`, the docs, **and every existing `<env>.tfvars`** — terraform
errors on the old name once the variable is renamed, so it must be changed in
lockstep with the maintainer's live `prd`/`dev`/`openai` tfvars on the next
apply. It is a clean breaking change with a small blast radius **while there are
no production customers**, so it is cheapest to do soon.

**Suggested fix:** rename to `ui_subnet_ids` (+ `ui_subnet_ids_secondary`),
update all terraform references + example + docs, and update the maintainer's
tfvars in the same change. Do it before the first production customer locks the
name in.

