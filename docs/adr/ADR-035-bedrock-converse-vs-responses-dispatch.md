# ADR-035: Bedrock Converse vs Responses API dispatch via unified model client

## Status: Proposed

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

- Boeing PoC has explicitly retired Anthropic Claude across all services. The
  openai-workspace deploy needs OpenAI frontier (not OSS) to be a credible
  alternative.
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

### 3. Translation surface (Converse ↔ Responses)

The Responses backend must translate request and response field-by-field. The
mapping is fixed and pinned by fixtures:

| Converse field | Responses field | Translation |
|---|---|---|
| top-level `system` | first `{role: "system"}` message | prepend |
| `messages[].content[].text` | `messages[].content` (str if only text) | flatten or block |
| `messages[].content[].image.source.bytes` | `content[{type:"image_url", image_url:{url:"data:..."}}]` | base64 → data URL |
| `inferenceConfig.maxTokens` | `max_tokens` | rename |
| `inferenceConfig.temperature` | `temperature` | direct |
| `inferenceConfig.topP` | `top_p` | rename |
| `inferenceConfig.stopSequences` | `stop` | rename |
| `toolConfig.tools[].toolSpec.name` | `tools[].function.name` | restructure |
| `toolConfig.tools[].toolSpec.description` | `tools[].function.description` | restructure |
| `toolConfig.tools[].toolSpec.inputSchema.json` | `tools[].function.parameters` | restructure |
| assistant `content[].toolUse` block | assistant `tool_calls[]` | restructure |
| user `content[].toolResult` block | follow-up `{role:"tool",tool_call_id,content}` | restructure |
| response `output.message.content[].text` | response `choices[0].message.content` | extract |
| response `usage.inputTokens` | response `usage.prompt_tokens` | rename |
| response `usage.outputTokens` | response `usage.completion_tokens` | rename |
| response `stopReason: end_turn` | response `finish_reason: stop` | enum map |
| response `stopReason: tool_use` | response `finish_reason: tool_calls` | enum map |
| response `stopReason: max_tokens` | response `finish_reason: length` | enum map |
| response `stopReason: stop_sequence` | response `finish_reason: stop` | enum map |

Each row in this table becomes a unit-test fixture pinning the translation
behavior in both directions. Translation is the moat against silent semantic
drift; fixtures are the only durable way to keep it honest.

### 4. Auth + endpoint for the Responses backend

`bedrock-mantle.{region}.api.aws` is an AWS-IAM-protected endpoint. boto3 does
not (as of June 2026) ship a `bedrock-mantle` service client. Two paths exist:

- **(a) SigV4-sign HTTPS directly** using `botocore.auth.SigV4Auth` with the
  Lambda's existing AWS credentials, posting JSON to the Mantle endpoint and
  parsing the JSON / SSE response. Zero new dependencies; full control.
- **(b) OpenAI Python SDK with custom `base_url` + AWS-IAM-derived bearer
  token** if AWS publishes a credential-broker that yields short-lived
  OpenAI-compatible tokens. No such broker exists as of 2026-06-04.

**Decision: path (a).** We sign HTTPS calls with SigV4. If boto3 later ships
native Mantle support, swap the internals of the Responses backend; the unified
shape doesn't change.

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

| Event | Carries | Converse origin | Responses origin |
|---|---|---|---|
| `TextDelta` | partial text | `contentBlockDelta.text` | `choices[0].delta.content` SSE |
| `ToolUseStart` | tool id + name | `contentBlockStart.toolUse` | first `delta.tool_calls[].id/.function.name` SSE |
| `ToolUseArgumentsDelta` | raw JSON fragment | `contentBlockDelta.toolUse.input` | `delta.tool_calls[].function.arguments` SSE |
| `ToolUseStop` | parsed args dict | `contentBlockStop` | terminal `tool_calls` delta + accumulator close |
| `Stop` | `StopReason` enum | `messageStop.stopReason` | `finish_reason` SSE |
| `Metadata` | `Usage` | `metadata.usage` | final `usage` SSE (Responses-specific timing) |

Responses-side accumulators: the Responses backend MUST accumulate
`tool_calls[index].function.arguments` partial strings until the
corresponding tool emits its final delta, then JSON-parse the accumulated
string into the `ToolUseStop.arguments` dict. Streaming tool-call
translation is therefore strictly more state-bearing than non-streaming
tool-call translation, but the unified event shape isolates that
complexity to the backend module.

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

## References

- `[[project_model_swappability]]` — tfvars-driven model swap contract
- `[[project_regulated_data_stack]]` — regulated-industry buyer constraints
- `[[project_gtm_strategy]]` — Boeing lighthouse, AWS GAM motion
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
