# ADR-059: Hybrid retrieval seam — symbol-graph facts fused into RAG below the ADR-023 seam

## Status: Accepted

## Context

The platform's structural-retrieval moat (ADR-024, ADR-028, ADR-033) rests on the
symbol graph: deterministic `callers` / `importers` / `implementers` /
`definitions` / `symbol_at_line` facts, citable at `file_path:line`. RAG retrieval
(dual-channel, ADR-033) retrieves authored summaries and code-maps as vectors; it
does **not** query the symbol graph.

A 2026-11-16 live validation surfaced the gap concretely. On the RAG surface,
structural questions are **phrasing-fragile**:

- query-kb's per-lane rerank protects a code-map slot (`code_maps=3/12`), but the
  caller then applies a flat, lane-blind `rerank_score >= RERANK_MIN_SCORE (0.15)`
  filter that **evicts** the protected code-map when the reranker scores it low.
  Whether it scores low is pure phrasing: proven in CloudWatch, "what calls X at
  what line" reranks the handler summary at median 0.073 (evicted, code-map lane
  emptied, docs-only answer) while "describe the handler" reranks it at 0.647
  (kept). Same KB, same summary, opposite outcome.
- The exact fact the RAG surface could not produce — the call site at
  `lambda/auth/handler.py:672` — lives in the symbol graph, which `find_callers`
  returned deterministically, immune to phrasing.

So the durable fix for structural questions is not rerank tuning; it is **fusing
symbol-graph facts into retrieval**. That fusion already existed in exactly one
surface and was missing from the others:

| Surface | RAG | Symbol graph | Fused? |
|---|---|---|---|
| chat | dual-channel, always | router-gated (`classify_intent` -> augmentation on structural/hybrid/unknown) | yes |
| query-kb (CLI, PR-check) | dual-channel, always | none | no — RAG-only |
| mcp-server | `search_knowledge_base` tool | separate graph tools | no — client picks |

Additionally, chat's hybrid was LLM-gated and degraded to `pattern` (RAG-only) on
any `classify_intent` Bedrock failure/throttle, so a structural question could
**silently** lose its graph augmentation at the worst moment.

## Decision

Make RAG + symbol-graph fusion a **shared-seam primitive** below the ADR-023
retrieval seam, inherited by every surface — the same move ADR-033 used to push
dual-channel below the seam.

1. **Extract the hybrid below the seam.** The router (`classify_intent`,
   `RouterDecision`) and the symbol-graph augmentation move from `lambda/chat`
   into `lambda/shared` (`shared.router`, `shared.hybrid_augment`,
   `shared.context_builder`). Chat becomes a thin caller, behavior-preserving. The
   shared augmentation's table names are injectable kwargs defaulting to env, so
   each surface wires its own.

2. **query-kb adopts it, opt-in.** A `hybrid` flag (default off) turns on the
   fusion. It is **opt-in, not a default**, because query-kb is fanned out by
   PR-check and an unconditional router (Haiku `classify_intent`) + DDB reads per
   call would multiply spend at the worst scale. PR-check enables it only on the
   structurally-sensitive checks (BREAKING_CHANGES consumer detection — see the
   known fragility of consumer detection, which this closes by grounding it on the
   graph instead of RAG token matching).

3. **Deterministic router backstop.** When the question names a symbol that
   resolves in the graph, the augmentation fires regardless of the LLM intent
   verdict (including the throttle-degrade `pattern` default). The backstop uses
   **DDB only** (`fuzzy_resolve_by_name`), never an extra Bedrock call, so the
   quota-safety the degrade exists for is preserved. A plain `pattern` question
   with no resolvable symbol stays RAG-only.

4. **Fused MCP tool.** A `search_hybrid` tool runs RAG + symbol-graph resolution
   and returns both in one response, with the ADR-058 grounding envelope on the
   graph portion, so a client that does not orchestrate two tool calls still gets
   the hybrid. The existing `search_knowledge_base` and graph tools are unchanged.

### What this ADR deliberately does NOT do

- **It does not change the rerank min-score filter.** The lane-blind
  `rerank_score >= RERANK_MIN_SCORE` gate that evicts protected code-maps is the
  proximate cause of the phrasing fragility on the RAG-only path. The graph fusion
  makes it moot for structural questions (the graph answer is deterministic and
  independent of rerank). If a lane-aware code-map floor is still wanted for
  narrative queries, it is a separate, smaller change that rides ADR-033, not here.
- **It does not touch symbol-graph ingestion/extraction.** The seam only READS the
  graph.
- **`generate-code` retrieval is out of scope** (separate path; trace later).

## Consequences

### Positive
- Structural questions are answered from the code graph, deterministically, on
  every surface — not from drifting docs or a phrasing-sensitive rerank.
- PR-check's structural checks (BREAKING_CHANGES) ground on graph facts, closing a
  known-fragile consumer-detection path.
- The hybrid is one shared primitive; chat, query-kb, and mcp-server stop
  diverging (ADR-023 intent).
- The backstop removes the silent RAG-only degrade for structural questions.

### Tradeoffs
- Hybrid augmentation costs a Haiku `classify_intent` + DDB reads per call. query-kb
  keeps it opt-in per check to bound that cost; the backstop is DDB-only so the
  throttle path stays quota-safe. This is a real cost decision, recorded here, not
  a pure win.
- The MCP fused tool returns more per call (RAG + graph); the ADR-058 token budget
  applies — the grounding envelope stays compact so it does not crowd out content.

## Related ADRs
- ADR-023: shared retrieval seam (why the hybrid can be pushed below chat).
- ADR-024: router intent taxonomy (verification questions are HYBRID).
- ADR-033: dual-channel retrieval (the RAG half of the fusion; same below-the-seam move).
- ADR-058: MCP grounding ceiling (the envelope the fused MCP tool carries).

## References

- `lambda/shared/router.py` — `classify_intent`, `RouterDecision` (moved from
  `lambda/chat/router.py`).
- `lambda/shared/hybrid_augment.py` — `augment_with_symbol_graph` (the shared
  entry; table names injectable, default to env), plus the deterministic
  `_extract_candidate_identifiers` / `_deterministic_backstop_symbol` backstop.
- `lambda/shared/context_builder.py` — `build_symbol_facts_block` /
  `build_symbol_definition_block` (moved from `lambda/chat/context_builder.py`).
- `lambda/chat/handler.py` — now a thin caller of the shared seam
  (behavior-preserving).
- `lambda/query-kb/handler.py` — the `hybrid` event flag (default off,
  cost-neutral; graph block injected after rerank so it bypasses the min-score gate).
- `lambda/process-pr-check/check_handlers/breaking_changes.py` — sets
  `hybrid: true` on its query-kb consumer-detection call (strengthens the plane-0
  RAG fallback; complements the ADR-054 plane-1 `symbol_graph_consumers` path).
- `lambda/mcp-server/handler.py` — the `search_hybrid` tool
  (`handle_search_hybrid`); ADR-058 grounding envelope on the `symbol_graph` section.
- `terraform/lambda-query.tf`, `terraform/iam_symbol_graph.tf` — `SYMBOL_GRAPH_TABLE`
  / `CODE_MAPS_TABLE` env + the shared symbol-graph reader IAM for query-kb.
  (query-kb is primary-only; no secondary module exists, so there is no HA mirror.)

## Known follow-up (out of scope)

`shared.router` hardcodes `shared.model_client.invoke(workload="chat", ...)`, so
`classify_intent` spend on the query-kb and MCP surfaces attributes to the "chat"
workload. Parameterizing the router workload for correct ADR-016 cost attribution
is a small follow-up, deliberately left out of these behavior-preserving changes.

## Version History
- v1.0 (2026-11-16): Initial. Records the hybrid-retrieval seam, the opt-in cost
  decision, the DDB-only determinism backstop, and the fused MCP tool; scopes out
  the rerank min-score floor and ingestion changes.
