# Lambda: Generate Code Maps

Analyzes your repositories to extract architectural patterns and code organization insights.

## Purpose

The generate-code-maps Lambda function scans repository structure and creates architectural summaries that help Claude understand your project's organization, patterns, and conventions. These summaries augment the ADRs and READMEs in the knowledge base.

## What Code Maps Include

- **Architectural Summaries**: Directory structure with intent/purpose of each major component
- **Batch Summaries**: Groups of related files (e.g., "all Lambda handlers", "Terraform modules")
- **File Relationship Analysis**: How files and directories relate to each other
- **Pattern Identification**: Common patterns used across the codebase (naming, structure, conventions)
- **Statistics**: File counts, lines of code, module organization

## Trigger

- **Manual**: `aws lambda invoke --function-name dev-outcome-ops-ai-assist-generate-code-maps --payload '{"repos": ["outcome-ops-ai-assist"]}' response.json`
- **On demand**: Call via API after major refactorings
- **Scheduled**: Can be integrated with EventBridge for periodic updates (not yet scheduled)

## Configuration

Code maps are generated for repositories defined in `repos_to_ingest` variable in your Terraform configuration. Unlike ingestion which runs hourly, code map generation is typically triggered manually or on-demand.

## Example Output

**Architectural Summary** stored in DynamoDB:

```json
{
  "PK": "outcome-ops-ai-assist/code-map",
  "SK": "METADATA",
  "type": "code-map",
  "content": "This repository implements a serverless knowledge base system for AI-assisted development...",
  "embedding": [0.123, 0.456, ..., 0.789],
  "repo": "outcome-ops-ai-assist",
  "path": "code-map",
  "content_hash": "abc123def456789...",
  "timestamp": "2025-01-15T10:00:00.000Z"
}
```

**File Batches** sent to SQS for async processing:

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

**Batch Summary** (created by SQS consumer, future):

```json
{
  "PK": "repo#outcome-ops-ai-assist",
  "SK": "summary#handler#ingest-docs",
  "type": "handler-group-summary",
  "batch_type": "handler-group",
  "group_name": "ingest-docs",
  "content": "This handler group processes document ingestion from GitHub...",
  "embedding": [0.789, 0.012, ..., 0.456],
  "repo": "outcome-ops-ai-assist",
  "file_count": 8,
  "timestamp": "2025-01-15T10:05:00.000Z"
}
```

## Storage

Generated code maps are stored in:

**DynamoDB**: Table specified by `/{environment}/{app_name}/dynamodb/code-maps-table`

Two schema patterns are used:

1. **Architectural Summary** (top-level overview):
   - Partition Key: `<repo_name>/code-map`
   - Sort Key: `METADATA`
   - Includes embeddings for semantic search
   - Example: `outcome-ops-ai-assist/code-map`

2. **Batch Summaries** (detailed component analysis):
   - Partition Key: `repo#<repo_name>`
   - Sort Key: `summary#<batch_type>#<group_name>`
   - Examples:
     - `summary#handler#ingest-docs`
     - `summary#infrastructure`
     - `summary#tests#unit`

**S3**: Knowledge base bucket at `/{environment}/{app_name}/s3/knowledge-base-bucket`
- Path: `code-maps/<repo_name>/architectural-summary.txt`
- Raw text files for archival and reference

## How Claude Uses Code Maps

When you ask Claude to implement a feature, Claude queries the knowledge base:

1. **Searches for architectural patterns**: "How are Lambda handlers structured in this repo?"
2. **Finds related code**: "What files implement event handling?"
3. **Understands conventions**: "How are tests organized?"
4. **Generates matching code**: Creates code that fits your project's patterns

## Example Workflow

### Generate Code Maps

```bash
# Generate maps for a single repository
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-generate-code-maps \
  --payload '{"repos": ["outcome-ops-ai-assist"]}' \
  response.json

# Or multiple repositories
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-generate-code-maps \
  --payload '{"repos": ["outcome-ops-ai-assist", "fantacyai-ui"]}' \
  response.json
```

### After Major Refactoring

When you restructure your code significantly, regenerate maps:

```bash
# After moving all Lambda handlers to a new directory
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-generate-code-maps \
  --payload '{"repos": ["outcome-ops-ai-assist"]}' \
  response.json
```

### Check CloudWatch Logs

Monitor the generation process:

```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-generate-code-maps --follow
```

## Implementation Details

**Current Status**: Lambda function fully implemented and tested.

The generate-code-maps function:
1. **Fetches repository structure** via GitHub API (recursive tree)
2. **Checks for recent commits** (last 61 minutes) to skip unchanged repos (unless `FORCE_FULL_PROCESS=true` or specific repos requested)
3. **Identifies key files** with prioritization:
   - Priority 1: Lambda handlers (`lambda/*/handler.py`)
   - Priority 2: Python modules in lambda directories
   - Priority 3: Terraform infrastructure files (`*.tf`)
   - Priority 4: Schema/model files (`*_schema.py`, `models/*.py`)
   - Priority 5: Test files (`tests/**/*.py`)
   - Priority 6: Configuration files (`requirements.txt`, `Makefile`, etc.)
   - Priority 7: Shared utilities (`src/**/*.py`, `utils/**/*.py`)
   - Priority 8: Documentation (`docs/**/*.md`)
4. **Groups files into logical batches**:
   - Infrastructure: All `.tf` files
   - Handler groups: Lambda functions grouped by directory
   - Tests: Grouped by type (unit, integration, fixtures)
   - Shared utilities: Common code and utilities
   - Schemas: Schema and model definitions
   - Documentation: All `.md` files
5. **Generates architectural summary** using Claude 3.5 Sonnet via Bedrock
6. **Creates embeddings** via Bedrock Titan Embeddings v2 (1024 dimensions)
7. **Stores summaries** in DynamoDB with embeddings and S3 for archival
8. **Sends batches to SQS** FIFO queue for async detailed processing

**Key Features**:
- **Skip unchanged repos**: Only processes repos with commits in last 61 minutes (configurable)
- **FIFO queue processing**: Batches sent to SQS for ordered async processing
- **Deduplication**: Uses MessageDeduplicationId to prevent duplicate processing
- **Fail-open design**: If commit check fails, processes repo anyway (ensures availability)
- **Retry logic**: Bedrock calls include exponential backoff retry logic

**Future Enhancements**:
- Automatic detection of shared patterns (factory patterns, middleware chains, etc.)
- Code metrics extraction (complexity, duplication)
- Integration with test coverage data
- Periodic automatic regeneration on main branch updates
- SQS consumer Lambda for detailed batch processing

## Configuration Requirements

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/s3/knowledge-base-bucket` - S3 bucket for code maps
- `/{env}/{app_name}/dynamodb/code-maps-table` - DynamoDB table name
- `/{env}/{app_name}/sqs/code-maps-queue-url` - SQS FIFO queue URL
- `/{env}/{app_name}/github/token` - GitHub personal access token (encrypted)
- `/{env}/{app_name}/config/repos-allowlist` - JSON list of repos to process

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)
- `FORCE_FULL_PROCESS` - Set to "true" to process all repos regardless of recent commits (default: "false")

**IAM Permissions Required**:
- `ssm:GetParameter` - Read SSM parameters
- `s3:PutObject` - Upload code maps to S3
- `dynamodb:PutItem` - Store embeddings in DynamoDB
- `sqs:SendMessage` - Send batches to SQS queue
- `bedrock:InvokeModel` - Call Bedrock Titan and Claude
- GitHub API access via personal access token

## Monitoring

View code map generation metrics:

```bash
# Check recent executions
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=dev-outcome-ops-ai-assist-generate-code-maps \
  --start-time 2025-01-01T00:00:00Z \
  --end-time 2025-01-15T00:00:00Z \
  --period 3600 \
  --statistics Sum

# Check CloudWatch Logs for detailed execution
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-generate-code-maps --follow

# Key log messages:
# - "Found X total items in {repo}" - Repository files fetched
# - "Generated architectural summary for {repo}" - Summary created
# - "Stored code map for {repo}" - Summary stored in DynamoDB/S3
# - "Grouped into X batches for {repo}" - File batching complete
# - "Sent {batch_type} batch to SQS: {group_name}" - Batch queued
# - "Processing X of Y repos with recent changes" - Recent commit filtering
```

## Error Handling

**Common Errors**:

1. **"Repos allowlist not found in SSM"**
   - Cause: SSM parameter not configured
   - Fix: Deploy Terraform with `repos_to_ingest` in tfvars

2. **"GitHub API error"**
   - Cause: Invalid GitHub token or rate limiting
   - Fix: Check token in SSM, verify permissions

3. **"Bedrock throttling"**
   - Cause: Too many API calls too quickly
   - Fix: Reduce number of repos processed simultaneously

4. **"Failed to send batch to SQS"**
   - Cause: SQS queue doesn't exist or wrong permissions
   - Fix: Verify SQS queue URL in SSM, check IAM permissions

## Related

- **Handler code**: `lambda/generate-code-maps/handler.py`
- **Unit tests**: `lambda/tests/unit/test_generate_code_maps.py`
- **Architecture**: `docs/architecture.md`
- **Knowledge base ingestion**: `docs/lambda-ingest-docs.md`
- **Deployment**: `docs/deployment.md`
- **ADR-002**: Development Workflow Standards
