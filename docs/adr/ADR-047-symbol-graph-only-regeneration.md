# ADR-047: Symbol-graph-only regeneration (vector-preserving)

## Status: Accepted

## Context

ADR-046 fixed the symbol-graph fact lifecycle: `write_facts` now does per-file
replace-on-reextract, so a file's stale generations collapse the next time it is
re-extracted. But that only heals a file **when it is re-extracted**. Graphs
that accumulated stale generations before the fix (up to 6+ generations per
symbol were observed) stay corrupted until each file passes through the fixed
writer again. Files in repos that never change again — forked OSS demo repos,
stable internal apps — would stay corrupted indefinitely.

The obvious cleanup, a normal re-ingest, is the wrong tool: the ingest pipeline
couples symbol-graph extraction to **Bedrock batch summarization and vector
embedding** inside `process-batch-summary.generate_batch_summary`. Re-ingesting
to fix the graph would re-run every summary and rewrite every vector — real
Bedrock spend, for repos whose summaries and vectors are **already correct**
(the accumulation bug is purely a symbol-graph fact-lifecycle defect; it never
touched the summaries or the vector store).

## Decision

**Cleanup is a symbol-graph-ONLY regeneration that never touches the vector
store or re-runs summaries.**

The extractor pass is cleanly separable — in `generate_batch_summary` the
symbol-graph work is `extract_facts_from_bytes(file_path, language, content,
sg_filter)` per file, and the summary + embedding + vector write all happen
downstream of it. The regeneration reuses only the upstream half:

Per repo, workspace by workspace:

1. **Select** repos with symbol-graph enabled: the `include_code_maps` flag
   (default `true`) on the workspace's `GITHUB_REPO#<owner>/<repo>` row, AND
   `.outcomeops.yaml`'s `ingestion.symbol_graph.enabled` (default `true`).
2. **Enumerate** the repo's tracked files at HEAD (the repo tree — tracked
   files only, never dependency dirs), filtered by the same `SymbolGraphFilter`
   the pipeline applies (`should_extract`: ADR-009 base excludes, repo
   include/exclude globs, language allow-list, `max_file_bytes`).
3. **Purge** all symbol-graph facts for the repo partition plus the graph
   pointer — and NOTHING else. The docs/vector state rows are left intact.
4. **Regenerate**: extract each file through `extract_facts_from_bytes` and
   persist via `shared.symbol_graph_writer.write_facts` (per-file replace).
5. **Never** invoke `process-documents`, embedding, or S3 Vectors.

The only DynamoDB partition written is the symbol-graph table's
`{workspace_id}#{repo}` and the code-maps graph pointer. The vector store is
not read or written.

### Fact key uses the BARE repo name

Symbol-graph facts key on the **bare repo name** (`outcome-ops-ai-assist`),
not `owner/repo`, in both the PK (`{workspace_id}#{repo}`) and the purge scope.
This is deliberate (github-sync keeps the fact-store keys bare-named; flipping to
`owner/repo` would orphan existing data). A regeneration MUST purge and write the
bare-named partition or it will hit an empty partition and silently no-op.

### Where the file bytes come from

The reference procedure enumerates and reads the repo tree at HEAD via the
GitHub tree/contents API using the installation token
(`shared.github_auth.get_installation_token`), matching how the pipeline fetches
(`fetch_file_content`). When the operator has a local checkout of the repo, the
tracked file set (`git ls-files`) and on-disk contents are an equivalent source
and avoid the API round-trips — used for the maintainer-sandbox runs.

## Consequences

- Corrupted graphs are cleaned with **zero Bedrock spend and zero vector
  writes**. Safe to run across forked/stable repos that must not be
  re-summarized.
- The operation is idempotent: re-running regenerates the same single generation
  per file (per-file replace makes the purge step belt-and-suspenders, needed
  only to evict orphans of files deleted since the last ingest).
- **Customers need this too.** After a customer deploys the ADR-046 fix, their
  graphs carry the same pre-fix accumulation. This ADR is the procedure they
  run — a symbol-graph-only regen in their own account — without re-summarizing
  their repos. The one-shot runner that executes it is disposable per-environment
  tooling; this ADR is the durable, portable specification.
- **Pointer/staleness:** a regen advances the symbol-graph pointer to the
  extracted HEAD so `staleness_seconds` reflects the regeneration. (If a run
  intentionally leaves the pointer untouched, the facts are still correct; only
  the staleness display lags.)
- **Not a substitute for ADR-046.** This is one-time cleanup of pre-fix
  corruption. Ongoing correctness comes from the writer fix; without it, regen
  output would re-accumulate on the next edit.

## Alternatives considered

- **Normal full re-ingest.** Rejected: re-runs summaries + re-embeds + rewrites
  vectors — wasted Bedrock spend for data that is already correct, and
  needlessly perturbs the vector store.
- **Passive self-heal (do nothing; let edits collapse files over time).**
  Rejected: files in never-changing repos stay corrupted forever, and the
  corruption is user-visible now in `find_callers` / `find_definitions` and
  every other graph consumer (chat retrieval, analyze-PR, codegen, read_source).
- **Read-time generation filter.** Rejected in ADR-046 — masks the data,
  adds hot-path latency, and leaves every non-MCP consumer reading bloat.
