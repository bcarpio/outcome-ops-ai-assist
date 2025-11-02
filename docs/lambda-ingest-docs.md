# Lambda: Ingest Docs

Automatically ingests patterns from your codebase into the knowledge base.

## Purpose

The ingest-docs Lambda function scans your repositories via GitHub API and ingests documentation into the knowledge base for semantic search and code generation. It processes:

- **ADRs** (Architecture Decision Records) from standards repos
- **READMEs** from all repos to understand structure and purpose
- **Function-specific docs** from `docs/` directory to avoid chunking large files

## Trigger

- **Schedule**: EventBridge rule (hourly by default) - `rate(1 hour)`
- **Manual**: `aws lambda invoke --function-name dev-outcome-ops-ai-assist-ingest-docs response.json`

## Configuration

Configure repositories to ingest via Terraform variables in `dev.tfvars` or `prd.tfvars`:

```hcl
repos_to_ingest = [
  {
    name    = "outcome-ops-ai-assist"
    project = "bcarpio/outcome-ops-ai-assist"
    type    = "standards"  # "standards" or "application"
  },
  {
    name    = "fantacyai-adrs"
    project = "bcarpio/fantacyai-adrs"
    type    = "standards"
  }
]
```

The allowlist is stored in SSM Parameter Store at: `/{environment}/{app_name}/config/repos-allowlist`

## What Gets Ingested

### From Standards Repos
- **ADRs**: `docs/adr/**/*.md` - Architecture Decision Records
- **READMEs**: `README.md`, `docs/README.md`
- **Function docs**: `docs/lambda-*.md`, `docs/architecture.md`, `docs/deployment.md`

### From Application Repos
- **READMEs**: `README.md`, `docs/README.md` - project overview
- **Function docs**: `docs/lambda-*.md` - Lambda-specific patterns

## Storage

Documents are stored in DynamoDB with embeddings:

```json
{
  "PK": "repo#outcome-ops-ai-assist",
  "SK": "adr#ADR-001",
  "type": "adr",
  "content": "...",
  "embedding": [0.123, 0.456, ...],
  "file_path": "docs/adr/ADR-001-error-handling.md",
  "content_hash": "abc123...",
  "timestamp": "2025-01-15T10:00:00Z",
  "repo": "outcome-ops-ai-assist"
}
```

Also uploaded to S3 for archival:
- `s3://{bucket}/adr/{adr_id}.md`
- `s3://{bucket}/readme/{repo_name}-root.md`
- `s3://{bucket}/docs/{function_name}.md`

## Chunking Strategy

Large documents (>8000 tokens) are automatically chunked at intelligent boundaries:

1. **Paragraph breaks** (`\n\n`) - preserves semantic structure
2. **Sentence breaks** (`.`) - maintains readability
3. **Default split** - every ~32KB of text

This avoids excessive chunking for large README files while staying within Bedrock's 8192 token limit.

## Error Handling

- **Missing repos**: Logs warning, continues with remaining repos
- **Missing files**: Logs info message, skips to next file
- **API failures**: Retries up to 3 times (via urllib), then logs and continues
- **Embedding failures**: Falls back to first chunk if document too large
- **DynamoDB failures**: Logs error, fails the document but continues batch

## Example Responses

**Successful run:**
```json
{
  "statusCode": 200,
  "body": {
    "message": "Document ingestion completed",
    "documents_ingested": 15,
    "timestamp": "2025-01-15T10:45:23.123456Z"
  }
}
```

**Missing allowlist:**
```json
{
  "statusCode": 400,
  "body": {
    "error": "Repos allowlist not configured in SSM Parameter Store"
  }
}
```

## Monitoring

View logs in CloudWatch:

```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ingest-docs --follow
```

Key log messages:
- `Processing {repo_name}` - starting repo ingestion
- `Ingesting ADR: {file}` - ADR file being processed
- `Fetching {file}` - README being fetched
- `Generated embedding with X dimensions` - embedding created
- `Stored in DynamoDB: PK={pk}, SK={sk}` - document stored
- `Document ingestion completed: X documents ingested` - run finished

## Requirements

- **GitHub Token** (in SSM): `/{environment}/{app_name}/github/token` (encrypted)
- **S3 Bucket**: `/{environment}/{app_name}/s3/knowledge-base-bucket`
- **DynamoDB Table**: `/{environment}/{app_name}/dynamodb/code-maps-table`
- **Bedrock Access**: Titan Embeddings v2 model (`amazon.titan-embed-text-v2:0`)

## Permissions

Lambda execution role needs:
- `s3:ListBucket`, `s3:PutObject`, `s3:GetObject` on knowledge base bucket
- `dynamodb:PutItem`, `dynamodb:UpdateItem`, `dynamodb:GetItem`, `dynamodb:Query` on code-maps table
- `ssm:GetParameter` on parameter store paths
- `kms:Decrypt` for encrypted SSM parameters
- `bedrock:InvokeModel` for Titan Embeddings v2

## Related

- **Handler code**: `lambda/ingest-docs/handler.py`
- **ADRs**: `docs/adr/*.md`
- **Infrastructure**: `terraform/lambda.tf`
