# ADR-029: Repository-Deletion Completeness Contract

## Status: Accepted

## Context

Removing a GitHub repository from a workspace must erase that repo's
entire footprint. A single repo spreads across several stores, and
under the active/passive multi-region topology (ADR-022) each store
exists independently in **both** regions:

- **Knowledge-base S3**: documents under
  `workspaces/{ws}/github/{owner}/{repo}/` AND a legacy bare-name prefix
  `workspaces/{ws}/github/{repo}/` (the architectural summary is written
  under the no-owner key).
- **S3 Vectors**: per-document chunk vectors keyed
  `{ws}#github#{owner}#{repo}#...` and code-map summary vectors keyed
  `{ws}#github#{repo}#...` (no owner segment).
- **Symbol-graph DDB** (`symbol-graph` table): facts keyed
  `PK = {ws}#{repo}` using the **bare** repo name (no owner).
- **Code-maps DDB** (`code-maps` table): the SHA-truth pointer row
  `PK = SYMBOL_GRAPH#{ws}#{repo}`, `SK = POINTER`.

Incomplete deletion was observed repeatedly in the sandbox: deletes
left symbol-graph facts, code-maps pointers, and bare-name KB objects
behind — most often in the **passive** region, because the original
delete path only cleaned the active region and only some stores. Leaked
facts pollute structural queries and any later re-add; a leaked pointer
breaks SHA-truth/incremental logic on re-ingest. Two distinct removal
entry points exist and must behave identically:
`workspace-management.remove_repo` and
`github-integration.handle_delete_repo` (the path the UI uses).

A further failure mode masked the above: the delete-worker caught
per-record exceptions, logged them, and still returned an SQS success,
so a failed delete was silently dropped — neither retried nor sent to
the DLQ — leaving a region permanently dirty with nothing to retry.

## Decision

A repository delete MUST satisfy the following contract.

### 1. Every store, every region

A delete enqueues cleanup for **all** stores and fans out to **both**
regions' delete queues (the secondary target is derived from its SQS
URL; single-region deployments no-op on the empty secondary URL). Both
removal entry points construct the same message set via the shared
dispatcher, so the UI path and the management path are identical:

- `delete_prefix` for the owner-prefixed KB prefix,
- `delete_prefix` for the bare-name KB prefix (when it differs),
- `delete_symbol_graph` for the symbol-graph facts + the code-maps
  pointer (keyed on the bare repo name).

Each region's delete-worker cleans **its own** region's stores; the
cross-region propagation is the dual-send of the delete messages, not a
managed replication of the deletes.

### 2. Post-delete self-verification

After cleaning, the worker re-checks the stores it just touched in its
own region and logs the result:

- `delete_symbol_graph` re-counts the partition and re-reads the
  pointer; logs `VERIFY clean` or a `VERIFY FAILED` **warning** with the
  remaining count.
- `delete_prefix` re-lists the prefix; logs `verified_clean` with the
  remaining object count.

`shared.delete_verify.check_repo_clean` is the reusable, region-scoped
enumerator behind on-demand attestation and backfill sweeps. It reports
symbol-graph facts, the code-maps pointer, and owner- and bare-name KB
object counts. (S3 Vectors emptiness is not yet asserted here; verifying
a vector index by prefix is a separate follow-up. The high-value leaks
were in the DDB stores and KB objects.)

### 3. Failures retry, never drop

The delete-worker reports `batchItemFailures` and both delete-worker
event source mappings enable `ReportBatchItemFailures`. Any record that
raises, returns a non-`success`/non-`skipped` status, or fails to parse
is returned to SQS so it redrives and lands in the DLQ after
`maxReceiveCount`. Silent success on a failed delete is forbidden — a
dirty store must always leave a retryable trace.

### 4. IAM must cover the access pattern, not just the obvious verbs

The symbol-graph cleanup uses `Query` + `BatchWriteItem` (batch fact
delete) on the symbol-graph table and `GetItem` + `DeleteItem` on the
code-maps table (pointer read-back + delete). Both delete-worker roles
(primary and secondary) must grant all four. A policy that grants only
`Query` + `DeleteItem` passes review but fails at runtime on the batch
delete and the verify read — and, combined with rule 3, that failure
must surface via retry/DLQ rather than vanish.

## Consequences

- A freshly-deployed delete leaves both regions clean and emits a
  `VERIFY clean` line per region; a regression is loud (warning +
  retry/DLQ) instead of silent.
- Pre-existing leaked data from deletes that predate this contract is
  not self-healing. It is cleaned by a one-shot backfill sweep built on
  `check_repo_clean`, run by an operator against the sandbox and then
  discarded. Customer deployments that must perform the same catch-up
  treat this ADR as the spec.
- Two GitHub repos in one workspace whose bare names collide on a final
  path segment (e.g. `alice/myrepo` and `bob/myrepo`) would share a
  code-map vector prefix; acceptable for single-customer workspaces
  today, to be revisited if it surfaces.
- This contract governs **deletion** completeness only. It does not
  address write-time replication drift (e.g. a live repo whose pointer
  exists in one region but not the other); that belongs to the
  dual-write path under ADR-022.
