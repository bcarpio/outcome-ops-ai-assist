# ADR-028: Graph-first code-map generation

## Status: Accepted

## Context

ADR-027 stopped code-maps from freezing wrong **literals** by omitting
them. But code-maps also state **structural** claims — which functions
exist, what calls what, where a symbol is defined — and those were still
recalled by the summarizer LLM from reading source, so it could invent a
function, hallucinate a cross-reference, or attribute a call to the wrong
function (observed: "called from `handle_verify` line 544" when 544 is in
`verify_magic_token`).

The symbol-graph extractor (`process-batch-summary/extractor`) already
runs per file **before** the map prompt is built, emitting parser-extracted
`Fact`s (`defines` / `calls` / `imports` / `implements` with `file:line`).
That is ground truth for structure — it was being persisted to the graph
but not used to ground the summary generated in the same pass.

## Decision

Generate code-maps **graph-first**: feed the batch's extracted facts into
the map prompt as a "verified symbols" skeleton with a *use-only-these*
constraint, so the narrative is built on the parser's structure rather
than the LLM's recollection of it.

### Key Points

- **Reuse the existing pre-pass.** `generate_batch_summary` already
  extracts facts into a sink before building the prompt. We accumulate
  this batch's facts and render them; no new extraction pass.
- **Verified-symbols block** (`_render_graph_grounding`): lists symbols
  **defined in this group** (`name (file:line)`) and **outbound**
  references (calls/imports/implements to symbols not defined here),
  bounded (`120` defines / `60` refs per kind) so it anchors without
  bloating the prompt. Placed first in the prompt; the source files are
  demoted to "narrative and behavior only."
- **Constraint:** "Do NOT introduce a function, class, call, import, or
  cross-reference not listed here; if tempted to, omit it." This kills
  invented symbols and hallucinated cross-references, and the `file:line`
  kills wrong-call-attribution.
- **Backward-compatible / fail-soft:** grounding is empty when no facts
  were extracted (fact_sink `None` — legacy/test paths), leaving the
  prompt unchanged. An extractor hiccup never blocks the summary.
- **Layers cleanly with ADR-027:** graph grounds *structure*; ADR-027
  keeps *literals* out; the (future) read-from-source adjudicator supplies
  exact *values*.

## Consequences

### Positive

- Structural claims become graph-verified, not LLM-recalled — closes the
  invented-symbol / hallucinated-cross-ref / wrong-line-attribution class.
- The map's `file:line` references are now accurate, which also sharpens
  the future read-from-source adjudicator's locator.
- Reduces map/graph drift: the map is a narrative *view* of the graph.

### Tradeoffs

- The grounding block adds prompt tokens (bounded). Negligible vs the
  source files already in the prompt.
- Signatures (param names/types) are still LLM-read — the graph doesn't
  carry them yet. That's the deferred signature-extraction work (see
  `plan/`), which would let graph-first ground signatures too.
- Sub-batches (large-group overflow) get the full group's grounding even
  when summarizing a file subset; acceptable, and the merge recombines.

## Implementation

- `_render_graph_grounding(facts)` + `_build_batch_prompt(..., graph_grounding)`
  in `process-batch-summary`; `generate_batch_summary` accumulates
  `batch_facts` and threads grounding into the main and sub-batch prompts.
- Tests: grounding lists defined symbols with file:line, surfaces outbound
  refs only, omits in-group targets; injection precedes the literal
  discipline; no-grounding path unchanged.

### Next phases

1. **Regen code-maps** (e.g. app.retrieveit.ai) to observe the combined
   ADR-027 + graph-first effect.
2. **Signature extraction** (`plan/`) so graph-first also grounds signatures.
3. **Read-from-source adjudicator** for exact literal values on demand.

## Related ADRs

- ADR-027: code-map literal discipline (omit literals).
- ADR-021: symbol-graph GSI naming; ADR-025: synthesis guardrails.

## References

- `lambda/process-batch-summary/handler.py`
  (`_render_graph_grounding`, `_build_batch_prompt`, `generate_batch_summary`)
- `lambda/tests/unit/test_process_batch_summary.py`

Version History:

- v1.0 (2026-05-21): Initial decision — ground code-map generation on the
  parser-extracted symbol graph.

<!-- Confluence sync -->
