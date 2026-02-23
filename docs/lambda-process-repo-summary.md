# Lambda: Process Repo Summary

Generates architectural summaries for repositories and fans out code units for detailed batch processing.

## Purpose

The process-repo-summary Lambda receives repository data from SQS, generates an architectural summary using Claude via Bedrock, creates embeddings, stores them in S3 Vectors, and sends individual code unit batches to the code-maps queue for detailed processing by `process-batch-summary`.

This Lambda serves as the middle step in the workspace code maps pipeline, handling the CPU/AI-intensive work of generating architectural summaries while using SQS for natural rate limiting against Bedrock throttling.

## Pipeline Position

```
github-sync OR generate-code-maps
    |
    v (SQS: repo-summaries FIFO queue)
    |
process-repo-summary      <-- This Lambda
    |
    +-- Architectural summary --> S3 Vectors + S3
    |
    v (SQS: code-maps standard queue)
    |
process-batch-summary
    |
    v (S3 Vectors)
```

## Trigger

- **SQS Event Source Mapping**: Messages from the `repo-summaries` FIFO queue
- **Batch Size**: 1 message at a time
- **Max Concurrency**: 20 via SQS scaling config

## Processing Flow

```
SQS message received (repo, repo_project, all_files, ...)
    |
    v
Load config from SSM (code-maps table, KB bucket, queue URL, etc.)
    |
    v
Determine mode:
  - Workspace mode (discover_code_units=true): Get installation token, discover code units via backend
  - Legacy mode: Use code_units provided in message
    |
    v
Generate architectural summary using Claude Sonnet 4.5 via Bedrock
    |
    v
Generate embedding using Bedrock Titan v2 (1024 dimensions)
    |
    v
Store summary + embedding in S3 Vectors
    |
    v
Upload summary to S3 (archival)
    |
    v
Send each code unit as a message to code-maps standard queue
    |
    v
Return batch item failures (for SQS partial batch response)
```

## SQS Message Format (Input)

Receives from the `repo-summaries` FIFO queue.

**Workspace repos** (from github-sync):
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

**Legacy repos** (from generate-code-maps):
```json
{
  "repo": "repo-name",
  "repo_project": "owner/repo-name",
  "repo_type": "application",
  "all_files": ["lambda/handler.py", "terraform/main.tf", "..."],
  "code_units": [
    {
      "batch_type": "handler-group",
      "group_name": "ingest-docs",
      "file_paths": ["lambda/ingest-docs/handler.py"],
      "storage_key": "summary#handler#ingest-docs"
    }
  ]
}
```

## SQS Message Format (Output)

Sends to the `code-maps` standard queue (one message per code unit):

```json
{
  "repo": "repo-name",
  "repo_project": "owner/repo-name",
  "batch_type": "handler-group",
  "group_name": "ingest-docs",
  "file_paths": ["lambda/ingest-docs/handler.py", "lambda/ingest-docs/requirements.txt"],
  "storage_key": "summary#handler#ingest-docs",
  "workspace_id": "B1MgdLzM1j0YeyAqCWbyxg"
}
```

## Two Processing Modes

### Workspace Mode (`discover_code_units: true`)
Used by workspace repos via `github-sync`. The Lambda:
1. Gets a GitHub App installation token using the provided `installation_id`
2. Fetches `.outcomeops.yaml` from the repo for backend configuration
3. Discovers code units using the backend abstraction (Python, TypeScript, Java, etc.)
4. Sends discovered code units to the code-maps queue

### Legacy Mode (code_units provided)
Used by SSM-configured repos via `generate-code-maps`. Code units are pre-computed by the producer and included in the message.

## Storage

**S3 Vectors** (semantic search):
- Workspace repos: `{workspace_id}#github#{repo}#code-map#{doc_id}`
- Legacy repos: `{repo}#code-map#{doc_id}`
- Includes: content, content_hash, repo, type, file_path, timestamp, workspace_id

**S3** (archival):
- Workspace repos: `workspaces/{workspace_id}/github/{repo}/architectural-summary.txt`
- Legacy repos: `code-maps/{repo}/architectural-summary.txt`

## Configuration

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/dynamodb/code-maps-table` - DynamoDB table name
- `/{env}/{app_name}/s3/knowledge-base-bucket` - S3 bucket for summaries
- `/{env}/{app_name}/sqs/code-maps-queue-url` - Code maps SQS queue URL
- `/{env}/{app_name}/github/token` - GitHub PAT (for legacy repos)
- `/{env}/{app_name}/s3vectors/bucket-name` - S3 Vectors bucket
- `/{env}/{app_name}/s3vectors/index-name` - S3 Vectors index
- `/{env}/{app_name}/github/app-id` - GitHub App ID (for workspace repos)
- `/{env}/{app_name}/github/private-key` - GitHub App private key (for workspace repos)

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)

**Infrastructure**:
- **Runtime**: Python 3.12
- **Timeout**: 900 seconds (15 minutes)
- **Memory**: 1024 MB
- **Concurrency**: Max 20 via SQS scaling config
- **Bedrock read timeout**: 300 seconds (5 minutes for Claude responses)

**IAM Permissions Required**:
- `ssm:GetParameter`, `ssm:GetParameters` - Read SSM config
- `kms:Decrypt` - Decrypt SSM parameters
- `dynamodb:PutItem` - Store code maps
- `s3:PutObject` - Upload summaries to S3
- `s3vectors:PutVectors` - Store embeddings
- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` - Consume from repo-summaries queue
- `sqs:SendMessage` - Send to code-maps queue
- `bedrock:InvokeModel` - Claude and Titan embeddings

## Retry Logic

**Bedrock calls**: Exponential backoff with up to 5 retries. Delays: 10s, 20s, 40s, 80s, 120s. Retries on `ThrottlingException`, `ServiceUnavailableException`, `InternalServerException`.

**SQS partial batch failures**: Uses `ReportBatchItemFailures` pattern. Successfully processed messages are deleted; failed messages remain for retry (up to 3 times per redrive policy, then DLQ).

## Monitoring

```bash
# CloudWatch Logs
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-process-repo-summary --follow
```

**Key Log Messages**:
- `Processing {N} repository summary requests` - Handler started
- `Workspace repo {repo_project}: discovering code units` - Workspace mode
- `Discovered {N} code units for {repo_project}` - Code units found
- `Processing {repo} ({N} files, {N} code units)` - Processing started
- `Generated architectural summary for {repo} ({N} chars)` - Claude response received
- `Stored code map embedding in S3 Vectors: {key}` - Embedding stored
- `Stored architectural summary for {repo} at {s3_key}` - S3 upload complete
- `Sent code unit to SQS: {group_name}` - Code unit queued
- `Queued {N} code units for {repo}` - All code units sent

## Error Handling

- If code unit discovery fails for workspace repos, continues with empty code units (architectural summary still generated)
- If Bedrock fails after all retries, the record is reported as a batch item failure for SQS retry
- If SQS send fails for a code unit, the entire record fails (retried by SQS)
- Failed messages go to the repo-summaries DLQ after 3 retries

## Related

- **Handler code**: `lambda/process-repo-summary/handler.py`
- **Terraform**: `terraform/lambda-knowledge-base.tf`
- **SQS queues**: `terraform/sqs.tf` (repo-summaries FIFO queue, code-maps standard queue)
- **Producer (workspace)**: `docs/lambda-github-sync.md`
- **Producer (legacy)**: `docs/lambda-generate-code-maps.md`
- **Consumer**: `docs/lambda-process-batch-summary.md`
- **Backend abstraction**: `lambda/generate-code-maps/backends/`
