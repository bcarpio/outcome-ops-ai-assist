# Lambda: GitHub Scheduler

Hourly fan-out scheduler that queues sync jobs for all workspace GitHub repos with code maps enabled.

## Purpose

The github-scheduler Lambda scans DynamoDB for all workspace GitHub repos that have `include_code_maps=true` and queues a sync job for each one to the github-sync SQS queue. This drives the hourly incremental code map update cycle for workspace repos.

## Pipeline Position

```
EventBridge (hourly)
    |
    v
github-scheduler          <-- This Lambda
    |
    v (SQS: github-sync queue)
    |
github-sync
    |
    v (SQS: repo-summaries queue)
    |
process-repo-summary
    |
    v (SQS: code-maps queue)
    |
process-batch-summary
    |
    v (S3 Vectors)
```

## Trigger

- **EventBridge Rule**: `rate(1 hour)` - runs every hour
- **Event**: Standard EventBridge scheduled event (`source: aws.events`)

## Processing Flow

```
EventBridge hourly trigger
    |
    v
Scan DynamoDB for GITHUB_REPO# items
(where include_code_maps=true AND enabled!=false)
    |
    v
For each repo: send message to github-sync SQS queue
    |
    v
Return summary (queued count, failed count)
```

## SQS Message Format (Output)

Sends to the `github-sync` standard queue:

```json
{
  "workspace_id": "B1MgdLzM1j0YeyAqCWbyxg",
  "repo_name": "owner/repo-name",
  "installation_id": "12345",
  "trigger": "scheduled"
}
```

## Configuration

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)
- `WORKSPACES_TABLE` - DynamoDB workspaces table name
- `GITHUB_SYNC_QUEUE` - URL of the github-sync SQS queue
- `LOG_LEVEL` - Logging level (default: INFO)

**Infrastructure**:
- **Runtime**: Python 3.12
- **Timeout**: 60 seconds
- **Memory**: 256 MB
- **Concurrency**: Single invocation (EventBridge)

**IAM Permissions Required**:
- `dynamodb:Scan` - Scan workspaces table for repos
- `sqs:SendMessage` - Send sync jobs to github-sync queue

## DynamoDB Query

Scans the workspaces table with filter:
- `SK` begins with `GITHUB_REPO#`
- `include_code_maps` equals `true`
- `enabled` does not exist OR equals `true`

Extracts `workspace_id` from `PK` (format: `WS#{workspace_id}`).

## Monitoring

```bash
# CloudWatch Logs
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-github-scheduler --follow
```

**Key Log Messages**:
- `GitHub scheduler invoked` - Handler started
- `Found {N} repos to sync` - Repos discovered from DynamoDB
- `No repos found for sync` - No repos with code maps enabled
- `Scheduler complete: {N} queued, {N} failed` - Final summary
- `Failed to queue sync job: {error}` - SQS send failure

## Error Handling

- If DynamoDB scan fails, returns empty list (no repos queued)
- If individual SQS send fails, logs error and continues with remaining repos
- Returns count of successful and failed queue operations

## Related

- **Handler code**: `lambda/github-scheduler/handler.py`
- **Terraform**: `terraform/lambda-github.tf` (github_scheduler_lambda module)
- **EventBridge rule**: `terraform/lambda-github.tf` (github_hourly_sync)
- **SQS queue**: `terraform/lambda-github.tf` (github_sync standard queue)
- **Consumer**: `docs/lambda-github-sync.md`
- **Pipeline end**: `docs/lambda-process-batch-summary.md`
