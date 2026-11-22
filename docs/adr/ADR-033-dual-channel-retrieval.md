# ADR-033 — Dual-Channel Retrieval (Code-Maps Lane + Docs Lane)

## Status: Accepted

## Context

The OutcomeOps north star is "code-maps over docs": graph-grounded
structural truth (code-maps generated from extracted symbol-graph
facts per ADR-028) wins over LLM-faithful narrative on every
structural claim. ADR-025 captures the precedence rule
("code wins on disagreement"). The regulated-buyer moat —
auditability, governance, deterministic attribution — depends on it.

ADR-025 is a precedence rule. It can only fire when BOTH sources
are quotable in the synthesis context. The 2026-06-03 magic_link
audit broke the moat at the retrieval seam:

The chat answer attributed `_claim_magic_token_jti` (line 487) as
"called from `handle_verify` (line 544)". Line 544 actually lives
in `verify_magic_token` (def at line 531). The bug was traced to
a VERBATIM RAG match in `docs/SECURITY_OVERVIEW.md`, which contained
the wrong attribution (a stale doc, written before
`verify_magic_token` got extracted as the intermediary). The model
faithfully retrieved the doc.

ADR-025 should have caught the drift — code wins over docs. But
the 29 sources the chat retrieved had ZERO code-map for the auth
handler. The `handler-group-summary#auth` vector existed in the
store (verified). It just lost on cosine similarity to a doc-heavy
field of competitors for the same top-K. The richer the customer's
documentation set, the WORSE their code-map utilization. ADR-025
was unfireable because retrieval never gave it a code side to
weigh.

This is the moat violation. Docs drift; code-maps don't. If
retrieval prefers docs for the most common structural question
shape, the platform silently inherits the customer's documentation
drift — the exact failure mode regulated buyers signed up to avoid.

Vector taxonomy audit (2026-06-03, full index walk of 1631 vectors,
31 distinct `type` values) confirmed:

- The platform's structural summaries already carry `type` metadata
  at ingest time (`handler-group-summary`, `architectural-summary`,
  `code-map`, `csharp-project-summary`, `source-module-summary`,
  `tests-summary`, `infrastructure-summary`, etc. — 19 distinct
  code-map types, 223 vectors total).
- Authored prose carries doc-shaped types (`adr`, `markdown`, `html`,
  `pdf`, `yaml`, `pptx`, `docx` — 7 types, 928 vectors).
- The single-channel retrieval architecture forces these two
  populations to compete for the same top-K slots. With a 4:1 docs-
  to-code-maps ratio in the index, doc-heavy questions
  systematically starve code-maps.

The fix is at the retrieval layer, not the ranker.

## Decision

Every chat synthesis turn runs **two parallel vector retrievals**:

- **Code-maps lane.** Filters `type` metadata to the code-map
  taxonomy (`CODE_MAP_TYPES`: 18 type values listed in
  `lambda/shared/retrieval.py`). Independent top-K budget.
- **Docs lane.** Filters `type` to the doc taxonomy (`DOC_TYPES`:
  12 type values, including `docs-summary`). Independent top-K budget.

Both lanes always fire, regardless of question intent. The two
result sets combine into the rerank candidate pool. Each lane has
its own budget so neither starves the other — ADR-025 then fires
naturally because both sources are reliably in context on every
turn.

Two structured invariants:

1. **Code-maps presence guarantee.** Every chat turn that
   retrieves docs for a workspace with code-maps available also
   queries the code-maps lane. Empty code-maps lane is allowed
   only when the workspace genuinely has no code-maps (thin-ingest
   case).

2. **Structural attribution requires code-map evidence.** When the
   synthesis is about to state a structural fact (`X is called from
   Y at line N`, `X is defined at file:line`, `X uses Y`, `the call
   chain A → B → C`), the claim must be grounded in a code-map /
   graph fact present in the context, NOT in a doc chunk alone. If
   only a doc carries the attribution and no code-map cross-asserts
   it, the synthesis must either qualify ("doc-derived; not code-map
   verified") or drop the attribution. Captured in the
   `STRUCTURAL_ATTRIBUTION_RULES` prompt block (`lambda/shared/prompts.py`).

### Code outranks docs in the final selection (v1.1)

Giving each lane a fixed half of the slots keeps code-maps in context,
but it does not rank them above docs. Code is the source of truth for
what the system does (ADR-025), so the ranking says so too:

1. **Each lane is scored against the question on one scale.** With
   rerank on, both lanes are reranked by the same model against the
   same query, keeping up to top-K each. With rerank off (or when one
   lane's rerank call falls back), every hit is scored by cosine
   similarity instead; the two scales are never mixed.
2. **Code-map scores are multiplied by a boost** (`code_map_score_boost`,
   default 1.5, must be >= 1.0) before the lanes compete for slots.
   A code-map wins any slot where its score is at least two thirds of
   the doc's. A much more relevant doc still wins.
3. **Each lane keeps a floor** (`retrieval_lane_floor`, default 0.25,
   range 0-0.5) of the slots when it has candidates, so the boost
   cannot shut docs out and a strong doc field cannot shut code out.
   The remaining slots go to the highest boosted scores from either
   lane.
4. **The context is ordered by boosted score** (code-map first on a
   tie), so "earlier documents are stronger matches" in the synthesis
   prompt also means code before docs.
5. **A summary of documentation is documentation.** `docs-summary`
   ranks in the docs lane and never receives the boost; it carries the
   docs' claims, not the code's.
6. **Identical chunks count once.** The same repo ingested into several
   workspaces (or under several SCM remotes) produces identical chunks
   under different keys; duplicates by `content_hash` are dropped
   before ranking so copies do not consume slots.

The boost and floor are tfvars, the same on every retrieval surface
(chat, query-kb, MCP server) and on the secondary region.

The boost ranks what retrieval returned; it does not add exact values.
Code-maps omit literals by design (ADR-027), so a question that needs
an exact value (an error code, a response field) still depends on
`read_source` (ADR-030), which the chat surface does not have yet.

## Consequences

- The synthesis layer no longer silently inherits doc drift on
  structural questions. Code-maps are always in context for
  workspaces that have them; ADR-025 can fire on disagreement;
  doc-only structural claims get explicit "unverified" qualifiers.
- Retrieval cost rises by roughly 2x (two parallel queries instead
  of one). S3 Vectors charges per query; for a workspace with
  modest traffic the absolute cost is negligible. Per-lane
  CloudWatch metrics (`[chat.retrieval] code_map_lane=N
  docs_lane=M combined=K`) help size budgets.
- Rerank handles surplus. When the question is purely conceptual
  (governance, policy, compliance prose), the code-map lane returns
  low-relevance results that lose slots in the rerank pass. The
  two-lane architecture HARDWIRES OPPORTUNITY, not inclusion.
  Rerank decides who keeps slots.
- Thin-ingest workspaces (no code-maps yet, only docs) degrade
  gracefully — the code-maps lane returns empty, docs lane carries
  the whole answer, and the structural-attribution rule scopes to
  "code-maps available" via the explicit disclosure clause so
  thin-ingest customers are not over-restricted.
- The architecture is INTENT-AGNOSTIC. We do not classify questions
  as "structural" vs. "conceptual" first. The router's symbol-
  extraction classifier was already brittle (the magic_link
  question mis-extracted the symbol; the auth question got a
  generic bare name); we do not want to add a second classifier on
  top.

This ADR is additive to:

- **ADR-024** — router-vs-synthesis split.
- **ADR-025** — code wins over docs on disagreement.
- **ADR-027** — code-map omits literals.
- **ADR-028** — graph-first code-map generation.
- **ADR-030** — read_source adjudicator.
- **ADR-031** — find_symbol_at_line + line-citation grounding.
- **ADR-032** — list-completeness contract.
- **ADR-033 (this)** — dual-channel retrieval + structural-
  attribution requires code-map evidence.

ADR-033 **deploys ADR-025 into the retrieval layer.** ADR-025 is
the precedence RULE; ADR-033 is the INFRASTRUCTURE that lets the
rule actually fire on every turn. Without ADR-033, ADR-025 was
theoretical — retrieval could starve the code side of the rule
indefinitely. With ADR-033, both sides reach context on every
turn (when both exist), and the rule has something to enforce
against.

## Alternatives Considered

- **Intent-based code-map promotion (rejected).** Classify
  questions as "structural" vs. "conceptual" at the router; force-
  include the matching handler-group-summary when structural.
  Rejected because (a) the router's symbol-extraction classifier
  was already brittle (mis-extracted `magic_link` as a symbol;
  failed to extract `auth` cleanly); (b) we do not want to add a
  second brittle classifier on top; (c) the dual-channel
  architecture is intent-agnostic and subsumes the use case
  without requiring intent labels.
- **Rerank-side weighting INSTEAD of lanes (rejected).** Boost
  code-map results in the rerank pass as the only mechanism.
  Rejected because rerank operates on candidates the retrieval
  pulled. If retrieval starves code-maps, rerank has nothing to
  boost. The fix has to be upstream of rerank. v1.1 adds a boost ON
  TOP of the lanes: the lanes guarantee candidates, the boost ranks
  them.
- **Weighted fixed split (considered for v1.1).** Give code a fixed
  larger share of slots (say 65%) instead of a score boost. Simpler,
  but it ignores relevance: a weak code-map would keep a slot over a
  highly relevant doc. The boost plus per-lane floor keeps the
  guarantee and still lets relevance decide.
- **Pagination cursors / "fetch more code-maps" tool (rejected).**
  Give the LLM a tool to fetch additional code-maps when it sees a
  structural question. Rejected because (a) chat synthesis does
  not currently have access to native outcomeops MCP tools (only
  user-configured external MCPs); (b) adding tool-use plumbing is
  much larger surface than dual-channel retrieval; (c) the
  retrieval-layer fix is principled and one-shot.
- **Wait for the customer to fix their docs (rejected).** The
  customer absolutely needs to fix `SECURITY_OVERVIEW.md` (the
  one-line edit). But the platform fix prevents the next 1000
  customers' stale docs from silently corrupting answers. Docs
  drift; code-maps don't. The platform must hedge against drift.

## Implementation Pointer

Vector ingest already writes a `type` field on every vector at
`PutVectors` time — audited 2026-06-03 across the prd
`prd-outcome-ops-ai-assist-vectors / knowledge-base` index
(1631 vectors, 31 distinct types). No re-ingest required.

Chat handler (`lambda/chat/handler.py`) runs two parallel S3
Vectors queries via `search_kb_dual_channel`, with type filters
drawn from `CODE_MAP_TYPES` and `DOC_TYPES` module-level taxonomies.
Both lanes feed into the existing rerank candidate pool. The
chat synthesis prompt (`lambda/shared/prompts.py`) carries
`STRUCTURAL_ATTRIBUTION_RULES` binding the model to ADR-033's
structural-attribution invariant. Per-lane CloudWatch telemetry
makes recall regressions visible without re-instrumenting.

The taxonomy constants in `lambda/shared/retrieval.py` are the single
source of truth for which `type` values belong to which lane.
Adding a new structural summary type (a future Java handler-group
summary, say) requires editing only the `CODE_MAP_TYPES` tuple;
the dual-channel mechanism picks it up automatically on the next
deploy.

The lane taxonomies, the boosted merge (`select_with_code_map_boost`,
`merge_dual_channel`) and the duplicate filter (`dedupe_by_content_hash`)
live in `lambda/shared/retrieval.py`, shared by chat, query-kb and the
MCP server.

## Version History

- v1.0: two lanes with independent budgets; per-lane rerank.
- v1.1 (2026-11-22): code-map score boost and per-lane floor in the
  final selection; context ordered by boosted score; `docs-summary`
  moved to the docs lane; duplicate chunks dropped by `content_hash`.
