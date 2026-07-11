# ADR-046: Symbol-graph facts use per-file replace-on-reextract

## Status: Accepted

## Context

The symbol graph stores structural facts (`defines`, `calls`, `imports`,
`implements`, `extends`) as DynamoDB rows and serves them through
`find_callers`, `find_definitions`, `find_importers`, `find_implementers`,
and `find_symbol_at_line`. It underpins the line-citation grounding described
in ADR-031 and the read_source adjudication in ADR-030.

A verification of the graph against ground-truth source (a Claude Code session
diffing `find_callers` / `find_definitions` output for a heavily-edited handler
against the current file) found the query results were a **superset with a
large false-positive rate**: every real call site appeared, but padded with
phantom entries pointing at unrelated lines (return statements, `except`
clauses, dict literals, comments, blank lines) inside the same enclosing
function, and definitions reported at stale line numbers. Concretely, for one
symbol: the definition was reported ~120 and ~340 lines above its true
location in the two largest files, and call-site counts were inflated 2-5x,
while small files that had not changed since first ingestion were exact.

### Root cause

The fact sort key **embeds the line number**:

```
SK = {kind}#{source_symbol_id}#{target_symbol_id}#{file_path}#{line}
```

The writer (`shared/symbol_graph_writer.write_facts`) is a pure upsert. A
prior design ("v2") **removed the delete pass** on the theory that
re-extraction "overwrites in place" under the same keys, and left
`previous_commit_sha` deprecated/unused and `facts_deleted` hardcoded to `0`.

That theory holds only for facts whose line number does not move. Any edit
that inserts or removes lines above a call or definition shifts its line, which
changes the SK, so re-extraction writes a **new row** while the old row
survives. Facts are deleted only on **explicit repo/file deletion** (the
delete-worker's `delete_symbol_graph`, reached through `delete_dispatch`) —
never on ordinary re-ingestion of an edited file.

The result: every re-ingestion of an edited file accumulates another
generation of stale, line-keyed facts. The GSI1 read path
(`GSI1PK = {workspace_id}#{target}`, `GSI1SK begins_with "calls#"/"defines#"`)
returns the **union of all historical generations**. Actively-edited files
accumulate the most noise; files unchanged since first ingestion stay exact.
This is language-agnostic: all six extractors (python, typescript, java,
csharp, go, abap) funnel through the same writer, so all are affected equally.

This is a broader manifestation of the previously-deferred "symbol graph never
deletes on file removal" gap: it corrupts not just removed files but every
edited file's callers and definitions.

### What is NOT the cause

The per-extraction line mapping is correct — each extractor emits a fact at the
call/definition node's own line. The defect is fact **lifecycle**, not
extraction. A read-time "collapse to the newest generation" filter was
considered and rejected (see Alternatives): it masks the corruption, adds
latency to the `find_callers` hot path (which carries a p99 guard), and cannot
distinguish a legitimately unchanged file's older `commit_sha` from a stale
generation under incremental ingestion.

## Decision

**Symbol-graph facts follow per-file replace-on-reextract semantics: the writer
writes a file's new generation, then prunes that file's prior rows, so exactly
one generation of facts per file survives.**

1. **Replace scope is the file, not the repo.** Ingestion is incremental — a
   re-extract processes only changed files, batched. A repo-wide delete would
   wipe unchanged files' facts that will not be rewritten. The unit of
   replacement is therefore `(workspace_id, repo, file_path)`.

2. **The writer owns the replace, write-before-prune.** `write_facts` receives a
   batch whose facts may span several files (each `Fact` carries `file_path`). It
   writes the new generation FIRST, then prunes each file's prior rows — the
   rows for the batch's files whose SK is not in the generation just written.
   `facts_deleted` counts the pruned rows (0 when nothing shifted).

   **Ordering is a safety property, not incidental.** The ADR-046 replace roughly
   doubles write volume (a delete plus a put per fact). Re-extraction is
   incremental (only the code units whose files changed are reprocessed), but a
   single large handler's facts, written across parallel process-batch-summary
   batches, is enough of a burst to briefly outrun the on-demand symbol-graph
   table's auto-scaling and raise `ThrottlingException` — no full-repo sync is
   required. process-batch-summary swallows a symbol-graph write failure (it must
   never break the summary path) and does not retry, and the pointer still
   commits.
   With **delete-first**, a write that throttles *after* the delete would leave
   the file with **zero facts** — silent data loss under a load the fix itself
   creates. **Write-then-prune** makes the same failure non-destructive: a failed
   write prunes nothing, so the file keeps its prior generation (stale, not
   gone), self-healing on the next successful re-extract. To further reduce the
   throttling, the DynamoDB client uses adaptive retries.

3. **Enumerating a file's rows to prune** reuses the delete-worker's proven
   key-query pattern (over the `{workspace_id}#{repo}` partition, projected to
   keys plus `file_path`), filtered to the batch's files and excluding the
   just-written SKs. No base-table SK change and no data migration are required,
   so the change is additive and reversible. This trades a per-batch partition
   key-read for correctness; it does not add a standing write tax.

4. **Concurrency invariant.** A single file's facts are written by exactly one
   batch per re-ingestion (the batch splitter partitions by file). Delete scope
   is limited to the batch's own files, so parallel batches for the same repo
   touch disjoint file sets and do not race on each other's deletes. This
   invariant must hold; it is asserted in tests.

5. **Existing corruption is purged by a one-time full re-extract** per repo
   after the fix deploys — forcing every file through the new replace path
   evicts its accumulated stale generations. This runs against any deployment
   (the maintainer sandbox and every customer deploy of this codebase) with no
   bespoke per-row deletion.

6. **Extractor completeness is a separate, parallel fix.** A confirmed
   secondary defect — the Python extractor skips decorated top-level
   definitions (`decorated_definition` is not unwrapped, so `@app.get`-style
   route handlers emit no `defines` and their internal calls are lost) — and
   the analogous "wrapped/annotated top-level definition" class in the other
   language extractors are corrected under the same effort. This is
   under-reporting, orthogonal to the over-reporting fixed here, but shares the
   verification pass.

## Alternatives considered

- **Read-time newest-generation filter.** Collapse duplicate `(target,
  file_path)` rows to the highest `commit_sha`/`created_at` at query time.
  Rejected: masks rather than fixes the corruption; adds work to the latency-
  guarded `find_callers` path; and under incremental ingestion an unchanged
  file legitimately carries an older `commit_sha`, so "newest wins" is not a
  safe generation discriminator.

- **File-first SK restructure** (`{file_path}#{kind}#{source}#{target}#{line}`).
  Makes per-file delete a native base-table `begins_with` query and would speed
  `find_symbol_at_line`. Rejected for now: requires rewriting every existing
  item (migration) and updating every reader that parses the SK — a large blast
  radius for a correctness fix that the chosen approach delivers additively.
  Revisit if per-batch delete-read cost becomes material.

- **Dedicated file-delete GSI** (`{workspace_id}#{repo}#{file_path}`).
  Targeted per-file delete with auto-backfill of existing data. Rejected as the
  default: a standing GSI write on a high-write table taxes every ingestion
  write forever to support an operation that runs only on re-ingestion. Kept as
  the escalation path if the per-batch partition read proves too costly at
  enterprise repo sizes.

- **Per-file fact manifest row.** Store the SKs written for each file and
  delete via the manifest. Rejected: adds write-path complexity and a manifest
  row per file for no advantage over the partition-key-read approach at current
  scale.

## Consequences

- `find_callers` / `find_definitions` / `find_importers` /
  `find_implementers` / `find_symbol_at_line` return exactly one generation of
  facts per file — no phantom lines, no inflated counts, definitions at their
  true current line. The line-citation grounding (ADR-031) and read_source
  adjudication (ADR-030) are restored to trustworthy line accuracy.
- Re-ingestion of a changed file does one extra partition key-read plus the
  deletes of the prior generation before writing. Symbol-graph storage is not
  the platform cost driver, so the added read is acceptable; it is revisited
  only if ingestion latency or read cost regresses at enterprise scale.
- The HA parity rule applies: any new resource added by the implementation
  (should the escalation GSI ever be adopted) mirrors into the secondary
  region, gated on `secondary_region`.
- The one-time full re-extract is required after deploy on every environment
  carrying an already-corrupted graph; until it runs, previously-edited files
  keep their stale generations.
