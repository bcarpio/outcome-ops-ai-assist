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

**Not blocking the lighthouse PoC** -- the name is a cosmetic embarrassment
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

### Claude Sonnet 5 support in `shared._converse_backend`

**Filed:** 2026-07-01
**Severity:** Medium (unblocks a real model swap, not urgent)
**Component:** `lambda/shared/_converse_backend.py`, `lambda/shared/model_client.py`

Sonnet 5 (`anthropic.claude-sonnet-5`, `us.anthropic.claude-sonnet-5`,
`global.anthropic.claude-sonnet-5`) is available and ACTIVE in
us-west-2 as of 2026-07-01. Live Converse invoke against
`us.anthropic.claude-sonnet-5` succeeded with account model access
already granted. But swapping `bedrock_advanced_model_id` to it
requires backend work first — three behavior deltas vs Sonnet 4.6:

1. **`temperature` is deprecated.** Passing any temperature value
   returns `ValidationException: 'temperature' is deprecated for
   this model`. Our chat handler currently sets `temperature=0.1`
   on the advanced-tier Converse call; that would fail hard.
2. **Reasoning-native model** (same posture as GPT-5 family). Every
   response's `content[0]` is a `reasoningContent` block (empty
   `text` + `signature`), then `content[1..N]` are the message
   text blocks. Existing `_extract_text(content_blocks)` walks all
   blocks and pulls `.get("text")` from each, so should Just Work
   for reading, but need to verify.
3. **Reasoning tokens bill against `maxTokens` and the output rate**
   — same tax GPT-5 has. A normal chat call at `maxTokens=4096`
   regularly starves visible output on hard turns. Our
   `_responses_backend` already gives `max_output_tokens ×= 4`;
   `_converse_backend` would need the same headroom applied for
   the Sonnet-5 family.

**Fix shape (one commit, mirrors what `_responses_backend` did for GPT-5):**

1. Detect Sonnet-5 family by model-ID prefix (`us.anthropic.claude-sonnet-5`,
   `anthropic.claude-sonnet-5`, `global.anthropic.claude-sonnet-5`) in
   `_converse_backend.invoke` / `invoke_stream`.
2. If matched: strip `temperature` from `inferenceConfig`, multiply
   `maxTokens` by a headroom constant (start with 4×, tune from
   there), and add reasoning-token accounting to usage.
3. Update pricing constants in `shared.audit` when we know Sonnet 5's
   published per-M rates (unknown at time of filing).
4. Live smoke a chat turn on the openai workspace or a scratch
   workspace against `bedrock_advanced_model_id = "us.anthropic.claude-sonnet-5"`
   and confirm the chat UI streams a real answer.

**Not blocking anything today.** prd runs cleanly on Sonnet 4.6.
Reopen when the demand for Sonnet 5 shows up (customer ask, blog
post, or maintainer curiosity).

### LICENSE_COMPLIANCE check has hardcoded policy patterns

**Filed:** 2026-07-01
**Severity:** Medium — surfaces the moment a customer's SecOps has a
different license posture than the platform maintainer's
**Component:** `lambda/process-pr-check/check_handlers/license_compliance.py`

The LICENSE_COMPLIANCE check is partially customer-configurable: it
queries the KB for the customer's license policy (`query_knowledge_base("...our
open source license compliance policies?...")`) and passes the answer
to Claude as `license_policy` for judgment. That path IS customer-tunable
via docs per ADR-007.

But the pre-scan pattern detection (`detect_license_patterns` +
`check_copyright_header` + `is_ai_generated_commit`) is hardcoded in
`license_compliance.py:35-96`:

- `PROHIBITED_PATTERNS` — regex for GPL/AGPL/SSPL only
- `ALLOWED_PATTERNS` — MIT/Apache/BSD/ISC only
- `REVIEW_PATTERNS` — LGPL/MPL only
- `COPYRIGHT_PATTERN` — matches "Copyright YYYY Name" only
- `SPDX_PATTERN` — matches the SPDX identifier line format
- `GPL_ALGORITHM_PATTERNS` — hardcoded regex for specific GPL-implementation
  code shapes
- `SOURCE_EXTENSIONS` — the file types that get scanned
- `is_ai_generated_commit()` markers — Claude-only (`Co-Authored-By:
  Claude`, `Generated with Claude Code`)

Concrete customer scenarios this breaks:

- **Customer prohibits LGPL.** The pre-scan tags LGPL as
  `review_required`, not `prohibited`. Claude might override based
  on the policy but the pre-scan miscategorizes it.
- **Customer requires a company-specific copyright header format**
  (e.g., `Copyright (c) 2026 ACME Corp` with parens, or "Property of
  ACME. All rights reserved.", or SPDX-only headers). The current
  regex misses non-standard shapes and the file gets falsely flagged
  as missing header.
- **Customer uses Cursor / Copilot / Windsurf** (not Claude Code).
  Phase 2 AI-generated detection never triggers because their
  commit trailers don't match the hardcoded Claude markers.

**Fix shape (per ADR-007 "documentation-driven decisions"):**

1. Move `PROHIBITED_PATTERNS` / `ALLOWED_PATTERNS` / `REVIEW_PATTERNS`
   to a customer-configurable location. Two viable slots:
   - **KB entry** — a well-known ADR / policy doc the customer
     ingests. The check parses it at runtime. Consistent with how
     the current `license_policy` is already retrieved.
   - **`.outcomeops.yaml`** — a `license_compliance:` block in each
     repo's config file. More per-repo granular but requires
     schema changes.
   KB-entry is probably right for org-wide policy; `.outcomeops.yaml`
   is right if per-repo overrides matter.
2. Same for `COPYRIGHT_PATTERN` / `SPDX_PATTERN` — customer specifies
   the acceptable formats. Default to today's regex if unset.
3. `AI_GENERATED_MARKERS` — customer-provided list of trailer / body
   patterns that indicate AI authorship. Default: Claude + common
   Copilot / Cursor markers so the average customer gets useful
   detection out of the box.
4. `SOURCE_EXTENSIONS` — customer specifies which file types matter.
   Default to today's list.

Alternative (bigger scope, cleaner design): replace the pre-scan
regex layer entirely with an LLM pass. Claude gets the file diff +
license policy + AI-tool markers as prompt input and does all the
detection. Removes the "hardcoded pattern coverage" problem
completely at the cost of one extra LLM call per file.

**Interim workaround for customers who need it now:** write a very
explicit ADR-008 in their KB spelling out where the platform's
hardcoded pattern coverage differs from their intent. Claude reads
that policy and interprets findings accordingly. Doc-only hack; the
real fix is de-hardcoding the patterns.

**Related:** the `[[tech-debt: PR-check handlers query the KB
workspace-blind]]` entry — even if a customer configures their own
policy, workspace-blind retrieval means the wrong workspace's policy
could win the ranking. Fix both together for a coherent customer
experience.

## Closed (kept for historical context until pruned)

_(none yet)_
