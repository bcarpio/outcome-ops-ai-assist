# Lambda: Process Batch Summary

Processes code map batch summaries from SQS queue and stores them in DynamoDB with embeddings.

## Purpose

The process-batch-summary Lambda function is an SQS consumer that processes file batches created by the generate-code-maps Lambda. For each batch, it:
1. Fetches file contents from GitHub
2. Generates detailed summaries using Claude via Bedrock
3. Creates embeddings using Bedrock Titan v2
4. Stores summaries in DynamoDB for semantic search

## Trigger

- **SQS Event Source Mapping**: Automatically triggered when messages arrive in the code maps FIFO queue
- **Batch Size**: 1 message at a time (each message already contains a batch of files)

## Processing Flow

```
SQS Message → Lambda Triggered
    ↓
Fetch File Contents from GitHub
    ↓
Generate Summary with Claude 3.5 Sonnet
    ↓
Create Embedding with Titan v2
    ↓
Store in DynamoDB (PK=repo#{repo}, SK=summary#{type}#{name})
    ↓
Delete Message from Queue (success) or Retry (failure)
```

## Batch Types

The Lambda handles different batch types with specialized prompts:

- **infrastructure**: Terraform files - describes resources and architecture
- **handler-group**: Lambda handlers - **detailed implementation documentation** including:
  - Function signatures with parameter types and format specifications
  - Query patterns (DynamoDB PK/SK, filters, S3 access)
  - Returns and error codes (200, 400, 404, 500) with trigger conditions
  - Data contracts (slug format, ID format, field names)
  - Common pitfalls with examples (incorrect vs correct usage)
  - Cross-references to related functions and utilities
- **tests**: Test files - describes testing patterns and coverage
- **shared**: Shared utilities - describes common code and helpers
- **schemas**: Schema definitions - describes data structures and validation
- **docs**: Documentation files - describes topics and organization

**Note**: The handler-group batch type generates comprehensive debugging documentation, not just high-level summaries. This enables developers to debug issues without reading source code directly.

## Example Input (SQS Message)

```json
{
  "repo": "outcome-ops-ai-assist",
  "repo_project": "bcarpio/outcome-ops-ai-assist",
  "batch_type": "handler-group",
  "group_name": "ingest-docs",
  "file_paths": [
    "lambda/ingest-docs/handler.py",
    "lambda/ingest-docs/requirements.txt"
  ],
  "storage_key": "summary#handler#ingest-docs"
}
```

## Example Output (DynamoDB)

```json
{
  "PK": "repo#outcome-ops-ai-assist",
  "SK": "summary#handler#ingest-docs",
  "content": "This handler ingests documentation from GitHub repositories...",
  "content_hash": "abc123def456...",
  "embedding": [0.123, 0.456, ..., 0.789],
  "repo": "outcome-ops-ai-assist",
  "type": "handler-group-summary",
  "batch_type": "handler-group",
  "group_name": "ingest-docs",
  "file_count": 2,
  "timestamp": "2025-01-15T10:00:00.000Z"
}
```

## Configuration Requirements

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/dynamodb/code-maps-table` - DynamoDB table name
- `/{env}/{app_name}/github/token` - GitHub personal access token (encrypted)

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)

**IAM Permissions Required**:
- `ssm:GetParameter` - Read SSM parameters
- `dynamodb:PutItem` - Store summaries in DynamoDB
- `bedrock:InvokeModel` - Call Bedrock Titan and Claude
- `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` - SQS event source mapping
- GitHub API access via personal access token

## Error Handling

**File Fetch Errors**:
- Logs error and continues with remaining files
- If no files can be fetched, stores "No files available for analysis"

**Bedrock API Errors**:
- Automatic retry with exponential backoff (3 attempts, max 8s delay)
- Retries on: ThrottlingException, ServiceUnavailableException, InternalServerException
- Fails immediately on: ValidationException, AccessDeniedException

**Storage Errors**:
- Logs error and raises exception
- SQS will retry the message (up to 3 times per redrive policy)
- Failed messages after 3 retries go to Dead Letter Queue

**Partial Batch Failures**:
- Uses ReportBatchItemFailures pattern
- Successfully processed messages are deleted
- Failed messages remain for retry

## Features

**Smart File Handling**:
- Skips files larger than 50KB
- Truncates files to 10KB per file for efficiency
- Continues processing if individual file fetch fails

**Retry Logic**:
- Bedrock calls include exponential backoff
- SQS automatic retry on Lambda errors
- Dead Letter Queue for permanently failed messages

**Content Deduplication**:
- Computes SHA-256 hash of summaries
- Can be used to skip re-processing if content unchanged

## Monitoring

**CloudWatch Logs**:
```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-process-batch-summary --follow
```

**Key Log Messages**:
- `Processing {batch_type} batch: {group_name} ({N} files)` - Started processing
- `Skipping large file in batch: {path}` - File too large
- `Generated batch summary for {group_name} ({N} chars)` - Summary generated
- `Stored {batch_type} summary for {group_name}` - Successfully stored
- `Successfully processed {batch_type} summary` - Record complete
- `Failed to process {N} out of {M} batches` - Partial failure

**CloudWatch Metrics**:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=dev-outcome-ops-ai-assist-process-batch-summary \
  --start-time 2025-01-01T00:00:00Z \
  --end-time 2025-01-15T00:00:00Z \
  --period 3600 \
  --statistics Sum
```

**SQS Queue Depth** (monitor for backlog):
```bash
aws sqs get-queue-attributes \
  --queue-url <queue-url> \
  --attribute-names ApproximateNumberOfMessages ApproximateNumberOfMessagesNotVisible
```

**Dead Letter Queue** (check for failed messages):
```bash
aws sqs receive-message \
  --queue-url <dlq-url> \
  --max-number-of-messages 10
```

## Performance

**Timing**:
- File fetch: ~100-500ms per file
- Claude summary: ~5-15 seconds (depends on content size)
- Embedding generation: ~500ms-1s
- DynamoDB storage: ~50-100ms
- **Total per batch**: ~10-30 seconds typically

**Batch Size Configuration**:
- Current: 1 message per invocation
- Each message contains 1-50 files (grouped by type)
- Timeout: 900s (15 minutes) handles large batches

**Throughput**:
- FIFO queue processes sequentially per MessageGroupId (repo)
- Multiple repos can process in parallel
- Lambda concurrency: Matches queue configuration

## Common Issues

**1. "Failed to fetch file from GitHub"**
- Cause: GitHub API rate limiting or invalid token
- Fix: Check token in SSM, verify permissions

**2. "Bedrock throttling"**
- Cause: Too many API calls too quickly
- Fix: Adjust queue visibility timeout, increase Lambda timeout

**3. "No files available for analysis"**
- Cause: All files in batch are too large (>50KB)
- Fix: Normal for binary/generated files, no action needed

**4. Messages in DLQ**
- Cause: Permanent failures after 3 retries
- Fix: Check DLQ messages, investigate errors in CloudWatch logs

## Related

- **Handler code**: `lambda/process-batch-summary/handler.py`
- **Unit tests**: `lambda/tests/unit/test_process_batch_summary.py`
- **SQS queue**: `terraform/sqs.tf`
- **Lambda Terraform**: `terraform/lambda.tf`
- **Producer Lambda**: `docs/lambda-generate-code-maps.md`
- **ADR-001**: Terraform Infrastructure Patterns
- **ADR-004**: Lambda Handler Standards
