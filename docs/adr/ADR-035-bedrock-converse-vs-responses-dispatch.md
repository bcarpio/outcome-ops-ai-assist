# ADR-035: Bedrock Converse vs Responses API dispatch via unified model client

## Status: Accepted

## Context

The platform reads `BEDROCK_ADVANCED_MODEL_ID` and `BEDROCK_BASIC_MODEL_ID` from
environment variables (terraform-driven from `tfvars`) and invokes them via the
boto3 `bedrock-runtime` client's `Converse` (or `ConverseStream`) API. This works
for any Bedrock model that the Converse API supports: `anthropic.*`, `meta.*`,
`amazon.nova.*`, and the recently-published `openai.gpt-oss-*` family.

On 2026-06-01 AWS shipped GPT-5.5, GPT-5.4, and Codex as GA on Amazon Bedrock —
but **not on the Converse API**. They are accessible only through the **Responses
API** on a new inference engine ("Bedrock Mantle") at endpoint
`https://bedrock-mantle.{region}.api.aws/openai/v1`. The endpoint speaks the
OpenAI Responses-API schema (`chat.completions`-style messages, `max_tokens`,
OpenAI-shaped tool calls, OpenAI usage fields, SSE-delta streaming). The
`bedrock-runtime` Converse API rejects these model IDs with
`ValidationException: The provided model identifier is invalid.`

Live-verified 2026-06-04 in the openai workspace: setting
`BEDROCK_ADVANCED_MODEL_ID = "us.openai.gpt-5-5:0"` caused `process-repo-summary`
to fail on its first invocation against this exact error. The model **is** GA,
the region is correct, the IAM is correct — the API is wrong.

This creates a divergence the existing tfvars-swap pattern doesn't span:

- **Model swap via tfvars works** for any Converse-compatible model (existing
  behavior; carried in the model-swappability tfvars contract).
- **Model swap to GPT-5.5 / 5.4 / Codex requires a different endpoint, different
  request schema, and different response schema** — the swap surface is no
  longer "one env var."

The platform must support both backends behind a single tfvars-driven swap:

- A lighthouse PoC customer has explicitly standardized away from Anthropic
  Claude across all services. The openai-workspace deploy needs OpenAI frontier
  (not OSS) to be a credible alternative.
- The broader customer base treats model swappability as a procurement
  requirement, particularly in regulated industries where the
  cloud-and-model-of-record is set by enterprise architecture, not by us.
- Bedrock positions Mantle as the long-term home for proprietary OpenAI
  frontier on AWS. Anthropic-shaped APIs are not going to absorb it.

## Decision

Introduce a single shared module `lambda/shared/model_client.py` that every
LLM-completion-calling Lambda imports. The module dispatches by model-ID prefix
to one of two backends. Callers see a single unified request/response shape and
remain agnostic to which backend ran.

### 1. Dispatch table

The shared client routes by **model-ID prefix**:

| Prefix | Backend | Reason |
|---|---|---|
| `anthropic.*`, `us.anthropic.*` | Converse (`bedrock-runtime`) | Native Converse |
| `meta.*`, `us.meta.*` | Converse | Native Converse |
| `amazon.nova.*`, `us.amazon.nova.*` | Converse | Native Converse |
| `openai.gpt-oss-*` | Converse | AWS shipped these on Converse |
| `openai.gpt-5-*`, `us.openai.gpt-5-*` | Responses (`bedrock-mantle`) | Mantle-only |
| `openai.gpt-5.4*`, `openai.gpt-5.5*` | Responses | Mantle-only |
| `openai.codex-*` | Responses | Mantle-only |
| anything else | **RAISE** | No silent fallback |

The dispatch table lives in code, not config. Adding a new family is a code
change plus tests; this is intentional — silent fallbacks for an unknown model
prefix are a class of bug that produces wrong-answer silently rather than
loudly. Per the platform's structural-attribution discipline (ADR-031, ADR-033),
the LLM is grounded by retrieval; routing the wrong model silently invalidates
that grounding.

**Update 2026-07-30 — GPT-5.6 (Terra / Luna / Sol).** AWS published the GPT-5.6
line. All three variants match the existing `us.openai.gpt-5-*` prefix, so they
route to the Responses backend with **no dispatch change** — the table above
already covers them; adopting them is a tfvars-only change (model IDs + pricing).
Tier / region / price facts (per-1M tokens, verified on the Bedrock pricing page
2026-07-30):

| Variant | Input | Output | Regions | Role |
|---|---|---|---|---|
| Sol | $5.50 | $33.00 | us-east-1, us-east-2 | premium flagship (= old 5.5 price) |
| Terra | $2.20 | $13.20 | us-east-1, us-east-2, **us-west-2** | 5.5-level quality, lower cost → advanced tier |
| Luna | $0.22 | $1.32 | us-east-1, us-east-2, **us-west-2** | fast/cheap high-volume → basic tier |

Because Terra + Luna reach **us-west-2** (the default region), an all-GPT deploy
can run advanced=Terra / basic=Luna in the home region without the us-east-2
relocation the us-east-2-only GPT-5.5 forced — the region obstacle behind the
demo-only OpenAI scope is removed. Two caveats remain: (1) Sol is still
us-east-1/us-east-2 only; (2) cost attribution — `audit.py` hardcodes per-model
pricing and ignores the `bedrock_*_price` tfvars (see `docs/tech-debt.md`), so a
GPT-5.6 deploy mis-attributes `cost_usd` until that is reconciled. Exact Mantle
model-ID strings are not returned by `list-foundation-models` (Mantle-served);
confirm them in the Bedrock console model catalog before deploying.

### 2. Unified request/response shape

The unified shape mirrors **Converse** (the existing dialect), so call-site
changes are minimal — most existing call sites only need to swap the import:

```python
from shared.model_client import invoke

resp = invoke(
    model_id=os.environ["BEDROCK_ADVANCED_MODEL_ID"],
    system="You are a synthesis assistant…",     # str | list[str], optional
    messages=[
        {"role": "user", "content": [{"text": "…"}]},
    ],
    max_tokens=4096,
    temperature=0.0,
    tools=[…],                                   # optional; Converse toolSpec shape
    stop_sequences=[…],                          # optional
)

# Unified response surface:
resp.text                  # first text block of assistant message
resp.content_blocks        # full Converse-shaped block list (use for tool_use)
resp.usage.input_tokens
resp.usage.output_tokens
resp.stop_reason           # canonical: end_turn | tool_use | max_tokens | stop_sequence
resp.tool_calls            # list of normalized tool calls (or None)
resp.raw                   # original backend response, for debug only
```

The Converse-shaped input is the canonical form because the existing code-base
already speaks it. The Responses backend translates inbound to OpenAI shape and
outbound back to Converse shape. The Converse backend is a near pass-through.

### 3. Translation surface (Converse ↔ Responses API)

GPT-5.5 / 5.4 / Codex are served by Bedrock Mantle's **Responses API** at
`/openai/v1/responses` — NOT the Chat Completions API. The Responses API
is OpenAI's newer agentic-workflow API; its request/response shape is
fundamentally different from Chat Completions (typed input items, typed
output items, named tool calls by `call_id`, separate `instructions`
field instead of a `role:"system"` message).

The Mantle endpoint also exposes `/openai/v1/chat/completions` for some
models, but **GPT-5.5's model card lists Chat Completions as NOT
supported** — Responses API is the only path. Hand-rolling the Responses
schema is non-trivial (the API is evolving as part of OpenAI's broader
"Responses-and-tools" direction), so we delegate transport + schema to
the **official `openai` Python SDK** pointed at the Mantle base URL via
`OPENAI_BASE_URL`.

The shared client's job is to translate between **our unified
Converse-flavored shape** and the **OpenAI SDK's Responses-API arguments**:

| Unified (Converse-flavored) | OpenAI SDK `client.responses.create(...)` | Translation note |
|---|---|---|
| `Request.system` (str or list[str]) | `instructions=` (str; concat list with `\n\n`) | rename + flatten |
| `Request.messages` (list of `{role, content:[{text}, {image}, ...]}`) | `input=` as a list of `{role, content}` items where content is a string or a list of typed items (`{type:"input_text",text}`, `{type:"input_image",image_url}`) | restructure |
| `messages[].content[].image.source.bytes` | typed item `{type:"input_image", image_url:"data:image/<fmt>;base64,..."}` | base64 → data URL |
| `Request.max_tokens` | `max_output_tokens=` | rename |
| `Request.temperature` | `temperature=` | direct |
| `Request.top_p` | `top_p=` | direct |
| `Request.stop_sequences` | (no direct equivalent — Responses-API does not expose `stop`; emit deprecation warning if set, ignore) | drop |
| `Request.tools` (`ToolSpec(name, description, input_schema)`) | `tools=[{type:"function", name, description, parameters:input_schema}]` | restructure (note: NOT nested under `.function.` like Chat Completions) |
| assistant `content[].toolUse` block (in multi-turn history) | input item `{type:"function_call", call_id, name, arguments:json.dumps(input)}` | restructure |
| user `content[].toolResult` block (in multi-turn history) | input item `{type:"function_call_output", call_id, output:str}` | restructure |
| (response) `output.message.content[].text` | (response) walk `response.output[]` for items where `type=="message"`, then `item.content[]` for `{type:"output_text",text}` | extract |
| (response) `usage.inputTokens` | (response) `usage.input_tokens` | rename |
| (response) `usage.outputTokens` | (response) `usage.output_tokens` | rename |
| (response) `stopReason: end_turn` | response `status: "completed"` AND no `function_call` items in output | enum map |
| (response) `stopReason: tool_use` | response output contains `{type:"function_call"}` items | structural |
| (response) `stopReason: max_tokens` | response `status: "incomplete"` AND `incomplete_details.reason == "max_output_tokens"` | enum map |
| (response) `stopReason: stop_sequence` | (no equivalent — Responses-API doesn't emit stop_sequences) | not applicable |

Each row becomes a unit-test fixture pinning the translation behavior in
both directions. Translation is the moat against silent semantic drift;
fixtures are the only durable way to keep it honest.

### 4. Auth + endpoint for the Responses backend

`bedrock-mantle.{region}.api.aws` supports BOTH SigV4 (IAM) and a
long-lived **Bedrock API key** (bearer token) per AWS docs. boto3 does
not (as of June 2026) ship a native `bedrock-mantle` service client.
Two implementation paths:

- **(a) Bedrock API key + OpenAI Python SDK.** AWS's published canonical
  path: generate a long-term API key in the Bedrock console, set
  `OPENAI_API_KEY` and `OPENAI_BASE_URL=https://bedrock-mantle.{region}.api.aws/openai/v1`,
  use `client.responses.create(...)` from the `openai` Python SDK. The
  SDK handles transport, SSE streaming, schema versioning, retries.
- **(b) SigV4-sign HTTPS directly** using `botocore.auth.SigV4Auth`,
  hand-rolling Responses-API JSON + SSE parser. Zero new dependencies,
  zero new secrets, but we own the entire wire-format implementation
  including forward-compatibility with Mantle's evolving Responses-API
  surface.

**Decision: path (a).** The OpenAI SDK is the AWS-blessed integration
path, hides the substantial complexity of the Responses-API schema
(which is still evolving as OpenAI iterates), and lets us track
upstream changes by bumping a pin rather than chasing schema drift.

**Compliance callout (load-bearing for regulated customers):**

The long-lived Bedrock API key is a design choice we inherit from AWS,
and it conflicts with typical regulated-customer secret-rotation policy
(30-90 day max lifetimes). We accept this trade-off because the
engineering cost of SigV4 + hand-rolled Responses-API is materially
higher than the friction of documenting the deviation for customer
SecOps. Per `[[project_bedrock_mantle_api_key]]`, every Mantle-enabled
deploy must:

- Provision the API key as a per-env SSM SecureString
  (`/<env>/outcome-ops-ai-assist/bedrock/mantle-api-key`) with terraform
  `lifecycle { ignore_changes = [value] }` after first apply (same
  pattern as the license key).
- Ship a customer-visible runbook (`docs/runbooks/bedrock-mantle-api-key.md`)
  covering generation, installation, rotation, and the
  compliance-disclosure language for SecOps sign-off.
- Allow regulated customers to opt OUT of Mantle entirely — leaving the
  SSM placeholder unset means GPT-5.5 / 5.4 / Codex are unavailable, and
  the deploy stays on Converse-only models (Claude, Llama, Nova,
  gpt-oss). The shared client's dispatcher raises
  `BackendConfigError` when a Responses model is requested but no API
  key is provisioned — loud, not silent.

### 5. Streaming is part of the unified shape, not a follow-on

The platform's chat UI streams tokens token-by-token, and this is
non-negotiable user-experience surface — the chat UI cannot degrade based
on which model a tfvars file points at. "Model-agnostic" must include
streaming or it isn't actually model-agnostic, just non-streaming-agnostic.

The unified client exposes BOTH `invoke()` (full response) and
`invoke_stream()` (generator of `StreamEvent` objects) from day 1. Both
backends implement both methods. The streaming translation surface is
materially different from non-streaming and earns its own per-translator
work in chunks 04 / 05 / 06, but it ships in the same foundation, not
later.

Concretely, streaming adds these unified events that BOTH backends must
emit:

| Event | Carries | Converse origin | Responses-API SDK origin |
|---|---|---|---|
| `TextDelta` | partial text | `contentBlockDelta.text` | `ResponseTextDeltaEvent.delta` |
| `ToolUseStart` | tool id + name | `contentBlockStart.toolUse` | `ResponseOutputItemAddedEvent` whose item is a `function_call` (carries `call_id` + `name`) |
| `ToolUseArgumentsDelta` | raw JSON fragment | `contentBlockDelta.toolUse.input` | `ResponseFunctionCallArgumentsDeltaEvent.delta` |
| `ToolUseStop` | parsed args dict | `contentBlockStop` | `ResponseFunctionCallArgumentsDoneEvent.arguments` (full JSON string) → json.loads |
| `Stop` | `StopReason` enum | `messageStop.stopReason` | `ResponseCompletedEvent` + inspection of `response.status` / `incomplete_details` |
| `Metadata` | `Usage` | `metadata.usage` | `ResponseCompletedEvent.response.usage` |

The OpenAI SDK handles SSE byte parsing + reassembly internally; the
shared client iterates SDK-emitted typed events and translates each into
a unified `StreamEvent`. The Responses-API SDK already accumulates
`function_call_arguments` deltas and emits a `Done` event with the full
arguments string — we do NOT need to hand-roll a per-tool-call
arguments accumulator; we just `json.loads` the SDK-provided
arguments at the `Done` event and emit `ToolUseStop`. This is a
significant simplification compared to the SigV4 path.

### 6. Pricing and telemetry stay where they are

`BEDROCK_ADVANCED_INPUT_PRICE`, `BEDROCK_ADVANCED_OUTPUT_PRICE`, etc., are read
by each Lambda's existing cost-attribution code. The shared client returns
`usage.input_tokens` / `usage.output_tokens` in the unified shape; cost
computation does not move and does not need to change. Pricing tfvars continue
to drive cost telemetry.

### 7. Loud failure modes

The shared client raises (not silently falls back) on:

- Unknown model-ID prefix (no implicit default).
- Tool-use request to a Responses model whose `tools` translation would lose
  schema fidelity (e.g., a tool schema feature only Converse supports).
- Mantle endpoint not configured for the region the Lambda runs in.
- Translation error from a malformed backend response.

Silent fallbacks across model families would erode the model-attribution claim
that audit (ADR-026) and structural attribution (ADR-031, ADR-033) depend on.

## Consequences

### Positive

- Single tfvars line continues to drive model selection across the
  Claude + Llama + Nova + gpt-oss + GPT-5 families.
- One place (`lambda/shared/model_client.py`) owns the divergence. Adding the
  next API family later (e.g., a new Bedrock-something endpoint) means adding
  one row to the dispatch table and one backend module, not editing 10+
  Lambdas.
- The cost-attribution code, the audit-write code, the retrieval/grounding
  code, and the response-handling code all remain untouched. Only the
  invocation switches.
- Customer procurement story: "model-of-record is a tfvars line, including
  GPT-5 frontier" remains true. Compliance customers (regulated industries
  per `[[project_regulated_data_stack]]` and `[[project_gtm_strategy]]`) can
  pick their model without the platform needing per-customer code branches.

### Negative

- Centralized client = centralized risk surface. A bug there breaks every
  LLM-calling Lambda at once. Mitigation: fixture-driven translation tests
  for every row in the table above, regression-tested on every chunk.
- Tool-use semantic differences between Anthropic-style and OpenAI-style
  tool use are real (parallel tool calls, content-block interleaving). The
  translator pins what it supports; what it can't translate it raises on,
  rather than approximating. Some tool patterns that work natively in
  Converse may need explicit re-design to work on Responses.
- Streaming tool-call accumulation is genuinely complex on the Responses
  side: arguments arrive as raw JSON fragments that may split mid-string,
  mid-number, or mid-escape. The accumulator must hold partial fragments
  per `tool_calls[].index` and only emit `ToolUseStop` once the terminal
  delta arrives. Bug surface area here is real; pin it with recorded SSE
  traces (chunks 02 and 03) and round-trip fixtures (chunk 06).
- Embeddings stay on `bedrock-runtime` `InvokeModel` + Titan. Embedding swap
  is an architectural problem (different vector dimensions, schema-incompat)
  not solvable via this dispatch. Out of scope.

### Out of scope

- **Embedding model swap.** Separate problem; separate ADR if/when urgent.
  Per `[[project_model_swappability]]`, the embedding swap is architectural
  not tfvars-tunable. Six `InvokeModel` call sites in the codebase
  (`process-documents`, `query-kb`, `mcp-server`, embeddings in
  `process-batch-summary` / `process-repo-summary` / `chat`) all hit Titan
  and stay where they are.
- **Bedrock Knowledge Bases** (`bedrock-agent-runtime`). Separate API,
  separate concern. `chat/handler.py:194` and `query-kb/handler.py:64`
  continue to use the agent-runtime client directly.
- **`BEDROCK_BASIC_MODEL_ID` vs `BEDROCK_ADVANCED_MODEL_ID` cost-band
  semantics.** The model-swappability contract already governs which tier
  invokes which env var; this ADR only changes _how_ the invocation reaches
  the underlying API.

## Verification

This ADR is "Accepted" once ALL of the following are demonstrated on live
infrastructure:

1. The shared module exists, dispatches correctly, and has 100% coverage of
   the translation table above by fixture-driven tests (both directions,
   both streaming and non-streaming).
2. **Non-streaming GPT-5.5:** `BEDROCK_ADVANCED_MODEL_ID = "openai.gpt-5-5:0"`
   round-trips through `process-repo-summary` end-to-end on a fresh repo
   connect in the openai env, with no `ValidationException`.
3. **Non-streaming Claude regression:**
   `BEDROCK_ADVANCED_MODEL_ID = "us.anthropic.claude-opus-4-7-20260514-v1:0"`
   (or whatever Claude is current in prd) continues to round-trip through
   `process-repo-summary` end-to-end in prd, with no regression.
4. **Streaming GPT-5.5 in the chat UI:** opening a chat in the openai
   workspace, asking a synthesis question, observing token-by-token
   render of the answer in the browser. Includes a tool-call mid-stream
   (e.g., `find_definitions`) round-tripping cleanly — the tool result
   appears in the streamed answer.
5. **Streaming Claude in the chat UI (regression):** same test on prd, no
   UX regression, identical streaming behavior to pre-migration.

All five are required. The chat UI cannot ship without streaming on both
backends — degraded chat UX on the GPT-5 path is not an acceptable
shipping state, because tfvars-driven swappability is the
product-positioning claim this ADR defends.

### Verification Trace (2026-07-01)

All five criteria demonstrated on live infrastructure:

1. **Shared module + fixture-driven tests:** `shared.model_client` +
   `_converse_backend` + `_responses_backend` shipped. `make test-unit`
   runs 6087 passing, 3 skipped, 0 failed. Translation table (Converse ↔
   Responses) covered both directions, both streaming and non-streaming.

2. **Non-streaming GPT-5.5 on openai:** `app.retrieveit.ai` connected
   fresh into the openai workspace (`WS#VS6QCfpkmI_oST5zqVhA6w`,
   us-east-2). `process-repo-summary` + `process-batch-summary` generated
   40 code-map units at SHA `c05485e393fa8581e936ac8b3b12a7cce633898c`
   with 0 failures. Handler-group summaries, tests summaries, docs
   summary, infrastructure summary, and architectural summary (2 chunks)
   all landed in `openai-outcome-ops-ai-assist-vectors` under the
   workspace prefix. Zero `ValidationException`.

3. **Non-streaming Claude regression on prd:** same repo, same SHA,
   generated 40 code-map units on prd (`WS#B1MgdLzM1j0YeyAqCWbyxg`,
   us-west-2). Identical ledger shape, 0 failures, 178 vectors landed
   under the workspace prefix.

4. **Streaming GPT-5.5 in openai chat UI (with tool call):** opened
   chat on `openai.outcomeops.ai`, asked "what are our lambda handler
   standards?" — GPT-5.5 streamed a grounded response quoting
   `outcomeops-adrs/docs/adr/ADR-004-lambda-handler-standards.md`
   verbatim, and cross-referenced the `audit-alert/handler.py`
   call chain via mid-stream code-map lookup. Ran the
   pre-migration-regression query "who calls require_auth?" — routed
   correctly to structural, graph returned 94 callers, response
   listed per-module breakdown with grounded line numbers, zero
   fabricated aggregate headers. Pre-migration ALB idle_timeout=60s
   killed silent reasoning streams; commit `0ee30cf` raised it to
   900s and streams now hold.

5. **Streaming Claude regression on prd:** same "who calls
   require_auth?" query returned a clean per-module breakdown
   with 94 callers, no fabrication, identical routing behavior to
   pre-migration. See earlier 2026-06-04 baseline trace in
   `[[project_model_client_live_verified]]`.

The 2026-06-04 require_auth aggregate-fabrication regression
(fabricated header "40 distinct Lambda handler modules ... 94 direct
call sites") does not recur on either backend as of 2026-07-01 after
the ADR-027 v1.2 (commit `2d180d1`) + ADR-032 principled prompt-rule
fixes (commits `9aebe19`, `fb31a24`, `6e3535a`, `c2ef76b`).

## References

- `[[project_model_swappability]]` — tfvars-driven model swap contract
- `[[project_regulated_data_stack]]` — regulated-industry buyer constraints
- `[[project_gtm_strategy]]` — lighthouse customer, AWS GAM motion
- `[[project_billing_model]]` — $2M enterprise-unlimited tier, no per-deploy
  segmentation
- ADR-031 — Line-citation grounding (the structural-attribution discipline
  this ADR's loud-failure stance protects)
- ADR-033 — Dual-channel retrieval (same)
- AWS announcement, 2026-06-01: OpenAI GPT-5.5, GPT-5.4, Codex GA on Bedrock
- AWS docs: Bedrock GPT-5.5 model card
  (https://docs.aws.amazon.com/bedrock/latest/userguide/model-card-openai-gpt-55.html)
- Live-verified failure, 2026-06-04: `process-repo-summary`
  `ValidationException` on `us.openai.gpt-5-5:0` via Converse, openai
  workspace
