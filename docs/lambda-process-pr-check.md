# Lambda: Process PR Check

Worker Lambda that processes PR check jobs from SQS queue, routes to appropriate check handlers, and posts results as GitHub PR comments.

## Purpose

The process-pr-check Lambda function is the **worker** in the PR analysis system. It:

- **Consumes jobs** from SQS FIFO queue (`pr-checks-queue`)
- **Routes to check handlers** based on check type
- **Executes checks** using AI-based analysis (Claude via Bedrock)
- **Posts results** as GitHub PR comments
- **Stores results** in DynamoDB for tracking

## Trigger

- **SQS Event Source Mapping**: Automatically invoked when messages arrive in `pr-checks-queue`
- **Batch Size**: 1 (processes one check job at a time)
- **Partial Batch Failures**: Returns `batchItemFailures` for retry handling

## Input Schema

**SQS Message Body:**
```json
{
  "checkType": "ADR_COMPLIANCE",
  "pr_number": 123,
  "repository": "owner/repo",
  "changedFiles": ["lambda/new-handler/handler.py", "terraform/main.tf"]
}
```

**Validation:**
- `checkType` must be one of: `ADR_COMPLIANCE`, `README_FRESHNESS`, `TEST_COVERAGE`, `BREAKING_CHANGES`, `ARCHITECTURAL_DUPLICATION`
- `pr_number` must be a positive integer
- `repository` must be in format `owner/repo`
- `changedFiles` must be an array of file paths

## Check Handlers

The Lambda routes to five specialized check handlers:

### 1. ADR Compliance (`adr_compliance.py`)

**Purpose:** Verify Lambda handlers and Terraform files follow documented architectural standards

**Analysis Method:**
- Queries knowledge base for relevant ADR standards
- Fetches unified diff for changed files from GitHub API
- Uses Claude to analyze code against standards
- Returns PASS/WARN/FAIL with detailed findings

**Dependencies:**
- Knowledge base (via query-kb Lambda)
- GitHub API (for file diffs)
- Bedrock Claude (for compliance analysis)

### 2. Architectural Duplication (`architectural_duplication.py`)

**Purpose:** Identify similar functionality across repositories that could be consolidated

**Analysis Method:**
- Summarizes PR functionality using Claude
- Queries knowledge base for similar handlers
- Uses Claude to compare PR summary with existing code
- Returns findings if duplicates detected

**Dependencies:**
- Knowledge base (via query-kb Lambda)
- GitHub API (for file diffs)
- Bedrock Claude (for summarization and comparison)

### 3. Breaking Changes (`breaking_changes.py`)

**Purpose:** Detect dependencies and potential breaking changes

**Analysis Method:**
- Summarizes changed handlers using Claude
- Performs vector search for dependent handlers
- Filters HIGH confidence dependencies (handler name + queue/topic references)
- Reports dependencies that may be affected

**Dependencies:**
- Vector query Lambda (direct invocation, not RAG)
- GitHub API (for file diffs)
- Bedrock Claude (for handler summarization)

### 4. README Freshness (`readme_freshness.py`)

**Purpose:** Ensure README documents new handlers and infrastructure

**Analysis Method:**
- Detects new Lambda handlers and infrastructure files
- Fetches README.md diff from GitHub
- Uses Claude to verify README adequately documents changes
- Returns WARN if README not updated or inadequate

**Dependencies:**
- GitHub API (for README diff)
- Bedrock Claude (for adequacy analysis)

### 5. Test Coverage (`test_coverage.py`)

**Purpose:** Verify new Lambda handlers have corresponding test files

**Analysis Method:**
- Extracts handler names from file paths (e.g., `lambda/hello/handler.py` → `hello`)
- Checks if test files contain handler name
- Returns WARN if tests missing

**Dependencies:**
- None (simple pattern matching)

## Processing Flow

```
SQS Message
     ↓
Parse & Validate (CheckJob schema)
     ↓
Route to Check Handler
     ↓
Execute Check (may call Claude, KB, GitHub)
     ↓
Build CheckResult
     ↓
Store in DynamoDB (initial)
     ↓
Post to GitHub as PR Comment
     ↓
Update DynamoDB with Comment URL
     ↓
Return Success (or batchItemFailures)
```

## GitHub Integration

### Authentication
GitHub Personal Access Token retrieved from SSM Parameter Store:
- Parameter: `/{environment}/{app_name}/github/token`
- Scope required: `repo` (read access for diffs, write for comments)

### API Endpoints Used

**Fetch file diff:**
```
GET /repos/{owner}/{repo}/compare/{base_sha}...{head_sha}
```

**Fetch README diff:**
```
GET /repos/{owner}/{repo}/compare/{base}...{head}
```

**Post comment:**
```
POST /repos/{owner}/{repo}/issues/{pr_number}/comments
```

### Example PR Comments

**PASS:**
```markdown
:white_check_mark: **ADR COMPLIANCE**: All files follow ADR standards

**Details:**
lambda/new-handler/handler.py: Uses Pydantic schemas
terraform/main.tf: Follows infrastructure conventions

_Check completed at 2025-01-15T10:00:00Z_
```

**WARN:**
```markdown
:warning: **README FRESHNESS**: README.md not updated

**Details:**
Infrastructure files changed but README not updated

_Check completed at 2025-01-15T10:00:00Z_
```

**FAIL:**
```markdown
:x: **ADR COMPLIANCE**: 2 file(s) do not follow ADR standards

**Details:**
lambda/new-handler/handler.py: Missing Pydantic schema validation
terraform/main.tf: Does not follow naming conventions

_Check completed at 2025-01-15T10:00:00Z_
```

## DynamoDB Storage

**Check results stored in code-maps table:**

```json
{
  "PK": "PR#123",
  "SK": "CHECK#adr_compliance",
  "checkType": "ADR_COMPLIANCE",
  "status": "PASS",
  "message": "All files follow ADR standards",
  "details": ["Handler uses Pydantic schemas"],
  "timestamp": "2025-01-15T10:00:00Z",
  "commentUrl": "https://github.com/owner/repo/pull/123#issuecomment-456"
}
```

**Access pattern:**
- Query all check results for a PR: `PK = "PR#123" AND SK BEGINS_WITH "CHECK#"`
- Query specific check result: `PK = "PR#123" AND SK = "CHECK#adr_compliance"`

## Error Handling

**Partial Batch Failures:**
- Failed messages returned in `batchItemFailures`
- SQS automatically retries failed messages
- After 3 retries, messages sent to DLQ

**Error Types:**
- **ValidationError**: Invalid SQS message format
- **ClientError**: AWS service errors (SSM, DynamoDB, Lambda invoke)
- **Exception**: Check execution errors (GitHub API, Bedrock)

**All errors include:**
- Full stack traces in CloudWatch logs
- SQS message ID for tracing
- Check type and PR number for context

## Dependencies

**Python packages:**
- **boto3**: AWS SDK for SSM, DynamoDB, Lambda, Bedrock
- **requests**: GitHub API HTTP client
- **pydantic**: Schema validation

See `lambda/process-pr-check/requirements.txt` for full dependency list.

**AWS Resources:**
- **SSM Parameter Store**: GitHub token
- **DynamoDB**: Check result storage
- **SQS**: Job queue (event source)
- **Lambda**: query-kb invocation
- **Bedrock**: Claude Sonnet 4.5

## CloudWatch Logs

All logs written to: `/aws/lambda/{environment}-{app_name}-process-pr-check`

**Log examples:**

```
INFO - Processing check job: ADR_COMPLIANCE for PR #123
INFO - Running ADR compliance check for PR #123
INFO - Querying KB for ADR standards: "Lambda handler development standards"
INFO - Fetching PR diff from GitHub for 2 files
INFO - Analyzing code with Claude Sonnet 4.5
INFO - Stored check result: PR#123 / CHECK#adr_compliance
INFO - Posted comment to PR #123 in owner/repo
INFO - Check complete: PASS - All files follow ADR standards
INFO - Successfully processed job: ADR_COMPLIANCE for PR #123
```

## IAM Permissions

**Required permissions:**
- **DynamoDB**: `PutItem`, `UpdateItem`, `GetItem`, `Query` (code-maps table)
- **SSM**: `GetParameter` (GitHub token)
- **KMS**: `Decrypt` (encrypted SSM parameters)
- **Bedrock**: `InvokeModel` (Claude Sonnet 4.5)
- **Lambda**: `InvokeFunction` (query-kb Lambda)
- **SQS**: `ReceiveMessage`, `DeleteMessage`, `GetQueueAttributes` (pr-checks-queue)

## Testing

Comprehensive test suite with 17 unit tests covering:
- Pydantic schema validation (CheckJob, CheckResult)
- GitHub token retrieval from SSM
- GitHub PR comment posting
- DynamoDB result storage
- Check routing logic
- End-to-end job processing
- SQS event handling (success, failures, multiple messages)

Run tests:
```bash
cd lambda/tests
../../venv/bin/python3.12 -m pytest unit/test_process_pr_check.py -v
```

## Performance Considerations

**Timeout:** 10 minutes
- ADR compliance check: ~30-60 seconds per file
- Architectural duplication: ~60-90 seconds (two Claude calls)
- Other checks: ~10-30 seconds

**Memory:** 1024 MB
- Higher memory for Bedrock API calls
- Concurrent HTTP requests to GitHub API

**Concurrency:**
- SQS FIFO ensures ordered processing per PR
- Batch size = 1 (one check at a time)
- Multiple workers can process different PRs concurrently

## Future Enhancements

- [ ] Parallel check execution within a single PR
- [ ] Check result caching to avoid duplicate work
- [ ] Configurable check thresholds via SSM parameters
- [ ] Check result aggregation (overall PR status)
- [ ] Webhooks for external integrations
- [ ] Performance metrics and optimization

## Related Documentation

- [Lambda: Analyze PR](lambda-analyze-pr.md) - Orchestrator that creates check jobs
- [Lambda: Query KB](lambda-query-kb.md) - RAG pipeline used by ADR compliance check
- [Architecture Overview](architecture.md) - System design and data flows
- [ADR-002: Development Workflow](adr/ADR-002-development-workflow.md) - PR analysis workflow
- [ADR-003: Testing Standards](adr/ADR-003-testing-standards.md) - Test patterns
