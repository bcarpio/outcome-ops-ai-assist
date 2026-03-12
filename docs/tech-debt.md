# Technical Debt

## Code Maps: Oversized Test File Batches

**Discovered:** 2026-02-26
**Severity:** Low
**Component:** `process-repo-summary` / `process-batch-summary`

The repo summary pipeline batches files by group for code-map generation. Test file groups (e.g., `batch_type: "tests", group_name: "unit"`) can contain 60+ files in a single batch. When `process-batch-summary` tries to summarize all files at once, the concatenated content exceeds Bedrock's input limits, causing the Lambda to fail and messages to land in the code-maps DLQ.

**Impact:** Code-map summaries for large test directories are never generated. Non-blocking since test summaries are low-value for KB search.

**Fix:** Cap `file_paths` per batch in `process-repo-summary` (e.g., max 20 files per batch) and split oversized groups into multiple messages.
