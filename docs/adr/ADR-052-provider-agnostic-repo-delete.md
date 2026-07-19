# ADR-052: Repo deletion is a single provider-agnostic contract

## Status: Accepted

- v1.0 (2026-07-19) — Records that removing a repo must purge ALL of its
  RAG artifacts (source mirror, per-doc vectors, bare-name
  architectural-summary, code-map/summary vectors, and symbol-graph rows)
  in every region, and that GitHub, GitLab, and Azure DevOps all route
  through one shared helper so the contract cannot drift per provider.
  Written after GitLab repo removal left the architectural-summary +
  code-map vectors queryable in the knowledge base.

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

3. **Code-map vector keys are bare for every provider.**
   `process-batch-summary` keys aggregate vectors on the bare project
   name (last path segment) uniformly. GitHub/ADO already passed a bare
   name (no-op); GitLab's slug is normalized here, so its code-map
   vectors now share the `{ws}#{source}#{bare}#…` shape the sweep purges.

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
