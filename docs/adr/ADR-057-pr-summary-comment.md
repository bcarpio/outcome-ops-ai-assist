# ADR-057: PR/MR Summary Comment (Upserted, Code-Supplied Table, 3-SCM Parity)

## Status: Accepted

## Context

When `analyze-pr` runs its checks on a pull/merge request, the reviewer sees a
stream of per-check result comments (ADR / README / test-coverage /
breaking-change / duplication / license) plus an "Analysis Started" marker. What
was missing was a single, human-readable **summary of what the change actually
does** -- the thing a reviewer reads first to orient before reading findings.

A summary comment is easy to get wrong in ways the rest of the product has
already ruled out elsewhere:

- **Fabricated counts and files.** A model asked to "summarize this PR" will
  happily state "this PR changes 12 files" and invent file paths. ADR-032
  (list-completeness / no model-authored aggregates) already forbids this for
  check output; the summary must hold the same line.
- **Summarizing the description instead of the diff.** ADR-025 established that
  the evidence is the diff, not the PR title/body (which may be empty, stale, or
  aspirational). The ADO run in testing proved the point: the model flagged that
  the PR *title* contradicted the handler's *docstring*.
- **Provider drift.** GitHub, GitLab, and ADO each model comments differently
  (flat issue comment vs. discussion note vs. thread+comment). ADR-036/050
  requires feature parity across all three SCMs, not a GitHub-only nicety.
- **Cost.** A summary that calls a model on *every* PR event -- including PRs
  where no checks will even run -- is spend with no reader.

## Decision

**`analyze-pr` posts exactly one upserted `COMMENT#summary` per PR, built from a
model prose summary of the diff plus a code-supplied per-file table, across all
three SCMs, only on the path where checks will run.**

### Key Points

- **Role-keyed, upserted, posted before `analysis_started`.** The summary is a
  new comment role (`COMMENT#summary`) alongside the existing analysis-started /
  per-check roles. It reuses the role-parameterized upsert helpers
  (`_upsert_{github,ado,gitlab}_comment`) and the companion DDB row
  (`PK=PR#{provider}#{repository}#{pr_number}`, `SK=COMMENT#summary`). The row
  stores the provider's own identifier(s) -- `providerCommentId` for
  GitHub/GitLab, `providerThreadId`+`providerThreadCommentId` for ADO -- so a
  re-run **PATCHes the same comment in place** rather than posting a duplicate.

- **The prose summarizes the diff; the model is advisory, never authoritative
  about structure (ADR-025, ADR-032).** The diff is passed to the model as
  primary evidence. The model's prose is rendered as-is, but the **per-file
  table and the file count are built entirely from code** --
  `PRMetadata.file_change_types` supplies the rows and their statuses, and the
  count is `len(changed_files)`. A path the model hallucinates in its FILE NOTES
  is filtered out (it is not in `changed_files`, so it cannot become a row); a
  count the model asserts in prose is ignored by the table header. Pipes in
  paths/notes are escaped so a crafted filename cannot break the table.

- **Degrade, don't fail (ADR-004).** The model call is wrapped: on any failure
  the summary still posts, carrying the deterministic file table and a "Summary
  unavailable" note. The table is the floor; the prose is the bonus.

- **Bounded diffs, with truncation signalled (ADR-032).** Diffs are capped
  per-file (`PR_SUMMARY_MAX_PATCH_BYTES_PER_FILE`) and in total
  (`PR_SUMMARY_MAX_TOTAL_PATCH_BYTES`), and the file count is capped
  (`PR_SUMMARY_MAX_DIFF_FILES`). Any file whose diff is capped, omitted, or
  failed to fetch is still rendered as a table row and the summary carries a
  "truncated or omitted" note -- never a silently-partial walkthrough.

- **Two diff sources, one contract.** GitHub supplies file patches inline, so the
  summary consumes `GitHubFile.patch` directly. GitLab and ADO do not, so the
  summary fetches bounded per-file diffs via `_fetch_diffs_via_sha`
  (`shared.source_fetch.fetch_file_at_sha` + `shared.pr_diff.compute_file_diff`),
  non-fatal per file. Both paths feed the identical builder.

- **Cost stays trivial.** The summary is built only on the branch where checks
  will actually run. A PR that queues no checks calls no model and posts no
  summary. The model runs at `workload="pr-analyzer", tier="basic"` via the
  shared `model_client` (ADR-035), and the captured token usage is rendered in a
  collapsed footer (ADR-016 workload cost accounting).

### Example Implementation

```python
# lambda/analyze-pr/pr_summary.py -- the builder. The table and count are
# code-supplied from metadata; the model never authors structure (ADR-032).
markdown, usage = build_pr_summary(
    metadata=pr_metadata,            # PRMetadata: changed_files + file_change_types
    diffs_by_file=diffs,             # GitHub .patch, or _fetch_diffs_via_sha (GL/ADO)
    checks_to_run=checks,            # rendered as the "Checks queued" line
    truncated_files=truncated,       # ADR-032 omission signal
)
# usage is None when the model degraded; the deterministic table still rendered.
```

```
## PR summary

<model prose: what the diff does -- advisory>

**Files changed (N):**        <- N = len(changed_files), code-supplied

| File | Change | Note |
| ---- | ------ | ---- |
| `path` | added/modified/removed/renamed | <in-range model note, or diff omitted> |

**Checks queued:** ADR_COMPLIANCE, TEST_COVERAGE, ...

<details><summary>Usage</summary> model tier / input+output tokens </details>
```

## Consequences

### Positive

- Reviewers get a diff-grounded orientation before the findings, identical in
  shape on GitHub, GitLab, and ADO.
- The structural claims (which files, how many, what status) are trustworthy by
  construction -- they come from the SCM's own change list, not the model.
- A model outage downgrades the summary to a plain table instead of dropping it
  or failing the run.
- Re-running analysis never clutters the PR with duplicate summaries.

### Tradeoffs

- The prose is only as good as the diff bounds allow; on a very large PR the
  model sees truncated diffs and the note says so. Raising the caps raises token
  cost -- revisit the defaults if summaries on big PRs read as too shallow.
- The summary is scoped to the checks-will-run path by design. If a future use
  case wants a summary on check-less PRs, that is a deliberate cost decision to
  reopen here, not a bug.

## Addendum: known hardening items (not yet in production)

These surfaced in review after the initial build and are **open**. None is a
correctness defect in the governed contract -- the per-file table and the count
are code-supplied and trustworthy by construction (ADR-032) regardless of any of
the below. All three are latency / abuse-surface items to close before this runs
against real customer PR volume.

1. **The summary sits on the check-dispatch critical path.** In all three
   provider flows the summary is built and posted *before* `queue_check_jobs`
   (GitHub, ADO, and GitLab paths in `handler.py`). The model call
   (`pr_summary._invoke_model`) is synchronous, and for ADO/GitLab it is preceded
   by `_fetch_diffs_via_sha`, which fetches base+head bytes **serially, one file
   at a time** -- up to 2xN sequential round-trips before any check is queued. So
   summary latency delays when checks start. It does **not** drop checks: each
   call site wraps the whole summary in try/except and `queue_check_jobs` runs
   after that block, so a model or fetch failure/timeout is caught and checks
   still queue. The exposure is added wall-clock on the dispatch path, worst on
   large ADO/GitLab PRs. **Fix to consider:** queue checks first and post the
   summary afterward, or move the summary off the request path entirely
   (fire-and-forget / separate invocation); parallelize the per-file SHA fetches
   if the summary stays inline.

2. **Untrusted PR title and diff feed the model; the model's prose is rendered
   unescaped.** The PR title (`_build_user_prompt`) and the diff are both
   author-controlled on any PR, and the model's prose is written into the comment
   markdown as-is. The structural surface is already safe: the table, the count,
   and the per-file notes stay code-owned, and `_parse_model_output` drops any
   note whose path is not in `changed_files`, so injected text cannot fabricate
   files, counts, or statuses. The residual surface is the free-text prose only
   -- a crafted title or diff could steer the walkthrough, or emit markdown
   (links/images) that renders live in the comment. The diff-is-truth system
   prompt blunts this but does not remove it, and the title is not length-capped.
   **Fix to consider:** cap the title length fed to the model, and neutralize
   active markdown in the prose (render it inside a blockquote, or strip
   link/image syntax) so injected content cannot render as live links or images.

3. **`PR_SUMMARY_MAX_OUTPUT_TOKENS` (default 700) can clip the prose.** If the
   model hits the output cap the walkthrough is cut mid-sentence and the parser
   renders whatever arrived. This is cosmetic, not fail-open: a summary carries no
   verdict, and the table, count, checks line, and truncation note are all
   code-supplied and always render, so a clipped paragraph never yields a wrong or
   misleadingly-partial *structural* claim. (Contrast the earlier check-output
   truncation class, where a truncated model *verdict* could silently pass -- a
   summary has no such pass/fail to corrupt.) **Fix to consider:** leave as-is, or
   raise the cap only if real summaries read as clipped; if raised, keep the
   deterministic table as the floor.

## Implementation

### Starting today

1. `pr_summary.build_pr_summary` owns prose+table assembly and model degradation.
2. `analyze-pr/handler.py` wires `COMMENT#summary` into all three provider flows
   just above `analysis_started`, on the checks-will-run path only.
3. `analyze-pr` has Bedrock `InvokeModel` + `BEDROCK_BASIC_MODEL_ID` so the basic
   tier resolves; without it the builder degrades to the table.

### Next phases

1. Optional: surface the summary's token spend in the same Cost-Explorer
   workload view as the checks (already tagged `pr-analyzer`).
2. Optional: tune `PR_SUMMARY_MAX_*` per-customer if large-PR summaries read thin.

## Related ADRs

- ADR-025: Summarize the diff, not the PR description
- ADR-032: List-completeness; no model-authored aggregates
- ADR-035 / ADR-016: Shared model client; workload cost accounting
- ADR-036 / ADR-050: Multi-SCM (GitHub/GitLab/ADO) feature parity
- ADR-053: CI-triggered PR analysis (the invoke path this rides on)
- ADR-004: Degrade gracefully; a non-essential enrichment must not fail the run

## References

- `lambda/analyze-pr/pr_summary.py` -- the builder
- `lambda/analyze-pr/handler.py` -- `COMMENT#summary` wiring, `_fetch_diffs_via_sha`
- `lambda/tests/unit/test_pr_summary.py`,
  `lambda/tests/unit/test_analyze_pr_summary_wiring.py`

Version History:

- v1.0 (2026-11-13): Initial decision. Live-verified across GitHub PR, GitLab MR,
  and ADO PR.

<!-- Confluence sync -->
