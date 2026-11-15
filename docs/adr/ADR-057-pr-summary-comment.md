# ADR-057: PR/MR Summary Comment (Upserted Prose Walkthrough, 3-SCM Parity)

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
  check output; the summary must hold the same line. The conformant answer here
  is **not** to render a code-built file table alongside the SCM's own one (that
  is pure duplication) but to render **no file list or count at all** and keep
  the model to prose that does not count -- the SCM already shows the
  authoritative changed-file list and statuses natively.
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

**`analyze-pr` posts exactly one upserted `COMMENT#summary` per PR: a model
prose walkthrough of the diff plus the queued-checks line, across all three
SCMs, only on the path where checks will run. The comment renders no file list
or count of its own -- the SCM shows those -- and the model is held to prose and
told not to count (ADR-032).**

### Key Points

- **Role-keyed, upserted, posted before `analysis_started`.** The summary is a
  new comment role (`COMMENT#summary`) alongside the existing analysis-started /
  per-check roles. It reuses the role-parameterized upsert helpers
  (`_upsert_{github,ado,gitlab}_comment`) and the companion DDB row
  (`PK=PR#{provider}#{repository}#{pr_number}`, `SK=COMMENT#summary`). The row
  stores the provider's own identifier(s) -- `providerCommentId` for
  GitHub/GitLab, `providerThreadId`+`providerThreadCommentId` for ADO -- so a
  re-run **PATCHes the same comment in place** rather than posting a duplicate.

- **Prose only; no redundant structure (ADR-025, ADR-032).** The diff is passed
  to the model as primary evidence and the changed-file list as grounding
  context, but the model is instructed to write ONE concise paragraph and to
  **not count files, enumerate names, or state totals**. The comment renders
  that prose and nothing structural -- no table, no "N files changed" line --
  because the SCM already shows the authoritative file list and statuses. ADR-032
  is held two ways: the model is told not to author aggregates, and the comment
  shows no aggregate for it to get wrong. Crucially there is **no post-hoc scrub**
  of the model's prose (that is the anti-pattern ADR-032 marks non-conformant);
  if the wording needs tightening the fix is the prompt + few-shot (ADR-027),
  not output filtering.

- **Degrade, don't fail (ADR-004).** The model call is wrapped: on any failure
  the comment still posts with the queued-checks line and an honest fallback --
  "Summary unavailable for this revision" when the model call errored (`usage`
  is `None`), or "too large to summarize in prose; review the changed files
  directly" when the call returned but yielded no usable prose. The checks line
  is the floor; the prose is the bonus.

- **Bounded diffs, with truncation signalled (ADR-032).** Diffs are capped
  per-file (`PR_SUMMARY_MAX_PATCH_BYTES_PER_FILE`) and in total
  (`PR_SUMMARY_MAX_TOTAL_PATCH_BYTES`). When any file's diff is capped, omitted,
  or failed to fetch, the comment carries a code-computed note -- "diffs for N
  file(s) were too large to include" -- so the reviewer knows the prose may not
  cover every change. That `N` is derived in code from the omission set, never
  authored by the model.

- **Two diff sources, one contract.** GitHub supplies file patches inline, so the
  summary consumes `GitHubFile.patch` directly. GitLab and ADO do not, so the
  summary fetches bounded per-file diffs via `_fetch_diffs_via_sha`
  (`shared.source_fetch.fetch_file_at_sha` + `shared.pr_diff.compute_file_diff`),
  non-fatal per file. Both paths feed the identical builder.

- **Cost scoped to where it is read.** The summary is built only on the branch
  where checks will actually run. A PR that queues no checks calls no model and
  posts no summary. The model runs at `workload="pr-analyzer", tier="advanced"`
  via the shared `model_client` (ADR-035): the summary is a high-visibility,
  every-PR comment, so it is worth the Sonnet-class tier rather than the cheap
  one. The advanced model id is tfvar-driven (`bedrock_advanced_model_id`, or
  the pr-analyzer advanced app-inference profile), swappable per customer to
  e.g. Opus -- nothing is hardcoded. Captured token usage is rendered in a
  collapsed footer (ADR-016 workload cost accounting).

### Example Implementation

```python
# lambda/analyze-pr/pr_summary.py -- the builder. The model writes prose only;
# the comment renders no file list/count (the SCM shows it). The one count it
# emits (the truncation note) is code-computed (ADR-032).
markdown, usage = build_pr_summary(
    metadata=pr_metadata,            # PRMetadata: changed_files for the truncation count
    diffs_by_file=diffs,             # GitHub .patch, or _fetch_diffs_via_sha (GL/ADO)
    checks_to_run=checks,            # rendered as the "Checks queued" line
    truncated_files=truncated,       # ADR-032 omission signal
)
# usage is None when the model call errored; the checks line + fallback still post.
```

```
## PR summary

<model prose: one concise paragraph on what the diff does; no counts, no file list>

**Checks queued:** ADR_COMPLIANCE, TEST_COVERAGE, ...

> Note: diffs for N file(s) were too large to include, so the summary above
> may not cover them.          <- only when some diff was capped; N is code-computed

<details><summary>Usage</summary> model tier / input+output tokens </details>
```

## Consequences

### Positive

- Reviewers get a diff-grounded orientation before the findings, identical in
  shape on GitHub, GitLab, and ADO.
- Nothing structural is duplicated: the comment adds the one thing the SCM does
  not already show -- a plain-English "what this does" plus which checks are
  running -- instead of re-listing files the PR view already lists.
- The model is kept from authoring aggregates by construction (prose-only,
  instructed not to count, nothing structural rendered), with no post-hoc scrub.
- A model outage still posts the checks line and an honest fallback instead of
  dropping the comment or failing the run.
- Re-running analysis never clutters the PR with duplicate summaries.

### Tradeoffs

- The prose is only as good as the diff bounds allow; on a very large PR the
  model sees truncated diffs and the note says so. Raising the caps raises token
  cost -- revisit the defaults if summaries on big PRs read as too shallow.
- Relying on the prompt (not a renderer table) to keep the model from counting
  means a disobedient model could still slip a number into the prose. The guard
  is prompt wording + few-shot (ADR-027), re-tuned if it misfires -- never a
  post-hoc scrub, which ADR-032 bans.
- The summary is scoped to the checks-will-run path by design. If a future use
  case wants a summary on check-less PRs, that is a deliberate cost decision to
  reopen here, not a bug.

## Addendum: known hardening items (not yet in production)

These surfaced in review and live testing after the initial build. None is a
correctness defect in the ADR-032 posture -- the comment renders nothing
structural (the SCM shows the file list), so there is no code-authored aggregate
to be wrong. Item 3 is now **resolved**; items 1 and 2 remain **open** latency /
abuse-surface items to close before this runs against real customer PR volume.

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
   markdown as-is. Injection cannot fabricate structure -- the comment renders no
   file list or count to spoof -- so the surface is the free-text prose only: a
   crafted title or diff could steer the walkthrough, or emit markdown
   (links/images) that renders live in the comment. The diff-is-truth system
   prompt blunts this but does not remove it, and the title is not length-capped.
   **Fix to consider:** cap the title length fed to the model, and neutralize
   active markdown in the prose (render it inside a blockquote, or strip
   link/image syntax) so injected content cannot render as live links or images.

3. **RESOLVED -- large-PR prose collapse.** The earlier design asked the model
   for a prose paragraph *plus* one note per file; on a large PR that per-file
   output blew the 700-token cap and the prose collapsed entirely to "Summary
   unavailable." Observed live on a 54-file GitLab MR and a 54-file ADO PR (both
   pinned at `output: 700`), with the file table the only thing that rendered.
   Fixed by holding the model to **prose only** (no per-file notes to bloat
   output -- which also removed the redundant table) and raising the default cap
   to **1500**; a successful-but-empty response now falls back to an honest "too
   large to summarize in prose; review the changed files directly" rather than
   the misleading "unavailable." The cap stays env-tunable via
   `PR_SUMMARY_MAX_OUTPUT_TOKENS`.

## Implementation

### Starting today

1. `pr_summary.build_pr_summary` owns prose assembly + model degradation; it
   renders no file table, only the prose, the checks line, and (when a diff is
   capped) the code-computed truncation note.
2. `analyze-pr/handler.py` wires `COMMENT#summary` into all three provider flows
   just above `analysis_started`, on the checks-will-run path only.
3. `analyze-pr` has Bedrock `InvokeModel` + `BEDROCK_ADVANCED_MODEL_ID` (and
   `BEDROCK_BASIC_MODEL_ID`) so the advanced tier resolves via the tfvar-driven
   model id; without it the comment degrades to the checks line + fallback.

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

- v1.0 (2026-11-13): Initial design -- prose plus a code-supplied per-file table
  and count. Live-verified across GitHub PR, GitLab MR, and ADO PR.
- Prose-only revision (2026-11-15): removed the per-file table and per-file notes
  (the SCM already shows the file list, so the table was redundant) and held the
  model to a single prose paragraph; raised the output cap 700 -> 1500 after the
  per-file-notes design collapsed the prose to "Summary unavailable" on large
  (54-file) GitLab/ADO diffs. Live-verified 2026-11-15 on the same 54-file GitLab
  MR and ADO PR: real diff-grounded prose returned (output ~860 tokens, no
  collapse), no file table, truncation note code-computed. GitHub uses the same
  builder via the inline-patch path.

<!-- Confluence sync -->
