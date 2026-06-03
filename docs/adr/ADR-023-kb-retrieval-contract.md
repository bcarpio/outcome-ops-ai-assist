# ADR-023: Knowledge-base retrieval contract (shared oversample + rerank + bounded response)

## Status: Partially superseded by ADR-033 (dual-channel retrieval) for the chat-handler path

> The oversample + rerank + bounded-response contract this ADR
> defined still applies to the MCP gateway's `search_knowledge_base`
> tool (`lambda/mcp-server`). The chat-streaming Lambda
> (`lambda/chat`) was previously bound by this contract but has
> since migrated to dual-channel retrieval per ADR-033 -- two
> parallel queries (code-maps lane + docs lane), per-lane rerank,
> combined for synthesis. New chat-side retrieval work should
> reference ADR-033; the original ADR-023 contract is retained for
> the MCP-server tool.

## Original Status: Accepted

## Context

The platform has two knowledge-base retrieval surfaces over the same S3
Vectors index:

- **`lambda/query-kb`** — the chat-side retrieval Lambda.
- **`lambda/mcp-server`** — the external-agent surface (Claude Code and
  other LLM clients connect here).

These drifted. `query-kb` oversampled (`fetch_k = top_k * 4`), reranked
with Cohere Rerank 3.5, and culled to a small `top_k`. The MCP server's
`search_knowledge_base` was a parallel, simpler implementation: default
`top_k=10`, no reranker, and a `format_results` that concatenated every
chunk's full content with `---` separators and **no size cap**.

The consequence: a single broad `query_knowledge_base` call returned
~60KB / 2,000+ lines. That overflows the context budget of the very
agents the MCP server exists to serve (Claude Code itself hit this and
had to spill to a subagent), and it inflates every downstream synthesis
prompt. Because the platform dogfoods its own MCP server for chat, the
failure hit every app built against it.

Per `project_moat_audit`, structural + KB retrieval is the platform's
defensible position; the retrieval surface returning 60KB for a one-line
question is the worst shape for that claim.

## Decision

Consolidate retrieval mechanics into one shared module and define a
bounded response contract that both surfaces honor.

### Key Points

- **One implementation, `lambda/shared/retrieval.py`.** It owns the S3
  Vectors query + normalization (`query_vectors`), the Cohere rerank
  pass (`rerank`), and snippet/token helpers (`make_snippet`,
  `estimate_tokens`). Clients, bucket/index, and rerank config are
  **injected** by each caller — chat filters by `repo`, MCP filters by
  `workspace_id`; the env var names differ — so the module owns
  mechanics, the caller owns config. This is the same
  consolidate-the-duplicated-critical-path pattern used for the Entra ID
  OAuth helper.

- **Oversample → rerank → cull, everywhere.** `fetch_k = min(top_k*4,
  RERANK_MAX_FETCH)` when rerank is enabled, then Cohere Rerank reorders
  and `top_n=top_k` culls. The reranker degrades gracefully to cosine
  order on any Bedrock error, so a region without Cohere Rerank (e.g.
  `us-east-2`) returns cosine-ordered results rather than failing.

- **Bounded response contract.** Defaults `top_k=5` (matches chat), a
  ~`4000`-token total-response cap, and `min_score=0.0`. Callers may
  override per request via the tool schema: `top_k` (≤20), `max_tokens`
  (≤16000), `min_score` (0.0–1.0).

- **Snippet windowing.** Each result is windowed to a query-relevant,
  sentence-bounded snippet (~500 tokens) instead of the full chunk. The
  match is at chunk granularity (no sub-chunk offset), so the window is
  chosen by query-term overlap with a sentence-prefix fallback.

- **Whole-result culling under the cap.** When the running total would
  exceed `max_tokens`, lower-ranked results are dropped entirely (top
  matches stay intact) and an explicit omission notice is appended,
  rather than truncating mid-result.

- **Match scores surfaced.** Each formatted result is annotated with its
  rerank (or cosine) relevance score so callers can judge confidence and
  filter client-side.

### Example Implementation

```python
# MCP search: oversample, rerank, cull, then bound at format time.
docs = shared_retrieval.query_vectors(
    s3vectors_client=_get_s3vectors_client(),
    vector_bucket=S3_VECTORS_BUCKET, vector_index=S3_VECTORS_INDEX,
    query_embedding=embedding, top_k=fetch_k,
    filter_dict={"workspace_id": {"$eq": ws}}, text_truncate=0,
)
docs = shared_retrieval.rerank(
    bedrock_agent_runtime_client=_get_bedrock_agent_runtime_client(),
    rerank_model_id=RERANK_MODEL_ID, query=query,
    documents=docs, top_n=top_k, lambda_name="mcp-server",
)
return format_results(docs, query=query, max_tokens=max_tokens)
```

## Consequences

### Positive

- The 60KB failure is fixed structurally: default settings cap a broad
  query to a few KB; even adversarial inputs are bounded by `max_tokens`.
- Chat and MCP can no longer drift — one reranker, one normalization,
  one repo-derivation.
- MCP gains reranking (cross-attention precision) for the first time.
- External agents get caller-side control (`top_k`/`max_tokens`/
  `min_score`) and match scores for client-side filtering.

### Tradeoffs

- MCP now calls Bedrock Rerank, requiring `bedrock:Rerank` on the MCP
  Fargate task role (added). Without it, the call AccessDenies and falls
  back to cosine order — degraded quality, not an outage.
- Snippet windowing can omit relevant text deeper in a chunk when the
  match is purely semantic (no term overlap); the prefix fallback
  mitigates this. Revisit if ingest-time sub-chunk offsets become
  available.
- The token estimate is a 4-chars/token heuristic, not a real
  tokenizer — adequate for bounding output, not for billing.

## Implementation

### Starting today

1. `lambda/shared/retrieval.py` owns `query_vectors`, `rerank`,
   `make_snippet`, `estimate_tokens`.
2. `query-kb` delegates `query_s3_vectors`/`rerank_results` to the shared
   module; its 6-key output and behavior are unchanged (regression gate:
   the existing `test_query_kb.py` suite).
3. `mcp-server` uses the shared pipeline, threads `top_k`/`max_tokens`/
   `min_score` through both tool schemas and handlers, and bounds the
   response in `format_results`.
4. MCP Fargate task role granted `bedrock:Rerank`; rerank env vars wired.

### Next phases

1. Consider ingest-time chunk offsets so snippets can window around the
   exact matched span rather than by term overlap.
2. Evaluate whether chat should also adopt snippet windowing (currently
   chat passes full chunks to synthesis on purpose).

## Related ADRs

- ADR-021: GSI naming for the symbol graph (the other MCP retrieval
  surface, already bounded/structured — out of scope here).
- ADR-018: Audit logging design (rerank calls emit a cost/usage row).

## References

- `lambda/shared/retrieval.py`, `lambda/query-kb/handler.py`,
  `lambda/mcp-server/handler.py`
- `lambda/tests/unit/test_retrieval.py`,
  `lambda/tests/unit/test_mcp_outcomeops.py` (cap regression)
- `docs/mcp-server.md` — "Subsequent Image Updates" (Fargate
  rebuild/redeploy/reconnect runbook)

Version History:

- v1.0 (2026-05-21): Initial decision — consolidate retrieval, bound the
  MCP response, fix the 60KB-for-one-question failure.

<!-- Confluence sync -->
