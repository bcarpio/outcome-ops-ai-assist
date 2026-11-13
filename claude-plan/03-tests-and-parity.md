# 03 — Tests + 3-SCM parity

## Task
Unit tests for the builder (01) and the wiring (02), then live parity on a real GitHub PR,
GitLab MR, and ADO PR. Depends on 01 + 02.

## Unit tests (`lambda/tests/unit/test_pr_summary.py`, plus additions to `test_analyze_pr.py`)
Assert the correctness properties the standards demand:
- **Table == input file list (no invented files):** given a `PRMetadata` with a known
  `changed_files` + `file_change_types`, the rendered table has exactly those rows, each
  `Change` cell equal to the supplied status. No file appears that was not in the input;
  none is dropped.
- **No model-computed count:** the "N files" header equals `len(changed_files)`; assert it
  is not taken from model text (e.g. feed a model mock whose text claims a different number
  and assert the rendered count still matches the input length).
- **Truncation signal:** when `truncated_files` is non-empty (or patch bytes exceed the
  cap), the body contains the truncation note; when empty, it does not.
- **ADR-025 prompt shape:** assert the diff is passed as primary evidence (the prompt
  includes the diff text) and the title/description as secondary — a light assertion on the
  constructed messages is enough.
- **Model mocked + usage captured:** patch `shared.model_client.invoke` to return a fake
  response with `.text` and `.usage.{input_tokens,output_tokens}`; assert `build_pr_summary`
  returns that usage and that it called invoke with `workload="pr-analyzer", tier="basic"`.
- **Upsert PATCHes on second run:** with an existing `COMMENT#summary` row carrying a
  `providerCommentId`, a second post PATCHes that id rather than POSTing a new comment
  (assert the PATCH path per provider, mirroring existing `_upsert_*` tests).
- **Non-fatal:** when `build_pr_summary` raises, the handler still queues checks (assert the
  pipeline continues and the SQS message is not failed by the summary path).

## Standards to apply (inlined verbatim)

### ADR-003 / ADR-005 — testing standards
- Framework: **pytest** with pytest-cov. Tests live in the centralized `lambda/tests/`
  tree. Mark new tests `@pytest.mark.unit`.
- Coverage gate: **>= 80%** for the new code. "Don't chase 100% — focus on critical paths."
  Test negative conditions (bad inputs, error cases, exceptions) and assert error messages
  are useful.
- Mocking discipline: no real AWS calls — moto (`mock_aws`) for DynamoDB/S3/SSM; `MagicMock`
  / `patch.object` for Bedrock/model_client and SCM HTTP. **Stub at the module-cached
  attribute level the handler exposes, NOT at the `boto3.client` factory level** — matches
  the existing analyze-pr tests and stays robust under refactors. Each test resets any
  module-level cached client/state it touches so tests don't leak into one another.

### ADR-013 — bounded pagination loops in tests (MagicMock trap)
If the diff-fetch or any added path iterates a paginated API with a
`while True: ... if not last_key: break` loop, a bare `MagicMock` whose `.return_value` is
not configured makes the loop NEVER terminate (`MagicMock` is truthy by default), pegging
CPU and OOM-killing the suite (incident 2026-05-12). Any mock standing in for a paginator
MUST have its `return_value` explicitly configured so `LastEvaluatedKey`/next-page is falsy
on the final page. Prefer feeding a concrete fake over a bare `MagicMock` for anything read
in a loop.

## Live parity (DONE = exercised, URLs recorded here)
Reuse this session's fixtures and open PRs:
- **GitHub:** a synthetic PR on a fixture repo. Record URL.
- **GitLab:** MR on `bcarpio-group/ooai-license-server` (WS `OeNnXKZnUMIvnbNRgnO2LQ`,
  conn `1jDuhfOQAqBzveTrfmtJyQ`). Record URL.
- **ADO:** PR on `bcarpio/OutcomeOps/ooai-license-server` (WS `Xr1w8Dq0VuDPOacINwrZtw`,
  conn `OTvHyIjb6kvZEp9frgMYzQ`). Record URL.
For each: confirm the summary comment renders (prose + per-file table + checks-queued line +
cost footer), then re-push and confirm it PATCHes in place (same comment id, no duplicate).
Mind the SQS FIFO dedup window (5 min, keyed `repo|pr|head_sha|check_type`) — a re-push needs
a NEW head sha, and ADO's PR-iteration registration lags a push, so wait past the window.

## Done when
- Unit suite green at >= 80% on new code; `make test-unit` passes.
- Summary verified live on all three providers (URLs recorded), lands + PATCHes in place.
