# ADR-027: Code-maps omit literals — narrative + structure only

## Status: Accepted

## Context

Code-maps are LLM-generated NARRATIVE + STRUCTURE summaries of source
(produced by `process-batch-summary` per handler/module group and
`process-repo-summary` for the architectural overview). They are *not*
AST extractions — a model reads the source and writes prose.

Live dogfooding found a code-map asserting **fabricated literals** at the
*current* source SHA (so not staleness):

- the magic-link `jti` summarized as `secrets.token_urlsafe(32)` — source
  is `(16)` (a regression to the dominant value: `(32)` appears 11× in
  the repo for OAuth-class secrets, the `jti` is the lone `(16)`).
- the redemption key invented as `USED_TOKEN#` — source is `MAGIC_TOKEN#`;
  `USED_TOKEN#` exists nowhere in the codebase.

Both are the same failure class: a summarizer LLM, asked to "extract
parameter format specifications / PK-SK keys / data formats," **paraphrases
a plausible-sounding literal instead of transcribing the real one**, and
the wrong value is then frozen into the artifact and retrieved into every
downstream answer. The `USED_TOKEN#` case is the dangerous one — no doc
contradicted it, so a discrepancy-triggered check would never catch it;
it ships silently.

This matters because the platform's stance is "code wins over stale docs"
(`project_moat_audit`) — but that only holds if "code" means *verified
code*, not a summary that sounds like code.

## Decision

A code-map summary MUST NOT transcribe specific literal values or exact
string constants. Append a stack-agnostic **literal-omission discipline**
to every code-map generation prompt, and lower generation temperature.

### Key Points

- **Omit, don't quote.** Asking the model to "quote literals verbatim"
  still leaves it deciding when it's confident — and it is confidently
  wrong. Defaulting literals/identifiers to *omit* removes the entire
  frozen-hallucination class. The exact value is read from source on
  demand (via the symbol-graph-located read path; see ADR-025 followup).
- **Stack-agnostic by category, not example.** The discipline names
  categories that hold across Python, Java, TypeScript, ABAP/SAP,
  SQL/PL-SQL, Go, C#: numeric literals (sizes, timeouts, limits, ports,
  versions, magic numbers) and exact string constants/identifiers
  (datastore object names — tables/columns/collections/keys/indexes —
  config/secret paths, URLs/endpoints, queue/topic names, regexes,
  hardcoded config, transaction/program/module codes). No AWS-specific
  nouns.
- **Keep structural facts.** Function/method/procedure/module names and
  parameter names+types, class/interface/struct relationships, *that* a
  unit reads/writes a datastore or calls an external system (without the
  exact identifier), outcome categories (success vs error, no exact
  codes), control flow. These are language-agnostic and are
  independently corroborated by the symbol graph.
- **Never invent.** If an exact value matters, write "(see source)" —
  never fill a gap with a plausible constant or key name.
- **Lower generation temperature** 0.3 → 0.1 in both summarizers — cuts
  the value/identifier hallucination at the source.
- **The discipline overrides** the existing prompt bodies that ask for
  "format specifications / keys / field types" (it's appended last with
  an explicit override line).

## Consequences

### Positive

- Removes the frozen-wrong-literal class from code-maps — the
  highest-leverage fix, since a bad artifact poisons every retrieval.
- Gates the regen: regenerating *after* this lands produces clean maps;
  before it, regen could re-hallucinate.
- Stack-agnostic, so it holds for any F500 codebase the platform ingests.

### Tradeoffs

- Code-maps lose exact literals/keys — by design. Consumers needing an
  exact value use the read-from-source path, not the summary. The
  symbol graph still carries structural facts.
- The model may occasionally omit a value that was actually safe to
  state; acceptable — omission is recoverable, a frozen wrong value is
  not.

## Implementation

### Starting today

1. `CODE_MAP_LITERAL_DISCIPLINE` constant in `shared/prompts.py`.
2. Appended to every batch prompt (`process-batch-summary._build_batch_prompt`)
   and the architectural summary (`process-repo-summary`).
3. Generation temperature lowered 0.3 → 0.1 in both.
4. Tests: discipline content + stack-agnostic + structural-facts-kept;
   builder appends it for known and fallback batch types.

### Next phases

1. **Regen code-maps** (unblocked by this) — produces clean artifacts.
2. **Read-from-source adjudicator** — the durable guarantee that "code
   wins" is backed by the actual source line (symbol graph locates the
   symbol → a `read_source(file, line_range)` tool reads the real value),
   triggered on *any* specific code claim, not only on doc/map
   discrepancy. (USED_TOKEN# proved the silent, uncontradicted case is
   real.) Mind SHA-skew: read a window and locate the construct, since
   graph line numbers are pinned to the graph SHA while the read fetches
   HEAD.

## Related ADRs

- ADR-025: Synthesis guardrails — the chat side that consumes code-maps;
  its "code over docs" rule is only safe once code-maps stop carrying
  fabricated literals and the read-from-source adjudicator exists.
- ADR-024 / ADR-021: router + symbol-graph (the locator for the read).

## References

- `lambda/shared/prompts.py` (`CODE_MAP_LITERAL_DISCIPLINE`)
- `lambda/process-batch-summary/handler.py` (`_build_batch_prompt`)
- `lambda/process-repo-summary/handler.py` (architectural summary)
- `lambda/tests/unit/test_shared_prompts.py`,
  `lambda/tests/unit/test_process_batch_summary.py`

## Post-regen observation (2026-05-21)

Regenerating app.retrieveit.ai with v1.0 + graph-first (ADR-028) fixed the
worst cases: the `token_urlsafe(32)` size literal is gone (jti now "a
unique token ID (random)"), file:line is accurate (514/531/487), and the
wrong-call attribution is fixed. One residual: the model emitted a
**caveated placeholder** key `JTI#{jti}` (real is `MAGIC_TOKEN#`) with a
"(see source)" note — confidently-wrong became caveated-but-still-wrong.
Tightened the discipline (v1.1) to forbid placeholder/templated/example
identifiers entirely ("a guessed shape is still a wrong literal"). The
durable fix for the exact value remains the read-from-source adjudicator
(next phases).

Version History:

- v1.0 (2026-05-21): Initial decision — omit literals/identifiers from
  code-map summaries (stack-agnostic), lower generation temperature.
- v1.1 (2026-05-21): Forbid placeholder/templated/example identifiers
  (not just exact values) after a regen showed a caveated placeholder key
  leaking. Describe by role + "(see source)".

<!-- Confluence sync -->
