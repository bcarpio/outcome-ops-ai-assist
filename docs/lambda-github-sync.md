# Lambda: GitHub Sync

Syncs workspace GitHub repos by fetching their file tree and routing to the appropriate processing pipeline.

## Purpose

The github-sync Lambda processes SQS messages to sync GitHub repos for workspace-scoped code map generation. It authenticates via GitHub App installation tokens, fetches the repository file tree, and routes based on repo type:
- **Application repos**: Queue to `repo-summaries` FIFO queue for code map generation
- **Standards repos**: Queue individual files to `file-ingestion` queue for document ingestion

## Pipeline Position

```
github-scheduler (hourly) OR github-integration (webhook)
    |
    v (SQS: github-sync queue)
    |
github-sync               <-- This Lambda
    |
    +-- Application repos --> (SQS: repo-summaries FIFO queue) --> process-repo-summary
    |
    +-- Standards repos ----> (SQS: file-ingestion queue) --> ingest-docs
```

## Trigger

- **SQS Event Source Mapping**: Messages from the `github-sync` standard queue
- **Batch Size**: 1 message at a time
- **Max Concurrency**: 5 concurrent invocations
- **Producers**: `github-scheduler` (hourly) and `github-integration` (webhook on repo add)

## Processing Flow

```
SQS message received (workspace_id, repo_name, installation_id)
    |
    v
Get repo record from DynamoDB (check include_code_maps, repo_type)
    |
    v
Update sync status to "in_progress"
    |
    v
Get GitHub App installation token via JWT exchange
    |
    v
Get default branch and latest commit SHA
    |
    v
Check if already synced this commit (skip if same SHA)
    |
    v
Fetch full repository tree via GitHub API
    |
    v
Route by repo_type:
  - application: queue_repo_for_code_maps() --> repo-summaries FIFO queue
  - standards: queue_file_for_ingestion() per file --> file-ingestion queue
    |
    v
Update sync status to "success" (with commit SHA) or "failed"
```

## SQS Message Format (Input)

Receives from the `github-sync` standard queue:

```json
{
  "workspace_id": "B1MgdLzM1j0YeyAqCWbyxg",
  "repo_name": "owner/repo-name",
  "installation_id": "12345",
  "trigger": "scheduled"
}
```

## SQS Message Format (Output - Application Repos)

Sends to the `repo-summaries` FIFO queue:

```json
{
  "repo": "repo-name",
  "repo_project": "owner/repo-name",
  "repo_type": "application",
  "workspace_id": "B1MgdLzM1j0YeyAqCWbyxg",
  "installation_id": "12345",
  "all_files": ["lambda/handler.py", "terraform/main.tf", "..."],
  "discover_code_units": true
}
```

The `discover_code_units: true` flag tells `process-repo-summary` to use backend abstractions to discover code units (rather than receiving them pre-computed).

## SQS Message Format (Output - Standards Repos)

Sends per-file messages to the `file-ingestion` queue:

```json
{
  "source": "github",
  "workspace_id": "B1MgdLzM1j0YeyAqCWbyxg",
  "repo_name": "owner/repo-name",
  "repo_type": "standards",
  "file_path": "docs/adr/ADR-001.md",
  "file_sha": "abc123",
  "commit_sha": "def456"
}
```

## Change Detection

Before syncing, the Lambda checks if the latest commit SHA matches the stored `last_commit_sha` on the repo record. If they match, it skips the sync and returns `no_changes`. This prevents redundant processing when the scheduler runs hourly but no commits have been pushed.

## File Filtering

For standards repos, files are filtered before queuing:
- **Included extensions**: `.py`, `.js`, `.ts`, `.tsx`, `.jsx`, `.java`, `.kt`, `.go`, `.rs`, `.rb`, `.php`, `.cs`, `.cpp`, `.c`, `.h`, `.tf`, `.yaml`, `.yml`, `.json`, `.md`, `.rst`
- **Skipped directories**: `node_modules`, `.git`, `__pycache__`, `.venv`, `venv`, `dist`, `build`, `.next`, `.nuxt`, `coverage`
- **Max file size**: 1 MB

## Configuration

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)
- `WORKSPACES_TABLE` - DynamoDB workspaces table name
- `FILE_INGESTION_QUEUE` - URL of the file-ingestion SQS queue
- `REPO_SUMMARIES_QUEUE` - URL of the repo-summaries FIFO queue
- `DELETE_QUEUE` - URL of the delete SQS queue
- `LOG_LEVEL` - Logging level (default: INFO)

**Infrastructure**:
- **Runtime**: Python 3.12
- **Timeout**: 900 seconds (15 minutes for large repos)
- **Memory**: 1024 MB
- **Concurrency**: Max 5 via SQS scaling config

**SSM Parameters** (read at runtime):
- `/{env}/{app_name}/github/app-id` - GitHub App ID
- `/{env}/{app_name}/github/private-key` - GitHub App private key (encrypted)

**IAM Permissions Required**:
- `dynamodb:GetItem`, `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:Query` - Workspaces table
- `ssm:GetParameter`, `ssm:GetParameters` - GitHub App config
- `kms:Decrypt` - SSM parameter decryption
- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` - Receive from github-sync queue
- `sqs:SendMessage` - Send to file-ingestion, repo-summaries, and delete queues

## Monitoring

```bash
# CloudWatch Logs
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-github-sync --follow
```

**Key Log Messages**:
- `GitHub sync handler invoked with {N} records` - Handler started
- `Processing sync for {workspace_id}/{repo_name}` - Sync started
- `No changes since last sync ({sha})` - Commit already synced, skipping
- `Found {N} items in repo tree` - Repo tree fetched
- `Queued {repo_name} to repo-summaries queue ({N} files)` - Application repo queued
- `Standards sync complete: {N} files queued` - Standards repo processed
- `Application sync complete: queued {repo_name} for code-map generation` - Application repo processed
- `Sync complete: {N} succeeded, {N} failed` - Final summary

## Error Handling

- If repo record not found in DynamoDB, returns error (message consumed)
- If GitHub API fails (token, tree fetch), marks sync as "failed" in DynamoDB
- If SQS send to repo-summaries fails, marks sync as "failed"
- DynamoDB `last_sync_status` and `last_sync_error` fields track failure state
- SQS automatic retry on Lambda errors (3 retries, then DLQ)

## Related

- **Handler code**: `lambda/github-sync/handler.py`
- **Terraform**: `terraform/lambda-github.tf` (github_sync_lambda module)
- **SQS queue**: `terraform/lambda-github.tf` (github_sync standard queue)
- **Producer**: `docs/lambda-github-scheduler.md`
- **Consumer (application)**: `docs/lambda-process-repo-summary.md`
- **Consumer (standards)**: `docs/lambda-ingest-docs.md`
- **Pipeline end**: `docs/lambda-process-batch-summary.md`
