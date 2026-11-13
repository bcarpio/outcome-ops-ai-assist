# OVERVIEW — PR/MR summary + walkthrough comment

## Goal
On PR/MR open, `analyze-pr` posts ONE upserted summary comment at the top of the
PR: a plain-English "what changed and why", a per-file changes table, and the list
of checks that will run. It PATCHes in place on re-push. First thing a reviewer
sees. Parity across GitHub / GitLab / Azure DevOps.

## Why (gap, audit 2026-10-05)
Today the only self-comment `analyze-pr` posts up front is
`_build_analysis_started_comment()` — a collapsed `<details>` status line listing
queued check *names*. It says nothing about the diff. The `_build_structural_impact_comment`
block exists but no-ops in prod (can't resolve `workspace_id`). There is no
human-readable "here is what this PR does" before the checks land.

## Scope
**In:**
- New builder module `lambda/analyze-pr/pr_summary.py` (chunk 01).
- Wire it as a new upsert role `COMMENT#summary` in all three provider flows in
  `lambda/analyze-pr/handler.py` (chunk 02).
- Unit tests + 3-SCM live parity (chunk 03).

**Out (do NOT do):**
- No new Terraform / IAM / resource. The DynamoDB write path (`_store_comment_row`
  via `shared.dual_write`) and the three provider comment posters already exist and
  already write `COMMENT#` rows for `analysis_started`. `analyze-pr`'s role already
  has the needed DynamoDB + poster permissions.
- No new SCM client capability. Issue/MR/thread-level comment only — the existing
  posters cover it.
- Do NOT revive or depend on the structural-impact pass (it no-ops without a
  workspace_id; orthogonal to this work).

## Grounded insertion points (codebase wins over the Oct-5 summary's line numbers)
In `lambda/analyze-pr/handler.py`:
- Role constants at L485–487 (`COMMENT_ROLE_ANALYSIS_STARTED` etc.) — add
  `COMMENT_ROLE_SUMMARY = "summary"`.
- `_build_comment_pk(provider, repository, pr_number)` (L529) → `PK`.
- `_get_existing_comment_row(pk, sk)` (L534), `_store_comment_row(...)` (L549).
- Upsert helpers, one per provider, each already role-parameterized and each already
  PATCH-if-`providerCommentId`-else-POST:
  - `_upsert_github_comment(...)` L582
  - `_upsert_ado_comment(...)`   L629
  - `_upsert_gitlab_comment(...)` L703
- Post sites to slot the summary next to (same spot `analysis_started` posts):
  GitHub flow ~L1324, ADO flow ~L1569 (`_post_ado_initial_comment` L1406),
  GitLab flow ~L1727.

Inputs already available (no new fetch capability):
- `shared.pr_metadata.PRMetadata` (NamedTuple): `head_sha, base_sha, base_branch,
  changed_files, author, title, file_change_types` where `file_change_types` maps
  each changed file to GitHub-style status `added|modified|removed|renamed`.
- `shared.pr_diff.compute_file_diff(...)` for per-file unified-diff text.
- `shared.model_client.invoke(*, workload="pr-analyzer", tier="basic", messages=...)`
  returns `resp.text`, `resp.usage.input_tokens`, `resp.usage.output_tokens`.
  Valid workloads: `{chat, code-maps, code-gen, pr-analyzer}`; valid tiers:
  `{advanced, basic, titan-embed, cohere-rerank}`. `basic` is the Haiku-class cheap
  tier — the right choice for a summary (not deep reasoning).

## Chunks & parallelization
- **01 — summary builder** (`pr_summary.py`). Independent. Pure function + one model
  call; no handler edits. START HERE.
- **02 — wire as COMMENT#summary** in the 3 provider flows. DEPENDS ON 01 (imports the
  builder).
- **03 — tests + parity**. DEPENDS ON 01 and 02 (tests both the builder and the wiring;
  live parity exercises the deployed path).

Sequence: 01 → 02 → 03. Little parallelism (02 wires 01; 03 tests both), so this runs
as a short pipeline, not a fan-out.

## Ship (after chunks land)
- **Three-Surface Rule** (this is a user-visible feature): update `docs/features.md`,
  `../marketing-for-outcomeops/outcomeops-feature-list.md`, and Confluence page
  57835580 in the same session.
- **ADR:** the summary-comment contract (summarize-the-diff grounding + code-supplied
  file table, no LLM counts) is a durable decision → capture it as an ADR under
  `docs/adr/` rather than leaving the rationale only in the commit.
- Pre-commit: `make test-unit`; `make fmt`/`make validate-tf` if any `.tf` (none
  expected); `make audit`. py_compile touched Python.

## Ground rules
- No aspirational checkmarks: each chunk is DONE only when exercised (unit for 01/02,
  real PR/MR per provider for 03). Record URLs in PROGRESS.
- Parity is not optional (ADR-036/050).
- No fabricated counts; summarize the diff, not the PR description (ADR-032 / ADR-025,
  inlined in the chunks).
