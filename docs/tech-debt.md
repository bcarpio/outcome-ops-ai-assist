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

## Code Maps: Hardcoded `main` Branch in File Listing

**Discovered:** 2026-05-03
**Severity:** Medium
**Component:** `process-repo-summary`

`list_repository_files()` in `lambda/process-repo-summary/handler.py:355` hardcodes the branch name in the GitHub trees API URL:

```python
url = f"{GITHUB_API_URL}/repos/{repo_project}/git/trees/main?recursive=1"
```

Any repo whose default branch is not `main` (e.g., `master`, `develop`, `trunk`) will 404 here, and `process-repo-summary` will fail to discover files for that repo.

**Impact:** F500 customers commonly have pre-2020 repos still defaulting to `master`. For those repos, code-map generation silently fails at the file-listing step. Affects every backend (Python, Java, TypeScript, React, ABAP, C#) - this is not language-specific.

**Inconsistency:** The per-backend change-detection code already does this correctly. For example, `csharp_backend.py:494` (`_get_default_branch`) and the equivalents in the other backends call `GET /repos/{repo}/` and read `default_branch` before fetching commit/compare data. So one half of the pipeline resolves the branch dynamically and the other half hardcodes `main`.

**Fix:**
1. Extract the existing `_get_default_branch` logic into a shared helper (or move it onto the base backend) so the handler can call it once per repo.
2. Replace the hardcoded `main` in `list_repository_files()` with the resolved default branch.
3. Pass the resolved branch through to `detect_changes()` so the backend doesn't re-resolve it.
4. Add a unit test covering a repo with `default_branch: "master"`.

**Workaround until fixed:** Only point the platform at repos whose default branch is `main`.

## Audit: Bedrock Pricing Env Vars Wired in Terraform but Ignored by audit.py

**Discovered:** 2026-05-03
**Severity:** Medium
**Component:** `lambda/shared/audit.py`, `terraform/lambda-chat.tf`, `terraform/lambda-code-generation.tf`

Pricing is exposed as Terraform variables in `prd.tfvars` and propagated to the relevant Lambdas as env vars:

```hcl
# terraform/prd.tfvars
bedrock_advanced_input_price  = 3.0
bedrock_advanced_output_price = 15.0
bedrock_basic_input_price     = 1.0
bedrock_basic_output_price    = 5.0

# terraform/lambda-chat.tf:61-64 (and lambda-code-generation.tf)
BEDROCK_ADVANCED_INPUT_PRICE  = tostring(var.bedrock_advanced_input_price)
BEDROCK_ADVANCED_OUTPUT_PRICE = tostring(var.bedrock_advanced_output_price)
BEDROCK_BASIC_INPUT_PRICE     = tostring(var.bedrock_basic_input_price)
BEDROCK_BASIC_OUTPUT_PRICE    = tostring(var.bedrock_basic_output_price)
```

But `lambda/shared/audit.py:61-79` ignores those env vars and uses hardcoded Python constants:

```python
ADVANCED_PRICING = {"input": 3.00, "output": 15.00}
BASIC_PRICING    = {"input": 0.80, "output": 4.00}
EMBEDDING_PRICING= {"input": 0.02, "output": 0.0}
```

These drive `_calculate_cost()`, which materializes `cost_usd` onto every audit row at write time. Historical rows are not rewritten if pricing changes - that is correct enterprise audit behavior.

**Why this matters:**

1. The Terraform plumbing is dead code from audit.py's perspective. The numbers happen to match for `ADVANCED` today, but `BASIC` already disagrees: tfvars says `input=1.0/output=5.0`, audit.py says `input=0.80/output=4.00`. Whichever invocation path counts the cost, only one of those is actually used, so dashboard numbers depend on which constant table wins.
2. Updating pricing requires a code change + Lambda deploy, even though the operator already updated `prd.tfvars`.
3. New Lambdas that import `shared.audit` inherit the hardcoded values silently.

**Fix:**

1. In `audit.py`, read `BEDROCK_ADVANCED_INPUT_PRICE` / `BEDROCK_ADVANCED_OUTPUT_PRICE` / `BEDROCK_BASIC_INPUT_PRICE` / `BEDROCK_BASIC_OUTPUT_PRICE` (and an embedding pair if added) at module load, fall back to current constants if unset, log a warning when falling back.
2. Reconcile the BASIC mismatch: pick whichever value is actually being charged and update the other source so they agree.
3. Make sure every Lambda that uses `shared.audit` has these env vars wired in its Terraform module - currently only `lambda-chat.tf` and `lambda-code-generation.tf` set them; modules like `process-batch-summary`, `query-kb`, `process-documents`, `process-repo-summary`, `analytics-cost-alert`, etc. do not.
4. Add a unit test that verifies `_calculate_cost()` honors the env vars.

**Future enhancement (not part of this debt):** per-org pricing overrides editable from the org-admin UI, since different enterprise customers negotiate different Bedrock rates / EDPs. Track separately if needed.

**Resolved 2026-05-03** (commit 5050303 + follow-up):

- `lambda/shared/audit.py` and `lambda/process-pr-check/check_handlers/bedrock_utils.py` both read `BEDROCK_{ADVANCED,BASIC,EMBEDDING}_{INPUT,OUTPUT}_PRICE` env vars at module load.
- `lambda/chat/handler.py`, `lambda/ask-claude/handler.py`, and `lambda/generate-code/utils.py` were already env-var-driven; verified.
- New Terraform variables `bedrock_embedding_input_price` / `bedrock_embedding_output_price`.
- Pricing env vars wired into every Terraform module whose Lambda imports a pricing-aware module: `lambda-knowledge-base.tf`, `lambda-pr-analysis.tf`, `lambda-file-ingestion.tf`, plus embedding-pricing parity on `lambda-chat.tf`, `lambda-code-generation.tf`, `lambda-query.tf`.
- BASIC mismatch (prd.tfvars 1.0/5.0 vs constants 0.80/4.00) is resolved at deploy time: tfvars wins.
