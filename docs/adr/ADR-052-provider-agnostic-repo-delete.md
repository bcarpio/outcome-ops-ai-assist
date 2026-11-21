# ADR-052: Repo deletion is a single provider-agnostic contract

## Status: Accepted

- v1.0 (2026-07-19) — Records that removing a repo must purge ALL of its
  RAG artifacts (source mirror, per-doc vectors, bare-name
  architectural-summary, code-map/summary vectors, and symbol-graph rows)
  in every region, and that GitHub, GitLab, and Azure DevOps all route
  through one shared helper so the contract cannot drift per provider.
  Written after GitLab repo removal left the architectural-summary +
  code-map vectors queryable in the knowledge base.
- v1.1 (2026-11-20): Ingestion writers check that the repo is still
  connected before persisting, and every delete runs a delayed second
  pass, so in-flight ingestion cannot recreate a removed repo.

## Context

A repo's data does not live under a single prefix. For any code host a
repo spans:

1. **Source mirror + per-doc chunk vectors** under the full slug —
   `workspaces/{ws}/{source}/{slug}/…` in S3 and
   `{ws}#{source}#{slug-with-#}#{path}_chunk_N` in S3 Vectors.
2. **The architectural-summary** (S3 + its chunk vectors) written under
   the **bare** repo name — `workspaces/{ws}/{source}/{bare}/architectural-summary.txt`
   and `{ws}#{source}#{bare}#architectural-summary.txt_chunk_N` — NOT
   under the slug.
3. **Code-map / handler-group / *-summary aggregate vectors**, keyed
   `{ws}#{source}#{bare}#{batch_type}#{group}` (bare name, no
   owner/group/org/project segment).
4. **Symbol-graph facts + the code-maps pointer** in DynamoDB, keyed by
   the **bare** repo name.

Under HA (ADR-022) every one of these exists in BOTH regions, and each
region's delete-worker cleans only its own stores.

Only GitHub removal was complete. GitHub routes through
`shared.delete_dispatch.dispatch_repo_delete`, which enqueues a
`delete_prefix` for the slug, a second `delete_prefix` for the bare name,
and a `delete_symbol_graph` — to every region's queue. GitLab and Azure
DevOps each **hand-rolled a single slug `delete_prefix`**, so they leaked
shapes 2, 3, and 4. Two further defects compounded it:

- The delete-worker's code-map/summary vector sweep was gated
  `source == "github"`, so even a correct bare `delete_prefix` from
  GitLab/ADO would not have purged shape 3.
- GitLab keyed its code-map vectors on the **slug**
  (`{ws}#gitlab#{slug}#code-map#…`) while GitHub and ADO used the bare
  name — so GitLab was the lone outlier even in the key convention.
- ADO's hand-rolled message set `source = "azure_devops"` AND
  `prefix = "azure_devops/{slug}/"`; the worker prepends
  `workspaces/{ws}/{source}/`, so the path doubled the `azure_devops/`
  segment and ADO deletes silently cleaned nothing.

A complete delete is still not enough on its own, because ingestion work
that was queued or running when the repo was removed can finish afterwards
and write everything back. This happened in prd with a repo removed while
its sync was in flight: the delete purged every shape above and the
symbol-graph delete logged VERIFY clean, then about 15 minutes later an
already-queued code-map job wrote roughly 70,000 symbol-graph facts, the
code-map vectors, and the code-maps pointer back for a repo that no longer
had a workspace row. Nothing re-syncs a removed repo, so the copy was
permanent: it surfaced deleted code in chat and MCP retrieval and produced
false consumers and duplication findings in PR checks. Deleting an idle
repo always passes, which is why ordinary delete testing never caught it.
No pipeline stage checked that the repo was still connected before it
wrote; the SHA-advance step refuses a removed repo, but it runs after the
vectors and facts are already written.

## Decision

1. **One helper, all providers.** `shared.delete_dispatch` is the single
   source of truth for *what* a repo-delete queues. It takes a `source`
   argument (`github` / `gitlab` / `azure_devops`, default `github` for
   existing callers) and derives the **bare** name as the LAST path
   segment of the slug — so nested GitLab subgroups
   (`group/subgroup/project`) and ADO's `org/project/repo` both collapse
   to the final segment, matching how shapes 2–4 are keyed. GitHub,
   GitLab, and ADO integrations all call `dispatch_repo_delete(...,
   source=…)` with primary + (under HA) secondary queue targets.

2. **The delete-worker code-map sweep is provider-agnostic.** The sweep
   that purges `{ws}#{source}#{bare}#…` aggregate vectors runs for every
   `source`, not just github.

3. **Code-map vector keys are bare for every provider.** EVERY code-map /
   summary vector writer keys on the bare project name (last path segment)
   uniformly: `process-batch-summary.store_batch_summary`,
   `process-repo-summary.store_code_map_embedding` (the direct
   architectural code-map vector), and the orphan-sweep's expected-key
   mirror `_expected_code_map_vector_keys`. The `delete_orphan_code_maps`
   producer/consumer also became provider-aware (it hardcoded `#github#`,
   so GitLab orphans were never swept). GitHub/ADO already passed a bare
   name (no-op); GitLab's slug is normalized, so its code-map vectors
   share the `{ws}#{source}#{bare}#…` shape both delete sweeps purge. A
   GitLab slug keeps its literal `/` when embedded whole, which would
   dodge both sweeps — hence normalizing at the writer, not the deleter.

### v1.1: a delete must survive in-flight ingestion

4. **Every ingestion writer checks that the repo is still connected
   immediately before it persists anything.** This covers the sync
   Lambdas, the repo summary, the batch summary, and document embedding,
   and it applies to every persistent write: vectors, symbol-graph facts,
   the code-maps pointer, and S3 uploads. All writers use one shared
   helper keyed by provider (`github`, `gitlab`, `azure_devops`) that does
   a strongly consistent read of the repo's workspace row, so the check
   has the same meaning for every code host. Long-running writers also
   check at job start, so a removed repo does not spend LLM or embedding
   work first; the start check alone is not sufficient, because the LLM
   call itself is a race window. The two outcomes are handled differently:
   - **Repo removed:** drop the work, log it, and acknowledge the message.
     This is never a retryable failure, since retrying cannot bring a
     removed repo back.
   - **Lookup failed:** retry, never write. Writing data for a repo the
     user removed is worse than a delayed write, because the removed repo
     reappears in every search.

5. **Every removal sends the cleanup twice.** The delete helper enqueues
   the normal delete messages immediately and the same set again with a
   15 minute delay, marked as a recheck. The delete-worker skips a recheck
   message when the repo is connected again, so a repo re-added within the
   window is never wiped. The immediate pass does not check presence,
   because the UI queues the delete before it removes the workspace row.
   With writers checking right before they write, the remaining race
   window is seconds, well inside the 15 minute delay; the second pass
   cleans up anything that slipped through it.

## Consequences

- Removing a repo from any code host purges every RAG artifact, in every
  region — no lingering architectural-summary, code-map, or symbol-graph
  residue that a `read_source` / retrieval call could still surface.
- ADO deletes now actually delete (the double-prefix bug is fixed by
  routing through the helper's repo-relative prefixes).
- GitLab code-map vectors move to the bare-name key on the next ingest.
  There is no migration step: a re-ingest (or the normal hourly sync)
  rewrites them, and any pre-existing slug-keyed vectors are removed by
  the same delete path when the repo is removed.
- New code hosts inherit complete deletion for free by calling
  `dispatch_repo_delete(..., source=<new>)`; the per-provider drift that
  caused this class of bug (delete-bug 01/02/03 for GitHub, and this one
  for GitLab/ADO) is structurally prevented.

## Alternatives considered

- **Teach the delete path GitLab's slug-keyed code-map quirk** (delete
  both bare and slug prefixes) instead of normalizing ingest. Rejected:
  it permanently embeds a per-provider special case in the delete path,
  the opposite of a generic platform (CLAUDE.md). Normalizing the key at
  ingest makes all three providers identical and keeps the delete path
  uniform.
- **Per-provider delete handlers.** Rejected — that is exactly the drift
  this ADR removes; the three integrations previously diverged and each
  missed a different subset of shapes.

## Related

- ADR-022 (active/passive HA — why deletes fan out to both regions),
  ADR-030 (read_source — the consumer that would surface leaked
  artifacts), ADR-050 (GitLab integration). Code:
  `lambda/shared/delete_dispatch.py`, `lambda/delete-worker/handler.py`,
  `lambda/process-batch-summary/handler.py`, and the `_queue_delete_job`
  in the gitlab / azure-devops integrations.

## Version History

- v1.0 (2026-07-19): one provider-agnostic delete helper; provider-agnostic
  code-map sweep; bare code-map vector keys for every provider
- v1.1 (2026-11-20): writers check repo presence before persisting; delayed
  second delete pass
