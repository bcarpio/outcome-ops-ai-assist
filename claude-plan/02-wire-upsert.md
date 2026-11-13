# 02 — Wire as COMMENT#summary (3-SCM, cost, truncation)

## Task
Call the chunk-01 builder from `lambda/analyze-pr/handler.py` and upsert its output as a
new comment role `COMMENT#summary`, in all three provider flows, so it posts on open and
PATCHes in place on re-push. Depends on 01.

## Steps (grounded in the real handler)
1. Add the role constant next to the others at L485–487:
   ```python
   COMMENT_ROLE_SUMMARY = "summary"
   ```
2. In each provider flow, right where `analysis_started` is posted, build the diffs and
   post the summary FIRST (so it is the top comment), then the analysis-started status:
   - GitHub flow (~L1324): has `repository`, `pr_number`, metadata, `checks_to_run`, token.
   - ADO flow (~L1569, see `_post_ado_initial_comment` L1406): ADO context +
     `connection_id`.
   - GitLab flow (~L1727): `connection_id`, full project path as `repository`.
3. Fetch per-file diffs with `shared.pr_diff.compute_file_diff(...)` for the changed files
   (cap bytes per chunk 01), assemble `diffs_by_file` + `truncated_files`, then:
   ```python
   body, usage = build_pr_summary(metadata=meta, diffs_by_file=diffs,
                                  checks_to_run=checks_to_run, truncated_files=truncated)
   body = body + _render_usage_footer(usage, tier="basic")  # collapsed <details>, like checks
   ```
4. Upsert via the existing per-provider helper with the new role — each already does
   PATCH-if-`providerCommentId`-else-POST and writes the row via `_store_comment_row`:
   ```python
   _upsert_github_comment(..., role=COMMENT_ROLE_SUMMARY, body=body)   # or _upsert_ado_comment / _upsert_gitlab_comment
   ```
   `PK=_build_comment_pk(provider, repository, pr_number)`, `SK=COMMENT#summary`. The DDB
   write goes through `shared.dual_write` inside `_store_comment_row` — do not add a second
   write path.
5. Append the collapsed usage/cost section exactly like the check comments do (model tier,
   input/output tokens, USD), so cost stays transparent. Reuse the existing footer rendering
   if one is already shared; otherwise mirror the checks' format.

## Standards to apply (inlined verbatim)

### ADR-036 / ADR-050 — cross-SCM parity (NOT optional)
From ADR-050 v1.2: the checks run on GitLab MRs "with the same collapsed-`<details>` +
upsert-in-place comment UX as GitHub/ADO (MR notes; note id stored in the same
`providerCommentId` column)." From ADR-036: every check "dispatches on `provider`" and
produces "the same ... result set" across providers. Applied here: the summary comment must
land, update-in-place, and render identically across GitHub, GitLab, and ADO, using each
provider's existing poster and the shared `providerCommentId` upsert column. Do not special-
case one provider's body. The `_upsert_{github,ado,gitlab}_comment` helpers already
normalize the POST-vs-PATCH decision — use them; do not hand-roll a fourth path.

### ADR-004 — error handling (summary must be non-fatal)
Wrap the whole summary build+post in try-except with `exc_info=True` and continue. The
summary is additive reviewer context, exactly like the structural-impact block which is
explicitly "non-fatal -- the structural pass is additive context" (handler comment at the
structural call site). A model error, a diff-fetch error, or a poster error must NOT block
the check-queueing pipeline or fail the SQS message. Log and move on.

### ADR-032 — truncation signalling survives to the posted comment
The truncation signal produced in chunk 01 must reach the posted body (do not strip it when
assembling the final comment). A capped diff shows the reviewer a truncation note, not a
silently-partial walkthrough.

## Order / placement
Post the summary before (or alongside) the analysis-started status so it reads as the top
comment. On re-run, the same `providerCommentId` is reused (PATCH), so there is exactly one
summary comment per PR that stays current — zero duplicates.

## Done when
- Opening a PR posts a summary comment above the analysis-started status, on all three
  providers (unit-level here; live in chunk 03).
- Re-push PATCHes the same comment id (no duplicate).
- A model/diff/poster failure logs and is swallowed; checks still queue.
- Cost footer renders from the real usage object.
