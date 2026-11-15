# ADR-058: MCP grounding is retrieval-layer + tool-response content only

## Status: Accepted

## Context

Chat and server-side RAG (ask-claude) enforce grounding through a **system
prompt**. `build_chat_system_prompt` composes eight grounding blocks (value
grounding, read-source verification, ADR-031 line-citation, ADR-032
list-completeness, ADR-025 code-over-docs, the magic_link structural-attribution
rule, the SAST scope guard, requirement-strength); `build_rag_system_prompt` now
carries the same stack (WS2 of the dual-channel plan). Both work because the
model that *synthesizes the answer* runs under a prompt we control.

The MCP server is different by construction. It **does not synthesize**. It
returns snippets (the `query_knowledge_base` tool) and graph-tool results
(`find_callers`, `find_importers`, `find_implementers`, `find_definitions`,
`find_symbol_by_name`, `find_symbol_at_line`) to the **client model** -- Claude
Code, Cursor, or another MCP client -- which synthesizes the answer under *its
own* system prompt. There is no server-side synthesis seam on MCP, so the eight
prose grounding rules have nowhere to be injected. They terminate at the chat /
RAG handlers by architecture.

This matters because the engineer surface (MCP / Claude Code) is the population
*most* likely to ask the structural questions where grounding bites hardest
("where is this called from", "what implements this"), and the failure is silent
-- a confidently wrong `file:line` attribution, not an error.

## Decision

**MCP grounding is structurally limited to two levers, and we use both:**

1. **Retrieval-layer grounding (ADR-033 dual-channel).** Pushed into the shared
   seam so MCP's `search_knowledge_base` now runs the two-lane (code_map + docs)
   retrieval + per-lane rerank, exactly like chat, instead of the single-channel
   path that starved the code_map lane. This is the layer MCP inherits for free
   once it is below the ADR-023 seam. (WS1 of the plan.)

2. **Tool-response content as data.** Since we cannot inject prose rules into the
   client's synthesis, we carry the load-bearing discipline as **data on the tool
   response**, which the client model can act on:
   - **Structural-attribution + line-citation provenance.** Every graph-tool
     payload carries a compact `grounding` block
     (`{"source": "symbol-graph", "cite_as": "file_path:line", "rule": "..."}`)
     alongside the per-site `file_path` / `line` / `source_symbol_id` it already
     returns. This is the ADR-031 line-citation rule and the magic_link
     structural-attribution rule expressed as data: the facts are marked
     graph-sourced and citable at `file_path:line`, so a client can cite
     precisely instead of attributing a structural claim to a doc chunk. It is
     kept to three short fields on purpose -- MCP responses are token-budgeted on
     the client side (ADR-033 cost model), so the envelope must not crowd out
     content.
   - **List-completeness** (ADR-032) continues to ride on the same payloads
     (`truncated` / `complete` / `total_known`), so a clipped enumeration is
     detectable by the client rather than silently shipped as complete.
   - **Retrieval origin** is conveyed by each result's existing `type`
     annotation plus the dual-channel `lane`: code-map / summary types are
     structural facts, doc types are authored narrative. The client has the
     material to apply code-over-docs precedence (ADR-025) without a prose rule.

3. **The SAST-scope caveat does not apply to this server.** This MCP server
   exposes **no SAST tool** -- SonarQube runs as a separate, OutcomeOps-owned MCP
   server. The ADR-025 SAST scope guard ("covers syntactic smells + known CVEs
   only; never the overall verdict, never exoneration") therefore belongs on
   *that* server's tool responses, not here. Recorded so a future change adds the
   caveat where the SAST results actually originate.

## Why the prose rules cannot port

The eight rules are instructions to a *synthesizing* model. On chat / RAG that
model is ours and reads our system prompt. On MCP the synthesizing model is the
client's, reached only through (a) the tool SCHEMA descriptions it reads when
deciding how to call a tool and (b) the tool RESPONSE content. Tool descriptions
already carry some guidance (e.g. "do not invent an enclosing function name"),
but a description cannot enforce a grounding rule over the whole answer. The only
thing that scales to arbitrary client models is grounding data on the response.
We provide the material; we cannot compel the client model to obey it. That is
the structural ceiling this ADR records.

## Consequences

### Positive
- The engineer surface stops running single-channel retrieval; the code_map lane
  is no longer starved (closes the silent `file:line` mis-attribution on MCP).
- Graph-tool facts arrive marked as structural, graph-sourced, and citable at
  `file_path:line` -- the attestation persona (security doc holds the criteria,
  code-maps hold the facts, the verdict is the join) gets citable evidence on the
  engineer surface, not just in chat.
- The grounding block is three short fields; negligible token cost.

### Tradeoffs
- We cannot *enforce* grounding on MCP, only supply it. A client model can ignore
  the `grounding` block. This is an architectural limit, not a bug to fix here.
- Grounding guidance is now split across tool descriptions (prose, read at
  call-decision time) and tool responses (data, read at synthesis time). Both are
  intentional; neither alone is sufficient.

## Related ADRs

- ADR-023: shared retrieval seam (why dual-channel could be pushed below chat).
- ADR-025: code-over-docs precedence + SAST scope (the rules translated to data).
- ADR-031: line-citation grounding (the `cite_as` provenance).
- ADR-032: list-completeness (the `truncated` / `complete` / `total_known` fields).
- ADR-033: dual-channel retrieval (the retrieval-layer grounding MCP inherits).

## References

- `lambda/mcp-server/handler.py` -- `_SYMBOL_GRAPH_GROUNDING`,
  `_query_result_to_dict`, `_resolution_result_to_dict`,
  `handle_find_symbol_at_line`, `search_knowledge_base` (dual-channel adoption).
- `lambda/shared/retrieval.py` -- `search_kb_dual_channel` / `rerank_dual_channel`
  (the shared two-lane entry MCP calls).
- `lambda/shared/prompts.py` -- `build_chat_system_prompt` /
  `build_rag_system_prompt` (the prose grounding stack that cannot port to MCP).
- `lambda/tests/unit/test_mcp_server_symbol_graph_tools.py` -- `TestGroundingEnvelope`.

Version History:

- v1.0 (2026-11-15): Initial. Records the MCP grounding ceiling
  (retrieval-layer + tool-response content), adds the `grounding` provenance
  block to the graph-tool responses, and notes the SAST caveat belongs to the
  separate SonarQube MCP server.
