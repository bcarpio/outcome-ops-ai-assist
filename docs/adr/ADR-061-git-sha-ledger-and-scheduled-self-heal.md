# ADR-061: Git SHA ledger and scheduled self-heal for GitHub sync

## Status: Accepted

- v1.0 (2026-11-20) - Records how the platform knows which commit of a
  repository is actually indexed (the SHA ledger, previously undocumented)
  and the policy by which the hourly scheduled GitHub sync repairs a commit
  whose pipelines were dispatched but never finished: budgeted retries,
  no automatic full rebuild, advance past permanently failing files, and
  per-workspace notifications.

## Context

A repository row records two different facts that are easy to confuse:

- **What was sent.** `last_commit_sha` / `last_dispatched_sha` are written
  when github-sync DISPATCHES work for a commit.
- **What is done.** `last_docs_sha`, `last_code_map_sha` and `last_gkg_sha`
  (docs, code maps, symbol graph) are written only when every unit of that
  pipeline's work for the commit has completed.

Before this decision the hourly scheduled sync compared only the dispatched
SHA with HEAD. Two failures followed:

1. **A dead run was never retried.** If commit A was dispatched and its run
   died (GitHub outage, Bedrock throttling, messages in a DLQ), the next
   hourly run saw `last_commit_sha == HEAD` and skipped the repo every hour,
   forever.
2. **A silent data gap.** If commit B then arrived, the scheduled sync
   diffed A..B, so the files A changed were never processed. When B's batch
   completed, the completed columns jumped to B, and the repo reported
   "complete at B" while A's changes were missing from the knowledge base
   until those files happened to change again.

Separately, one file that failed permanently (for example a README in a
non-UTF-8 encoding) kept the completed SHA from advancing at all, so the diff
for that repo grew with every commit. Nothing read the failed-files table and
nobody was told.

Constraints: the platform runs in the customer's AWS account and is
single-tenant per account. Recovery must never turn into a retry storm or an
unplanned full re-ingest (cost and Bedrock quota), and a person who can fix
the cause has to hear about it.

## Decision

The completed-SHA columns are the source of truth for what is indexed, and
both manual and scheduled syncs diff from them. The scheduled sync heals a
lagging repo within a fixed budget, never rebuilds a repo on its own, and
moves past files that fail permanently after recording and reporting them.

### Key Points

- **The ledger gates the completed columns.** Before publishing SQS messages
  a dispatcher opens a pending-work row per pipeline and target SHA
  (`open_pending_batch`, idempotent). Each consumer calls
  `mark_unit_complete` or `mark_unit_failed` (the latter also writes a
  failed-files row, kept 30 days). The consumer whose update closes the batch
  calls `try_advance_sha`, which writes `last_<pipeline>_sha` only when
  completed + failed equals expected, with two guards: a monotonic
  commit-timestamp check (`last_<pipeline>_sha_ts < :ts`, so an older commit
  finishing late cannot overwrite a newer one) and `attribute_exists(PK)` (so
  a pipeline finishing after the repo was removed does not recreate the row).
  Code maps have two ledger stages (`code_map_dispatch`, `code_map_units`)
  and both must close.
- **Zero-work commits advance directly.** A commit that changes nothing a
  pipeline tracks opens no batch, so `advance_sha_without_work` writes the
  column with the same two guards. Otherwise "nothing to do" would look
  identical to "failed".
- **Manual sync ("Sync now").** Diffs from the completed baselines. It
  returns `up_to_date` when every completed SHA is at HEAD, and
  `already_running` when a live batch exists. A batch is live when it is
  open and made progress within the last hour; an open batch with no
  progress for an hour is dead and is redone. A manual sync resets the heal
  budget.
- **Scheduled sync uses completed baselines too.** This closes the A..B gap.
  For legacy rows with no completed column, it falls back to
  `last_commit_sha` so a deploy never triggers a mass first sync.
- **New commit (HEAD != `last_commit_sha`).** Dispatch from the completed
  baselines, unbudgeted (bounded by the commit rate), and reset the heal
  state.
- **Heal (HEAD == `last_commit_sha`, a completed SHA behind, no live batch).**
  Only when the scheduler granted a heal slot (`heal_allowed` on the SQS
  message) and within the per-target budget: at most
  `GITHUB_SYNC_HEAL_MAX_ATTEMPTS` (default 3) attempts per target SHA, with
  backoff of 1h, 2h, then 4h. State lives on the repo row as
  `heal_target_sha`, `heal_attempts`, `heal_next_at`.
- **Global throttle.** The scheduler grants at most
  `GITHUB_SYNC_HEAL_MAX_PER_RUN` (default 20) heal slots per hourly run,
  oldest eligible first. A deploy that finds 60 lagging repos heals them over
  three hours, not in one burst.
- **Never an automatic full rebuild.** If a heal's completed baseline is
  missing, or the GitHub compare from baseline to HEAD fails (force-push,
  unreachable SHA), nothing is dispatched. The repo shows `stalled` with
  reason `baseline_unreachable`; "Sync now" is the escape hatch.
- **Budget exhausted with a dead batch.** Status `stalled`, reason
  `heal_budget_exhausted`. No more automatic attempts for that target; a new
  commit or a manual sync resets the budget.
- **Poison units: advance past them.** When the budget is exhausted and the
  target's batches are CLOSED with `failed_count > 0`,
  `advance_past_failures` moves the completed column (for code maps it also
  advances the symbol graph) so the next diff does not grow without bound.
  The failed files stay in the failed-files table and the repo shows
  `stalled` with reason `units_failed`.
- **Notifications are per workspace.** Entering `stalled` (any reason) writes
  a `repo_stalled` row to the notifications table, listing up to 20 failed
  files with "and N more". Recipients are the workspace's admins plus the
  repo's `added_by` if that person is not already an admin. Visibility is
  computed at read time, so there are no per-user fan-out rows. The row id is
  a hash of workspace, repo, target SHA and reason, so repeats update one row;
  rows expire after 7 days and dismissal removes the row for everyone. There
  is no org-admin fan-out, SNS topic or email: platform admins of a
  single-tenant deployment do not need to hear that one team's README failed.
- **The two known permanent-failure causes are fixed at the source.**
  Non-UTF-8 document content no longer raises an uncaught `UnicodeDecodeError`
  that failed the whole sync every hour. File contents are now fetched at
  `current_sha` instead of by branch name, so the file list and the contents
  come from the same commit. The configured `default_branch` still decides
  which commit is synced; no other branch is read.
- **Scope.** GitHub only. GitLab and Azure DevOps can adopt the same policy
  later. DLQ redrive stays manual; the DLQ alarm remains the signal.

### Example

Repo `acme/payments`: commit A's code-map run dies in a Bedrock throttle.
`last_commit_sha` = A, `last_code_map_sha` = Z (the previous commit).

- Hour 1: the scheduler sees code maps behind and grants a heal slot.
  github-sync diffs Z..A, dispatches, `heal_attempts` = 1.
- Commit B lands at hour 2: a normal dispatch diffs Z..B (not A..B), so A's
  files are included, and the heal state resets.
- If one file in B fails every attempt, after three heals the batches are
  closed with `failed_count = 1`: the column advances to B, the repo shows
  `stalled` / `units_failed`, and the workspace admins see a notification
  naming that file.

## Consequences

### Positive

- "Complete at B" now means B and everything before it is indexed, except
  files listed in the failed-files table and reported to admins.
- Transient outages heal themselves within a few hours with no operator.
- Recovery cost is bounded: at most three attempts per target per repo and
  20 heals per hour across the deployment, and never a full rebuild.
- The people who can fix a bad file (workspace admins, whoever added the
  repo) are told, and nobody else is.

### Tradeoffs

- A file that fails permanently is skipped until it changes again or someone
  fixes it. That is a visible, recorded gap instead of an invisible one.
- A diff from an older completed baseline can be larger than the dispatched
  diff, so a heal may redo files that already succeeded. Work is idempotent.
- `stalled` repos need a human (fix the file, then "Sync now").

## Alternatives considered

- **Diff from the dispatched SHA.** Rejected: it is the A..B silent gap above.
- **Automatic DLQ redrive.** Rejected: replays stale messages for commits
  that have since been superseded, and a heal from the completed baseline
  already redoes the work for the current HEAD.
- **Retry forever.** Rejected: a poison file produces an hourly retry storm
  per repo with no end and no signal.
- **`allow_partial_advance` on the first failure.** Rejected: it hides
  transient failures that one more attempt would have fixed. Advancing past
  failures only after the budget is spent keeps both properties.

## Related ADRs

- ADR-022: Multi-region active/passive HA (ledger, row and notification
  writes go through dual-write).
- ADR-047: Symbol-graph-only regeneration.

## References

- `lambda/shared/sha_truth.py`: ledger, `try_advance_sha`,
  `advance_sha_without_work`, `advance_past_failures`, `batch_in_flight`
- `lambda/github-sync/handler.py`: baselines, heal budget, stalled reasons
- `lambda/github-scheduler/handler.py`: heal-slot grants
- `lambda/shared/notifications.py`: `notify_workspace`, `list_failed_files`

Version History:

- v1.0 (2026-11-20): Initial decision

<!-- Confluence sync -->
