# Fix Tests Command Implementation Status

## Current State

The `outcomeops: fix tests` command is fully implemented in code but not yet fully deployed.

## What's Been Done

### 1. handle-command Lambda (deployed)
- Added `FIX_TESTS` to `CommandType` enum
- Added `parse_command()` support for `fix tests` (with optional guidance)
- Added `fetch_test_analysis_comment()` to find analysis from PR comments
- Added `handle_fix_tests_command()` handler
- Added routing in `lambda_handler()` to dispatch to the handler
- Flow: Validates analysis exists, sends SQS message to generate-code

### 2. generate-code Lambda (deployed)
- Added routing for `fix_tests` action in `handle_sqs_event()`
- Created new `tests_fixer.py` module with:
  - `fetch_test_analysis()` - gets analysis from `<!-- outcomeops:test-analysis -->` marker
  - `fetch_test_failure_output()` - gets failures from `<!-- outcomeops:test-results -->` marker
  - `extract_failed_test_files()` - parses pytest output for file paths
  - `query_kb_for_test_patterns()` - queries KB for testing standards
  - `generate_test_fixes()` - uses Claude Sonnet to generate fixes
  - `handle_fix_tests()` - main handler that orchestrates the flow
  - `invoke_run_tests()` - triggers run-tests Lambda after committing fixes
- Added `get_pr_comments()` to `github_api.py`
- Added IAM permission for generate-code to invoke run-tests Lambda

### 3. run-tests Lambda (NOT YET DEPLOYED - needs container rebuild)
- Made `planFile` optional: `Optional[str] = Field(default=None, ...)`
- Added `skipPlanUpdate` field: `bool = Field(default=False, ...)`
- Added guard for plan update: `if repo_dir and detail.planFile and not detail.skipPlanUpdate:`
- Added `post_test_success_comment()` function for when tests pass on existing PR
- Added branch for existing PR success: `elif tests_passed and detail.prUrl is not None:`

### 4. Terraform (applied)
- Added `invoke_run_tests` IAM permission to generate-code Lambda

## Next Steps

1. **Restart WSL** (Docker not detecting)

2. **Rebuild run-tests container image:**
   ```bash
   ./scripts/build-run-tests-image.sh --env prd
   ```

3. **Apply terraform** (to update Lambda to use new image tag):
   ```bash
   cd terraform
   terraform apply -var-file=prd.tfvars
   ```

4. **Test the full flow:**
   - On PR #57, comment: `outcomeops: fix tests`
   - Should see:
     1. handle-command acknowledges and sends to SQS
     2. generate-code/tests_fixer commits fixes
     3. run-tests runs tests and posts results
     4. If still failing, user can run `outcomeops: explain failed tests` again

## Flow Diagram

```
User: "outcomeops: fix tests"
         |
         v
   handle-command
   (validates analysis exists)
         |
         v
   SQS (code-generation-queue)
         |
         v
   generate-code
   -> tests_fixer.handle_fix_tests()
      1. Fetch test analysis from PR comments
      2. Fetch test failure output from PR comments
      3. Extract failed test file paths
      4. Read test file contents
      5. Query KB for test patterns
      6. Generate fixes with Claude
      7. Commit fixes to branch
      8. Post progress comment
      9. Invoke run-tests (async)
         |
         v
   run-tests
   1. Clone repo at PR branch
   2. Run pytest
   3. Post result comment:
      - Success: "Tests Passed"
      - Failure: New test failure details
         |
         v
   User can iterate with "explain failed tests" if still failing
```

## Files Modified

| File | Status |
|------|--------|
| `lambda/handle-command/handler.py` | Deployed |
| `lambda/generate-code/handler.py` | Deployed |
| `lambda/generate-code/tests_fixer.py` | Deployed (new file) |
| `lambda/generate-code/github_api.py` | Deployed |
| `lambda/run-tests/handler.py` | **Needs container rebuild** |
| `terraform/lambda-code-generation.tf` | Applied |
