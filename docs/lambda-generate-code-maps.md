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

Documents stored in DynamoDB with structure:

```json
{
  "PK": "repo#outcome-ops-ai-assist",
  "SK": "summary#architecture",
  "type": "code-map",
  "content": "# Outcome-Ops AI Assist Architecture\n\nThe outcome-ops-ai-assist repository implements the core OutcomeOps AI Assist backend...",
  "embedding": [0.123, 0.456, ...],
  "file_path": "code-map:architecture:summary",
  "timestamp": "2025-01-15T10:00:00Z",
  "statistics": {
    "total_files": 145,
    "total_directories": 32,
    "main_components": ["lambda", "terraform", "tests", "docs"]
  }
}
```

## Storage

Generated code maps are stored in:
- **DynamoDB**: Table specified by `/{environment}/{app_name}/dynamodb/code-maps-table`
  - Partition Key: `repo#<repo_name>`
  - Sort Key: `summary#<component>` (e.g., summary#architecture, summary#lambda_handlers)
  - Includes embeddings for semantic search

- **S3**: Knowledge base bucket at `/{environment}/{app_name}/s3/knowledge-base-bucket`
  - Path: `code-maps/<repo_name>/<component>.md`
  - Raw markdown files for reference

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

## Implementation Notes

**Current Status**: Lambda function scaffolding complete, implementation in progress.

The generate-code-maps function will:
1. Fetch repository structure via GitHub API
2. Analyze file organization and relationships
3. Generate summaries of major components
4. Extract naming conventions and patterns
5. Create embeddings via Bedrock Titan v2
6. Store in DynamoDB and S3

**Future Enhancements**:
- Automatic detection of shared patterns (factory patterns, middleware chains, etc.)
- Code metrics extraction (complexity, duplication)
- Integration with test coverage data
- Periodic automatic regeneration on main branch updates

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
```

## Related

- **Handler code**: `lambda/generate-code-maps/handler.py` (future)
- **Architecture**: `docs/architecture.md`
- **Knowledge base ingestion**: `docs/lambda-ingest-docs.md`
- **Deployment**: `docs/deployment.md`
