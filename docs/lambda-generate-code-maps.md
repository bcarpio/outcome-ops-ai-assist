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

Two invocation modes:

**Full Regeneration** (CLI with repos list):
- **All repos**: `ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps`
- **Single repo**: `ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps outcome-ops-ai-assist`
- Processes all handlers in specified repositories
- Used for: Initial setup, major refactorings, manual updates

**Incremental** (EventBridge with empty event):
- **EventBridge hourly**: Sends empty event `{}`
- Checks repos for commits in last 61 minutes
- Only processes changed handlers from active repos
- Used for: Automated hourly updates, continuous sync

## Configuration

Code maps are generated for repositories defined in the SSM Parameter Store allowlist at `/{env}/{app_name}/config/repos-allowlist`. The Lambda supports both manual invocation (full regeneration) and automated hourly runs (incremental updates).

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

### Generate Code Maps for All Repos

```bash
# Full regeneration of all application/internal repos
ENVIRONMENT=dev ./scripts/outcome-ops-assist generate-code-maps

# Production environment
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps
```

### Generate Code Maps for Single Repo

```bash
# Single repository regeneration
ENVIRONMENT=dev ./scripts/outcome-ops-assist generate-code-maps outcome-ops-ai-assist

# Or using make
ENVIRONMENT=dev make generate-code-maps-repo REPO=outcome-ops-ai-assist
```

### After Major Refactoring

When you restructure your code significantly, regenerate maps for that repo:

```bash
# After moving all Lambda handlers to a new directory
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps outcome-ops-ai-assist
```

### Check CloudWatch Logs

Monitor the generation process:

```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-generate-code-maps --follow
```

## Implementation Details

**Current Status**: Lambda function fully implemented with pluggable backend abstraction and incremental updates.

The generate-code-maps function:
1. **Determines invocation mode** - Empty event = incremental, repos list = full regeneration
2. **For incremental mode**: Checks each repo for commits in last 61 minutes (optimization)
3. **Fetches repository structure** via GitHub API (recursive tree)
4. **Uses pluggable backend** to discover code units (Lambda handlers, K8s services, modules, etc.)
5. **Detects changes via git diff** - compares current commit SHA to last processed commit
6. **Filters to changed code units** - For incremental mode, only processes handlers with changed files
7. **Discovers code units** using backend-specific logic:
   - Lambda backend: Groups by function directory (`lambda/*/`)
   - K8s backend (future): Groups by service
   - Monolith backend (future): Groups by module/package
8. **Generates architectural summary** using Claude 3.5 Sonnet via Bedrock
9. **Creates embeddings** via Bedrock Titan Embeddings v2 (1024 dimensions)
10. **Stores summaries** in DynamoDB with embeddings and S3 for archival
11. **Sends code units to SQS** FIFO queue for async detailed processing
12. **Tracks state** - saves current commit SHA in DynamoDB for next incremental run

**Backend Abstraction**:
- **Lambda Serverless Backend**: Discovers Lambda handlers, infrastructure, frontend files, tests, schemas, docs
- **Pluggable Architecture**: Easy to add new backends (K8s, monolith, microservices)
- **Backend Factory**: Registry-based factory pattern for backend instantiation
- **State Tracking**: Per-repo state tracking in DynamoDB (commit SHA, timestamp, files processed)

**Key Features**:
- **Incremental updates**: Only processes changed files since last commit (git-based change detection)
- **Full regeneration mode**: Set `FORCE_FULL_PROCESS=true` or pass specific repos in event
- **Backend selection**: Set `BACKEND_TYPE` env var (default: "lambda")
- **FIFO queue processing**: Code units sent to SQS for ordered async processing
- **Deduplication**: Uses MessageDeduplicationId to prevent duplicate processing
- **Fail-safe design**: Continues processing even if change detection fails
- **Retry logic**: Bedrock calls include exponential backoff retry logic

**Backend Types Supported**:
- **lambda**: Lambda serverless architecture (current implementation)
- **k8s**: Kubernetes microservices (coming soon)
- **monolith**: Monolithic applications (coming soon)

**State Tracking**:
- Stores last processed commit SHA per repository in DynamoDB
- Schema: `PK=repo#<repo_name>`, `SK=state#last-processed`
- Fields: `commit_sha`, `timestamp`, `files_processed`, `batches_sent`
- Enables efficient incremental updates on subsequent runs

**Invocation Mode Detection**:
- **Full regeneration**: Event contains `{"repos": ["repo1", "repo2"]}` - processes all handlers in specified repos
- **Incremental**: Event is empty `{}` - only processes changed handlers from repos with recent commits

**Incremental Mode Behavior**:
- **61-minute check**: Skips repos without commits in last 61 minutes (GitHub branches API)
- **Git compare**: For repos with recent commits, fetches changed files between last SHA and current SHA
- **Filter code units**: Only queues SQS batches for handlers/infrastructure with changed files
- **State tracking**: Saves current commit SHA after processing for next incremental run
- **Optimization**: Reduces Bedrock API costs and SQS messages by only processing changes

**Full Regeneration Behavior**:
- **CLI invocation**: outcome-ops-assist script queries SSM allowlist and sends all application repos
- **Process all handlers**: Discovers all code units and queues all batches
- **Used for**: Initial setup, major refactorings, manual updates

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
- `FORCE_FULL_PROCESS` - Set to "true" to process all repos regardless of changes (default: "false")
- `BACKEND_TYPE` - Backend to use: "lambda", "k8s", "monolith" (default: "lambda")
- `ENABLE_INCREMENTAL` - Enable incremental updates via state tracking (default: "true")

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
# - "Full regeneration mode: Processing X specific repos" - Full mode detected
# - "Incremental mode: Checking X application/internal repos" - Incremental mode detected
# - "{repo} has commits from {date} (within 61m)" - Repo has recent commits
# - "{repo} last commit {date} (older than 61m)" - Repo skipped (no recent commits)
# - "Skipping {repo} - no commits in last 61 minutes" - Repo optimization skip
# - "Found X total items in {repo}" - Repository files fetched
# - "Found X changed files between {sha1}..{sha2}" - Git compare results
# - "Code unit {name} has changes: {file}" - Handler matched to changed file
# - "Filtered X code units to Y changed units" - Incremental filtering applied
# - "Generated architectural summary for {repo}" - Summary created
# - "Stored code map for {repo}" - Summary stored in DynamoDB/S3
# - "Sent {batch_type} batch to SQS: {group_name}" - Batch queued
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
