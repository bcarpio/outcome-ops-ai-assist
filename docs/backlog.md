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

## Closed (kept for historical context until pruned)

_(none yet)_
