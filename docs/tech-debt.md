# Technical Debt

## Code Maps: Oversized Test File Batches

**Discovered:** 2026-02-26
**Severity:** Low
**Component:** `process-repo-summary` / `process-batch-summary`

The repo summary pipeline batches files by group for code-map generation. Test file groups (e.g., `batch_type: "tests", group_name: "unit"`) can contain 60+ files in a single batch. When `process-batch-summary` tries to summarize all files at once, the concatenated content exceeds Bedrock's input limits, causing the Lambda to fail and messages to land in the code-maps DLQ.

**Impact:** Code-map summaries for large test directories are never generated. Non-blocking since test summaries are low-value for KB search.

**Fix:** Cap `file_paths` per batch in `process-repo-summary` (e.g., max 20 files per batch) and split oversized groups into multiple messages.

## Teams File Sync: No Delta, Full Re-Processing Every Hour

**Discovered:** 2026-03-19
**Severity:** Medium
**Component:** `teams-sync`, `file-ingestion`

The Teams file sync re-processes ALL files every hourly sync cycle, even if they haven't changed. This creates unnecessary API calls to Microsoft Graph, redundant S3 uploads, and re-embedding in the vector database.

**Root cause chain:**
1. `teams-sync/handler.py` `_sync_channel_files()` (lines 569-611) does a full enumeration of all files in each channel's SharePoint folder with no delta filtering
2. Each hourly sync generates a new `sync_id`, which changes the SQS `MessageDeduplicationId`, defeating cross-hour deduplication
3. `file-ingestion/handler.py` (lines 914-979) always downloads and uploads to S3 without checking if the file already exists or has changed
4. Every S3 upload triggers `process-documents`, which re-embeds the file in the vector database

**Impact:** Wasted compute, unnecessary Microsoft Graph API calls (risk of throttling at scale), redundant vector embeddings. Not functionally broken but scales poorly.

**Comparison:** Confluence filters pages by `last_sync_at` modification time. SharePoint uses drive delta tokens for files and `lastModifiedDateTime` filtering for pages. Teams messages correctly use Microsoft's delta API. Only Teams files lack delta sync.

**Fix (two layers):**
1. **Teams sync (primary):** Use the SharePoint drive delta API for channel files (Teams stores files in SharePoint under the hood) or filter files by `lastModifiedDateTime` >= `last_sync_at`, matching the Confluence/SharePoint pattern
2. **File ingestion (safety net):** Before uploading to S3, compare the file's etag or content hash against the existing S3 object. Skip upload if unchanged
