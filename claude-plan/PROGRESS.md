# Progress — PR/MR summary + walkthrough comment

Active plan (branch `feat/pr-summary-claude`). Standards retrieved at plan time via
OutcomeOps MCP and inlined into each chunk (ADR-004, ADR-025, ADR-032, ADR-035/016,
ADR-036/050, ADR-003/005, ADR-013). Chunks are self-contained.

## Chunks
- [x] 01 — summary builder (`lambda/analyze-pr/pr_summary.py`): DONE. Model prose +
      code-supplied per-file table + checks line + usage captured; model failure degrades
      to deterministic table (non-fatal); diffs capped with ADR-032 truncation signal.
- [x] 02 — wire as `COMMENT#summary` in analyze-pr: DONE. All 3 provider flows upsert the
      summary just above analysis-started (checks-will-run path only, so no Bedrock spend on
      no-check PRs). GitHub feeds inline `.patch`; ADO/GitLab fetch bounded diffs via
      `_fetch_diffs_via_sha` (cap `_SUMMARY_MAX_DIFF_FILES`=12, non-fatal per file). Collapsed
      token footer via `_render_summary_footer`.
- [~] 03 — tests DONE; live parity PENDING (needs deploy). Unit: test_pr_summary.py (9) +
      test_analyze_pr_summary_wiring.py (8); pr_summary.py at 100% coverage; full analyze-pr
      suite green (147). Live 3-SCM parity on GitHub PR / GitLab MR / ADO PR = after deploy.

## Execution notes
- Codebase overrode the Oct-5 summary: real upsert helpers `_upsert_{github,ado,gitlab}_comment`
  are already role-parameterized (just added `COMMENT_ROLE_SUMMARY`); model tier is
  `workload="pr-analyzer", tier="basic"`; `PRMetadata.file_change_types` makes the table +
  count fully code-supplied.
- Design choice: summary scoped to the checks-will-run path (overview "keep cost trivial").
- One existing test updated (`test_handler_success`: success path now posts 2 comments).
- Test-isolation fix: wiring test loads handler under a distinct sys.modules name so it does
  not clobber `analyze_pr_handler` that test_analyze_pr.py relies on.

## Deploy (for chunk 03 live parity)
- Artifact: Lambda zip code only (`lambda/analyze-pr/`). No Terraform/IAM change (reuses the
  existing COMMENT# upsert path + posters; analyze-pr role already has the perms).
- Env (optional): `BEDROCK_PR_ANALYZER_BASIC_PROFILE_ARN` (or legacy `BEDROCK_BASIC_MODEL_ID`)
  must resolve for the model tier; without it the builder degrades to the deterministic table
  (table still posts, no prose). `PR_SUMMARY_MAX_DIFF_FILES` / `PR_SUMMARY_MAX_PATCH_BYTES_*`
  tune bounds.
- Verify: `aws lambda get-function-configuration --function-name prd-outcome-ops-ai-assist-analyze-pr --query CodeSha256`.

## Sequence
01 -> 02 -> 03 (short pipeline; little parallelism).

## Ground rules
- No fabricated counts (ADR-032). Summarize the diff, not the PR description (ADR-025).
- Parity is not optional (ADR-036/050).
- Done = exercised: unit for 01/02, real PR/MR per provider for 03; URLs recorded.

## Ship (post-chunks)
- Three-Surface: docs/features.md + marketing feature list + Confluence 57835580.
- ADR for the summary-comment contract under docs/adr/.
