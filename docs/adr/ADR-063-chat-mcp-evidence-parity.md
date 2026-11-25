# ADR-063: Chat and MCP answer from the same evidence; code wins there, ADRs win in PR review

## Status: Accepted

## Context

The platform answers questions on two surfaces:

- **Chat** synthesizes the answer itself, under our system prompt.
- **The MCP server** returns evidence to a client model (Claude Code, Cursor,
  or another MCP client), which synthesizes the answer under its own prompt
  (ADR-058).

Both get the same questions: "how does X work?", "how long is a token
valid?", "do we already have a service that does Y?". Over time the two
drifted apart in what evidence they hand to the synthesizing model:

| Evidence | Chat | MCP `search_hybrid` / `query_knowledge_base` |
|---|---|---|
| Dual-channel retrieval (ADR-033) | yes | yes |
| Symbol-graph facts (ADR-059) | yes | `search_hybrid` only |
| Source lines of the matched definition (ADR-030 v1.2) | yes | no |
| Code-map source fallback, reranked on definition heads (ADR-030 v1.3) | yes | no |
| File constants used by a source window | yes | no |

The result is that "code wins" holds in chat and not over MCP. Chat now
quotes `MAGIC_LINK_EXPIRY = 3600` from source and calls a README's
"15-minute expiry" stale. The same question over MCP gives the client the
README and a code-map summary that leaves out literal values (ADR-027), so
the client repeats the stale value unless it chooses to call `read_source`.
The engineers on the MCP surface are the people most likely to ask these
questions.

A second question came up at the same time: when an ADR and the code
disagree, which one wins? The answer depends on what the surface is for.

## Decision

### 1. Chat and MCP return the same evidence

For any question, the MCP search tools return the same evidence chat attaches
to its context. One shared step in `lambda/shared` builds that evidence:

- symbol-graph augmentation for the matched symbol;
- source lines for the matched definitions;
- the code-map fallback when no symbol matches (resolve, rerank on definition
  heads, read);
- the constants a window uses.

Chat renders the evidence into its prompt. The MCP tools return it as data:
a `source_lines` section with file, line range, SHA, window text and
constants. Neither surface builds evidence the other lacks. A new kind of
evidence (for example caller windows, #58) lands on both surfaces in the
same change, or it does not land.

Prose grounding rules still cannot reach the client model (ADR-058). The
evidence can, and the ADR-058 grounding envelope says that source lines
outrank prose on how-it-works questions.

A parity test feeds the same question, hits and graph to both paths and
asserts the same evidence. It is the enforcement for this decision.

### 2. Which source wins depends on the surface

| Surface | Question | Source of truth |
|---|---|---|
| Chat, MCP | How does it work? Does it exist? What value does it use? | **The code.** Docs that disagree are reported as drift (ADR-025). |
| PR review (`analyze-pr`, `process-pr-check`) | Does this change follow our decisions? | **The ADRs.** |

PR review enforces the ADRs as written. If a change fails a check because the
ADR is out of date, the check is working: the fix is to update the ADR, in
the same PR or before it. PR review never lets newer code override or soften
an ADR finding, and we do not build "stale ADR" detection that relaxes the
check.

Chat and MCP describe the system as it is. An ADR there is evidence of
intent, not of behavior. When an ADR describes behavior the code does not
have, the answer goes with the code and names the gap.

## Consequences

### Positive

- An engineer gets the same answer in Claude Code as in the chat UI.
- Code-over-docs (ADR-025, ADR-030) holds on the surface where engineers
  ask the most detailed questions.
- One evidence step means one place to fix retrieval bugs, not two.
- The split between behavior and intent is explicit. Review keeps its teeth,
  and chat stops repeating stale docs.

### Tradeoffs

- MCP search responses get bigger. Source windows count against the
  caller's `max_tokens`, and the evidence is trimmed to fit, never dropped
  silently (the response says what was cut).
- MCP calls get slower on fallback turns: one rerank call and up to 8 file
  reads, the same cost chat pays (ADR-030 v1.3).
- The MCP task role needs the same source-read and rerank grants as chat,
  mirrored on the secondary (ADR-022).
- A team with outdated ADRs sees PR checks fail until the ADRs are updated.
  That is intended.

## Implementation

Tracked in #67:

1. Move chat's evidence assembly into one shared function that returns
   structured evidence; chat calls it with no behavior change.
2. `search_hybrid` and `query_knowledge_base` call it and return a
   `source_lines` section. `get_coding_standards` stays a standards lookup.
3. Surface-neutral settings for the source-lines caps, shared by both.
4. The parity test, then a live check of the magic-link question over MCP.

## Related ADRs

- ADR-022: HA parity (the MCP grants mirror on the secondary).
- ADR-025: code-over-docs precedence in synthesis.
- ADR-027: code-maps leave out literal values (why source lines are needed).
- ADR-030: source lines in chat (v1.2) and the code-map fallback (v1.3).
- ADR-058: MCP grounding is evidence and tool-response data only.
- ADR-059: the shared hybrid retrieval seam this extends.

## References

- `lambda/chat/handler.py`: the evidence chat assembles today
  (`_augment_context_with_symbol_graph`, `code_map_definitions`,
  `_source_lines_block`).
- `lambda/mcp-server/handler.py`: `handle_search_hybrid`,
  `handle_query_knowledge_base`.
- `lambda/shared/hybrid_augment.py`, `lambda/shared/source_reader.py`.

Version History:

- v1.0 (2026-11-25): Initial. Chat and MCP share one evidence step; code
  wins on chat and MCP, ADRs win in PR review.
