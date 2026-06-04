# Backlog

Tracked low/medium-priority work that hasn't shipped yet. Not a roadmap
(see Jira); this is the engineering-internal "we noticed it, didn't fix
it now, don't lose it" file.

## Open

### Build-everything target discoverability

**Filed:** 2026-06-04
**Severity:** Low
**Component:** Makefile / docs

`make pre-deploy` already does what every operator instinctively
reaches for — runs `build-all-layers` + `build-all-images` and
transitively builds every layer (runtime, platform-ca, terraform-tools)
and every container image (run-tests, ui, mcp-server, snyk, database).
Idempotent end-to-end. As of 2026-06-04 the build-terraform-layer
gap is closed (added to `scripts/build-all-layers.sh`).

**Remaining ergonomics concern:** `pre-deploy` is the wrong name.
It sounds like a CI step, not "do every build at once before terraform
apply." Operators don't discover it from `make help` because the
verbal cue isn't there, so they reach for 5-7 separate `make
build-{thing}` invocations and frequently miss one.

**Fix shape:**
1. Add an alias target: `build-all` or `build-everything` defined as
   `build-all: pre-deploy`. Keep `pre-deploy` as the canonical name
   for docs / muscle memory; the alias is just for discoverability.
2. Update `make help` to advertise the alias prominently in the
   "Bootstrap a workspace" section (currently `make help` doesn't
   call out `pre-deploy` at all).
3. Update `docs/getting-started.md` and any per-environment quickstart
   to lead with the new target.
4. Optional: post-build verification step that prints ECR URIs +
   SHA256 digests so the operator can confirm push success without
   greping terraform output.

**Not blocking:** `ENVIRONMENT=<env> make pre-deploy` works correctly
end-to-end for any workspace including the openai one. This is
discoverability + ergonomics, not correctness.

---

### Rename `ask-claude` Lambda to be model-agnostic

**Filed:** 2026-06-04
**Severity:** Low (cosmetic; functional behavior is correct)
**Component:** Lambda naming / SSM / terraform / cross-Lambda imports

Surfaced during the openai-workspace deploy when the terraform log
showed `module.ask_claude_lambda.aws_lambda_function.this[0]: Still
creating...` -- a perfectly normal deploy line that just happens to
name "claude" while building an OpenAI workspace. The Lambda is
correctly using whatever `bedrock_advanced_model_id` resolves to
(GPT-5.5 in this case); only the IDENTIFIER is misleading.

**Rename surface (audited 2026-06-04):**

- `terraform/lambda-query.tf` -- `module "ask_claude_lambda"` declaration,
  function_name (`${var.environment}-${var.app_name}-ask-claude`),
  ARN + name outputs
- `terraform/lambda-code-generation.tf` -- consumers reference
  `module.ask_claude_lambda.lambda_function_arn`
- SSM parameter: `/<env>/<app>/lambda/ask-claude-arn`
- Code directory: `lambda/ask-claude/` (and its `handler.py`)
- 4 sibling Lambdas that invoke it:
  - `lambda/generate-code/knowledge_base.py`
  - `lambda/query-kb/handler.py`
  - `lambda/process-pr-check/check_handlers/test_coverage.py`
  - `lambda/run-tests/auto_fix.py`
- Tests: `lambda/tests/unit/test_ask_claude.py` + Makefile + README

**Suggested new name:** `synthesize` (describes the action -- synthesize a
grounded answer from RAG context + the symbol graph), or `ask-model`
(generic and obvious). I'd lean `synthesize` because it's a noun the
audit narrative can use (`SYNTHESIZE_QUERY` audit actions read cleanly).

**Why this is more than a sed -i:**

- Lambda function name change drops the old function + creates a new
  one. Any cached ARN in SSM, CloudWatch alarms, EventBridge rules,
  or downstream Lambda IAM policies needs updating in the same apply.
- Audit log table has `lambda_function_name = "ask-claude"` rows back
  to ingest; renaming creates a discontinuity unless we either
  back-fill the audit table or accept the boundary.
- Cross-Lambda invokes (the 4 above) call by name; the renamed
  function won't be found until those imports update.
- The terraform `module.ask_claude_lambda` name change requires a
  `terraform state mv` in each workspace's state OR a destroy/create
  cycle (the second is brutal -- it'd take the chat-synthesis Lambda
  offline for a few minutes).

**Not blocking the Boeing PoC** -- the name is a cosmetic embarrassment
during demos but doesn't affect platform behavior. Worth doing before
a public marketing push of the model-agnostic story; deferred until
then.

---

---

### Customer-visible runbook: Bedrock API key provisioning + rotation

**Filed:** 2026-06-04
**Severity:** Medium (compliance gating for regulated customers)
**Component:** docs/runbooks / customer-facing ops

When a deploy enables GPT-5.5 / GPT-5.4 / Codex (i.e. any model on the
`bedrock-mantle` endpoint), the platform must authenticate with a
long-lived Bedrock API key, NOT IAM. This is AWS's design choice for the
OpenAI-frontier models on Bedrock; SigV4 is technically supported but the
OpenAI SDK + bearer token is the canonical path and the one we use.

For regulated customers (healthcare, financial services, defense), this
collides with secret-rotation policy. Typical SecOps requires 30-90 day
max secret lifetimes; a Bedrock "long-term API key" is the opposite
design.

**What needs to ship:**

1. `docs/runbooks/bedrock-mantle-api-key.md` (customer-visible, NOT
   `plan/`) covering:
   - Why the key exists (model availability matrix; GPT-5.5 is Mantle-only)
   - How to generate one in the Bedrock console
   - How to install it into the customer's deploy (`aws ssm put-parameter`
     into `/<env>/outcome-ops-ai-assist/bedrock/mantle-api-key`)
   - How to rotate (generate new key in console, put-parameter overwrites,
     warm Lambdas pick up new value on cold start)
   - Compliance-disclosure language for customer SecOps sign-off:
     "Enabling Mantle models requires accepting a long-lived bearer token
     per AWS's product design. This is a known deviation from typical
     SecOps key-rotation cadence."
2. The deploy checklist (`docs/deploy-checklist.md` if it exists, or new)
   adds a "Mantle enablement" step that prompts the operator to either
   provision the key (after SecOps sign-off) OR confirm the deploy is
   Converse-only and skip Mantle models entirely.
3. An automated rotation Lambda (cron-driven, talks to the Bedrock
   control plane to mint a new key and update SSM). Separate, larger
   effort; backlog this whole rotation-automation work as a follow-on.

**Not blocking ADR-035 / chunk 03** — the platform code only needs to
read the SSM SecureString. Provisioning is a deploy-time step. But the
moment the openai workspace's deploy README is customer-facing, this
runbook must exist.

See memory `[[project_bedrock_mantle_api_key]]` for the underlying
decision and context.

---

## Closed (kept for historical context until pruned)

_(none yet)_
