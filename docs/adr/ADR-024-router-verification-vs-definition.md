# ADR-024: Router taxonomy — verification questions are HYBRID, not PATTERN

## Status: Accepted

## Context

The chat router (`lambda/chat/router.py`) classifies each question into
one of five intents (`pattern | dependency | structural | hybrid |
unknown`) to decide which retrieval surfaces fire. The chat handler then
gates the symbol graph on intent: `fires_graph = intent in ("structural",
"hybrid", "unknown")` — PATTERN and DEPENDENCY skip the graph.

The v1 router prompt lumped three different question shapes under
PATTERN: *concepts/architecture*, *standards/compliance*, and *gap
analysis/ADR synthesis*. That conflated two shapes that need different
retrieval:

| Shape | Example | Right surface |
|---|---|---|
| **Definition** | "What are our Python standards?" | RAG only |
| **Verification** | "Is the magic_link flow secure?" / "Does X meet standard Y?" | RAG (the standard) + Graph (the implementation) |

Verification/audit/gap-analysis questions classified as PATTERN, so the
graph was skipped — the model answered from prose chunks alone. This is
the exact shape the platform's structural-retrieval moat
(`project_moat_audit`) is supposed to win, and it was getting the worst
retrieval mix the platform offers. Observed live: "Is the magic_link
system secure?" → `pattern`, graph skipped, model cited a stale doc's
"15 minutes" instead of the code's `MAGIC_LINK_EXPIRY = 3600` and missed
the JTI replay protection entirely.

## Decision

Split verification out of PATTERN in the router prompt.

### Key Points

- **PATTERN narrows** to *define / explain / summarize* a concept,
  convention, architecture, standard, or ADR — answerable from
  documentation alone.
- **HYBRID gains an explicit verification/audit/gap-analysis category**:
  any question that compares an implementation against a standard, ADR,
  or industry practice is HYBRID, because answering it requires reading
  both the standard (RAG) and the actual implementation (graph). HYBRID
  already fires the graph, so no handler change is needed — the fix is
  entirely in the prompt.
- **Symbol extraction guidance**: verification questions usually name a
  subsystem (`magic_link`, `auth_flow`, `audit_logging`) rather than a
  single function — the prompt instructs the model to extract it.
- **Versioned + flag-gated**: the prompt is selected by
  `CHAT_ROUTER_PROMPT_VERSION` (default `v2`, set `v1` for instant
  rollback without a redeploy if HYBRID over-fires). Read at call time.
- **Defense-in-depth (ADR companion, chunk 02)**: a graph-fire backstop
  fires the graph whenever an extracted symbol resolves to a definition,
  regardless of intent — so a router misclassification still yields the
  graph answer.

## Consequences

### Positive

- Verification/audit questions now exercise structural retrieval — the
  moat's strongest demo shape works.
- No handler code change for the routing fix itself (HYBRID already
  fires the graph); the prompt is the whole change.
- Instant rollback via the version flag.

### Tradeoffs

- HYBRID may over-fire on borderline prose questions, adding a
  symbol-graph lookup per turn. Mitigated by the rollback flag and by
  skip-reason telemetry (shipped first) so the intent-distribution shift
  is measurable.
- LLM classification is never perfect; the backstop (chunk 02) covers
  residual misclassification.

## Implementation

### Starting today

1. `_ROUTER_SYSTEM_PROMPT_V2` is the default; `_ROUTER_SYSTEM_PROMPT_V1`
   retained for rollback. `_router_system_prompt()` selects by env.
2. Router unit tests cover the selector + that `classify_intent` sends
   the selected prompt to Bedrock.

### Next phases

1. Live eval of classification quality on a verification-question set
   (cannot be unit-tested with a stubbed model).
2. If HYBRID over-fires, add a confidence floor or tighten the prompt;
   the flag buys time to measure first.

## Related ADRs

- ADR-023: KB retrieval contract (the RAG side of the HYBRID answer).

## References

- `lambda/chat/router.py` (`_ROUTER_SYSTEM_PROMPT_V1/V2`,
  `_router_system_prompt`)
- `lambda/tests/unit/test_chat_router.py`
- `lambda/chat/handler.py` (`fires_graph` gate + graph-fire backstop)

Version History:

- v1.0 (2026-05-21): Initial decision — verification/audit/gap-analysis
  classifies HYBRID; PATTERN narrowed to definition/explanation.

<!-- Confluence sync -->
