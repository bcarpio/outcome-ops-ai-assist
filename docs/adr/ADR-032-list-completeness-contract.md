# ADR-032 — List-Completeness Contract

## Status: Accepted

## Context

The platform's chat synthesis layer answers enumeration questions
("which Lambdas call require_auth", "which services import
@hyatt/hpe-utils", "which classes implement IBookingClient") by
issuing list-returning tool calls and walking the responses. ADR-024
split chat questions into router-resolvable and synthesis;
ADR-025/027/028 hardened narrative answers against fabrication;
ADR-030 added read_source for value verification; ADR-031 added
find_symbol_at_line for enclosing-symbol grounding plus an
anti-aggregate sub-rule forbidding LLM-computed counts.

A failure mode they do not close: **silent list truncation.** A
list-returning tool whose response carries no completeness signal
can return a clipped set that the LLM consumes as if it were whole.
The 2026-06-02 live eval against a customer workspace exposed this.
Chat router called `fuzzy_resolve_by_name` without an explicit
`limit`, the reader's default 25 clipped the canonical list at
alphabetical position 25, and 8 of 24 lambdas with vendored
`require_auth` definitions never reached `find_callers`. The user-
facing answer: 16 lambdas, 37 enumerated sites, a fabricated headline
of "65 call sites" — correct on every specific line cited, fully wrong
on the aggregate. None of the existing ADRs caught this because the
failure isn't about WHAT was cited; it's about what SHOULD have been
cited but never made it into the LLM's context.

The data was present in the graph (all 40 canonicals existed; the
single caller at `org-management/handler.py:5830` in a 6,000-line
file was queryable). The bug was in the call site's contract with
the reader and the LLM's contract with the response shape.

The product is GA on AWS Marketplace as of 2026-05-30. Every
customer running an enumeration question against a workspace with
more name collisions than the reader's defaults silently gets a
clipped answer today. This is the correctness failure mode regulated
buyers screenshot during an audit.

## Decision

Every list-returning tool response must carry two structured fields:

- **`complete: bool`** — true when the returned list contains every
  matching item known to the workspace at the time of the call;
  false when the list was clipped by a cap (the caller's explicit
  `limit`, a reader's default, a DynamoDB pagination boundary,
  etc.).
- **`total_known: int | null`** — the deterministic total count
  when the tool can compute it cheaply (typically when
  `complete: true` the field equals `len(items)`; when
  `complete: false` the field is either the exact count from a
  separate counting query or null if the tool cannot compute it
  without paginating to completion).

Chat synthesis must honor these fields:

- `complete: true`: the list is exhaustive. Counts in the answer
  must quote `total_known` verbatim. Aggregates the LLM computes
  from the enumeration remain forbidden (ADR-031 item 6).
- `complete: false`: the list is partial. The answer must either:
  1. call the tool again with a higher `limit` to obtain
     completeness;
  2. **disclose** the truncation explicitly ("showing N of K" when
     `total_known` is known, or "showing N of at least K" when
     `total_known` is null) AND surface an escalation pattern to
     the user; or
  3. **refuse** the enumeration question and explain the answer
     would be partial.
- Quoting `len(items)` from a partial result, estimating a count
  from a partial enumeration, or omitting the limitation silently
  are all forbidden.

The chat synthesis prompt enforces the contract via
`LIST_COMPLETENESS_RULES` in `lambda/shared/prompts.py`, composed
into `build_chat_system_prompt` after the line-citation block.
List-returning MCP tool envelopes carry the two fields. The chat
router passes explicit limits at every consumer site identified by
the audit. When a router-side limit is hit (a workspace whose
canonical set exceeds the router's cap), the augment block prepends
an explicit `NOTE: the bare-name lookup for X was clipped at N
canonicals...` line so the LLM context carries the disclosure
literally — the prompt rule binds it to honor that note rather than
strip it.

## Consequences

- Every list-returning tool's response envelope grows two additive
  fields. Old MCP clients ignore them; the LLM consuming the schema
  sees them and the prompt rule binds it to honor them.
- Reader functions return wrapped shapes (`QueryResult` for
  `CallSite` lists, `ResolutionResult` for canonical-id lists) so
  the truncation signal travels alongside the items. Consumers that
  previously read `len(returned_list)` as a count must read
  `total_known`, or refuse the count when it's null.
- The "showing N of K" disclosure pattern becomes a known shape in
  chat answers. Customers see it occasionally on huge workspaces
  and have a clear escalation path ("show all" or raise the limit).
- Confidently-incomplete enumerations stop shipping. The product
  becomes safer to cite from downstream (audits, change-management
  systems, security reviews) because every quantitative claim
  traces to a structured count field on a tool response.
- The fix scope is bounded to call sites with enumeration semantics.
  Sample-shaped consumers ("show a few example callers in a
  conversational answer") keep low limits and document the choice;
  the rule doesn't force every read to fetch the world.
- Reader signatures for `definitions` and `fuzzy_resolve_by_name`
  changed from bare lists to wrapped result types. In-tree consumers
  were updated together; the change is breaking for downstream
  callers outside the repo but those don't exist for these internal
  readers.

This ADR is additive to:

- **ADR-024** — router-vs-synthesis split.
- **ADR-025** — synthesis guardrails (no fabrication of behavior).
- **ADR-027** — code-map omits literals (no value fabrication via
  summary recall).
- **ADR-028** — graph-first code-map generation.
- **ADR-030** — read_source adjudicator (verify values AT a line).
- **ADR-031** — find_symbol_at_line (verify SYMBOL containing a
  line) + anti-aggregate sub-rule (no LLM-computed counts).
- **ADR-032 (this)** — list-completeness signal + surface-or-refuse
  on truncation.

Together they form a closed loop: every cited value, every cited
location, every cited symbol, and every cited count is graph- or
source-resolvable to a tool field — never to LLM memory or LLM
arithmetic.

## Alternatives Considered

- **Raise every reader's default `limit` to a "safe" large number.**
  Rejected. The reader default is right for general MCP
  disambiguation use cases (an LLM picking among a small canonical
  set doesn't want hundreds); raising the default just postpones
  the failure to a workspace big enough to exceed whatever new
  default we pick. The structured signal solves the whole class.
- **Pagination tokens (cursor-based) on the tool envelope.** More
  complex; pushes pagination state into the LLM's working memory.
  The `(complete, total_known)` signal is sufficient: if the LLM
  needs more, it calls the tool again with a higher `limit`. v2
  may add cursors if a workspace appears where 200+ enumeration is
  routine.
- **Per-tool refuse-or-extend semantics chosen by the tool author,
  not the prompt.** Rejected. The prompt rule needs to be uniform
  across tools or the LLM has to remember per-tool conventions —
  which is exactly the kind of thing models silently get wrong.
- **Wait for the user to ask "show all".** Rejected as a primary
  mechanism. The failure mode is the user NOT KNOWING the answer
  was partial. The disclosure has to be load-bearing on the
  truncation signal, not on the user's ability to ask the right
  follow-up.
- **Drop the augment-block NOTE; rely only on tool-side `complete`
  fields.** Rejected. The chat router does multiple tool calls
  internally (canonical resolution then relation fan-out) and the
  intermediate truncation needs to reach the LLM's context. A
  literal NOTE on the symbol facts block is the cheapest reliable
  signal.

## Implementation Pointer

Reader functions in `lambda/shared/symbol_graph_reader.py` carry
`complete` (derived property, inverse of `truncated`) and
`total_known` on `QueryResult` and `ResolutionResult`. MCP tool
handlers in `lambda/mcp-server/handler.py` forward both fields via
`_query_result_to_dict` and `_resolution_result_to_dict`. Chat
synthesis prompt rule (`LIST_COMPLETENESS_RULES`) is composed into
`build_chat_system_prompt` in `lambda/shared/prompts.py`. Chat
router (`lambda/chat/handler.py`) passes an explicit
`_FUZZY_RESOLVE_LIMIT` (currently 200) to `fuzzy_resolve_by_name`
and prepends an explicit "NOTE:" disclosure to the symbol facts
block when the canonical resolution truncates.
