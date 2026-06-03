# ADR-031: Line-Citation Grounding — Verified Enclosing Symbols at Answer Time

## Status: Accepted

## Context

ADR-024 split chat questions into router-resolvable (graph traversal,
deterministic) and synthesis (LLM narrative). ADR-025 added synthesis
guardrails to stop fabrication of behavior. ADR-027 forced the code-map
to omit literals so synthesis would not invent them from a summary that
gestured at "a token size" without naming it. ADR-028 grounded
code-map generation on extracted graph facts. ADR-030 added the
`read_source` adjudicator so synthesis can verify the exact value at
a known `file:line` by reading raw source on demand.

A residual error class remains: **wrong-enclosing-symbol citations.**
When chat synthesis writes "`handle_verify` (line 544)" or "called
from `handle_verify` near line 544", the *line number* may be correct
(line 544 in `lambda/auth/handler.py` really is the JTI claim site)
but the *enclosing function name* is fabricated. Line 544 actually
lives inside `verify_magic_token`, not `handle_verify`. The model
collapsed the call chain
`handle_verify → verify_magic_token → _claim_magic_token_jti` and
pinned the line onto the wrong scope.

The 2026-06-02 live audit eval surfaced this in a question about an
authentication system in a customer workspace. Every line number cited
was correct; one of them was attributed to the wrong function. A reader
who clicks "go to `handle_verify` line 544" lands in the wrong
function and concludes the answer is hallucinated.

`read_source` cannot catch this. It returns the line *content*, not
the *enclosing symbol*. The symbol graph already carries the data to
prevent it — every `defines` fact stores
`(workspace, repo, file_path, start_line, target_symbol_id)` — but
until this ADR, no MCP tool exposed a `(file, line) → enclosing symbol`
lookup, and synthesis filled the gap from memory.

This is the failure mode regulated reviewers screenshot during an
audit. The line citation looks confident; the navigation lands
nowhere; the conclusion is "the model fabricated the source location."

## Decision

Every chat synthesis citation that names an enclosing symbol with a
line number — `name (file:line)`, `name at line N`, `defined in
name() near line N`, "called from name on line N", etc. — must be
graph-grounded through the `find_symbol_at_line` MCP tool.

`find_symbol_at_line(workspace_id, repo, file_path, line)` returns:

- The innermost defining symbol whose `start_line ≤ line`, projected
  as `{target_symbol_id, file_path, line, repo, language}`. Synthesis
  uses `target_symbol_id` as the cited name.
- `symbol: null` when no `defines` fact covers the line (e.g.,
  module-level code, blank line between functions, line before the
  first defined symbol in the file). Synthesis must drop the
  enclosing-name claim in this case and cite only `file:line`.

The chat synthesis prompt enforces the contract via
`LINE_CITATION_GROUNDING_RULES` in `lambda/shared/prompts.py`,
composed into `build_chat_system_prompt` after the read_source block.
The rule explicitly forbids inferring the enclosing symbol from the
LLM's own reading of the file or surrounding code-map text.

The rule block also closes a separate failure mode surfaced by the
same eval: **LLM-computed aggregate counts**. An answer to "which
Lambdas call `require_auth`" claimed "65 call sites" while its own
enumeration summed to 37 and the ground truth was 51. The headline
number reconciled with neither its data nor reality. Item 6 of the
rule forbids synthesis from computing or estimating totals across
items it enumerated; only counts that appear as a deterministic
field on a tool response (e.g., `len(rows)`) may be quoted, and only
verbatim. A separate plan addresses the truncation half of that
failure (a tool returning a clipped result set so the LLM only saw
37 of 51 rows); this ADR's anti-aggregate sub-rule closes only the
hallucinated-headline half.

## Consequences

- Per-citation cost rises by ~1 DynamoDB query per distinct
  `(file, line)` cited. Acceptable: chat answers cite a handful of
  locations, not thousands. The base-table query is scoped to a
  single repo's partition by PK, filtered to the requested file via
  a `FilterExpression`.
- The synthesis prompt forbids inferring enclosing-symbol names from
  memory of file structure. If the graph does not know a line's
  enclosing symbol, the answer must say so rather than guess.
- v1 uses **start-line only**: a line that falls *between* two
  defines (module-level code, blank lines between functions) returns
  the previous function rather than `null`. The prompt rule tells
  synthesis to prefer dropping the enclosing-name claim over citing
  a function that almost contains the line. v2 will add `end_line`
  to `defines` facts for strict containment; that change touches all
  five language extractors (Python, TypeScript, Java, C#, ABAP) and
  is deferred until the v1 contract has been live-verified.
- Line citations in chat answers become audit-quality: every
  `name (file:line)` is a graph-resolvable fact, navigable by the
  reader and verifiable by the auditor.
- The anti-aggregate sub-rule (item 6) eliminates the hallucinated-
  headline class. Together with the structured count fields added by
  the follow-on recall plan, every quantified claim in a chat answer
  becomes traceable to a tool response.

This ADR is additive to:

- **ADR-024** — router-vs-synthesis split.
- **ADR-025** — synthesis guardrails (no fabrication of behavior).
- **ADR-027** — code-map omits literals (no value fabrication via
  summary recall).
- **ADR-028** — graph-first code-map generation.
- **ADR-030** — read_source adjudicator (verify values AT a line).
- **ADR-031** (this) — verify which symbol *contains* a cited line.

## Alternatives Considered

- **Pure prompt discipline ("do not guess enclosing function").**
  Cheap but unreliable: the model patches enclosing names from
  memory whenever the code-map summary is its only context. Does
  not produce an audit story.
- **Re-extract enclosing function as a precomputed property on
  every `defines` fact, queried directly by `(file, line)`.** That
  is functionally what `find_symbol_at_line` does behind the tool
  surface — the contract ADR captures the user-visible behavior; the
  implementation can evolve (precompute table, dedicated GSI, etc.)
  without breaking the contract. The reader's PK structure
  (`workspace#bare_repo`) already supports the query without
  additional indexing.
- **Require synthesis to call `read_source` for every line citation
  and parse the returned window for the enclosing `def`/`function`/
  `public class` keyword.** Possible but brittle: parsing varies
  per language, blank-line offsets shift the keyword window, the
  approach requires `read_source` calls on lines synthesis does not
  otherwise need. Worse fit than a graph tool that already has the
  exact answer.
- **Strict end-line containment in v1 instead of start-line-only.**
  Considered. Deferred because adding `end_line` to all five
  language extractors is a larger surface than this ADR justifies on
  its own. The start-line-only contract handles the audit-relevant
  failure mode (wrong-function-for-line-inside-function); the
  between-functions false-positive is mitigated by telling synthesis
  to drop the name claim when uncertain.

## Implementation Pointer

The MCP tool ships as `find_symbol_at_line` on the
`outcomeops` gateway (`lambda/mcp-server/handler.py`), backed by
`symbol_graph_reader.symbol_at_line` in `lambda/shared/`. The
synthesis prompt rule (`LINE_CITATION_GROUNDING_RULES`) is composed
into `build_chat_system_prompt` in `lambda/shared/prompts.py`. v1's
start-line-only limitation is documented in the tool's MCP
description so synthesis can comply without needing to read this
ADR.
