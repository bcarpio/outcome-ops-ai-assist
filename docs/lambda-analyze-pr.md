# Lambda: Analyze PR

Orchestrates Pull Request analysis by detecting changed files, determining necessary checks, and queueing jobs for async processing.

## Purpose

The analyze-pr Lambda function integrates with GitHub to automatically analyze Pull Requests. It:

- **Fetches PR details** from GitHub API (PR metadata, changed files)
- **Parses changed files** to determine which checks are relevant
- **Queues check jobs** to SQS FIFO for async execution
- **Posts status comments** to PRs with analysis progress

## Trigger

- **Manual**: `aws lambda invoke --function-name dev-outcome-ops-ai-assist-analyze-pr --payload '{"pr_number": 123, "repository": "owner/repo"}' response.json`
- **GitHub Webhook** (future): Triggered automatically on PR events

## Input Schema

```json
{
  "pr_number": 123,
  "repository": "owner/repo"
}
```

**Validation:**
- `pr_number` must be a positive integer
- `repository` must be in format `owner/repo`

## Check Types

The Lambda determines which checks to run based on changed file patterns:

### ADR Compliance
**Triggered by:** Lambda handlers (`lambda/**/*.py`) or Terraform files (`terraform/**/*.tf`)

**Purpose:** Verify architectural decisions are documented

**Files checked:**
- Lambda handlers (excludes test files)
- Terraform infrastructure definitions

### README Freshness
**Triggered by:** Changes in `lambda/`, `terraform/`, or `docs/` directories

**Purpose:** Ensure documentation stays up-to-date with code changes

### Test Coverage
**Triggered by:** New Lambda handler files (`lambda/*/handler.py` with status `added`)

**Purpose:** Verify new handlers have corresponding unit tests

### Breaking Changes
**Triggered by:** Modified or removed Lambda handlers

**Purpose:** Detect API contract changes that could break consumers

### Architectural Duplication
**Triggered by:** New or modified Lambda handlers

**Purpose:** Identify duplicate patterns that could be abstracted

## Job Queueing

Check jobs are sent to SQS FIFO queue for async processing:

```json
{
  "check_type": "ADR_COMPLIANCE",
  "pr_number": 123,
  "repository": "owner/repo",
  "changed_files": ["lambda/new-handler/handler.py", "terraform/main.tf"]
}
```

**FIFO Attributes:**
- **MessageGroupId**: `pr-{owner}-{repo}-{pr_number}` (ensures ordered processing per PR)
- **MessageDeduplicationId**: `{MessageGroupId}-{check_type}-{timestamp_ms}` (prevents duplicate jobs)

Queue URL retrieved from SSM: `/{environment}/{app_name}/sqs/pr-checks-queue-url`

## GitHub Integration

### Authentication
GitHub Personal Access Token retrieved from SSM Parameter Store:
- Parameter: `/{environment}/{app_name}/github/token`
- Scope required: `repo` (read access to repository)

### API Endpoints Used

**Fetch PR details:**
```
GET /repos/{owner}/{repo}/pulls/{pr_number}
```

**Fetch changed files:**
```
GET /repos/{owner}/{repo}/pulls/{pr_number}/files
```

**Post comment:**
```
POST /repos/{owner}/{repo}/issues/{pr_number}/comments
```

### Example PR Comment

When checks are queued:
```markdown
**OutcomeOps Analysis Started**

Running 3 checks:
- ADR COMPLIANCE
- README FRESHNESS
- TEST COVERAGE

Results will be posted as comments when complete.
```

When no checks needed:
```markdown
**OutcomeOps Analysis:** No checks needed for this PR (no relevant files changed)
```

## Error Handling

- **Invalid input**: Raises validation error with detailed message
- **GitHub API errors**: Logs error with request details, raises exception
- **SSM parameter missing**: Logs error, raises exception
- **SQS send failures**: Logs error with check type, raises ClientError
- **All errors** include full stack traces in CloudWatch logs

## Example Responses

**Successful analysis with checks:**
```json
{
  "message": "Analysis started for PR #123",
  "pr_number": 123,
  "checks_queued": 3
}
```

**No checks needed:**
```json
{
  "message": "No checks needed for this PR",
  "pr_number": 123,
  "checks_queued": 0
}
```

**Validation error:**
```json
{
  "error": "validation error for AnalyzePrRequest",
  "detail": [
    {
      "loc": ["pr_number"],
      "msg": "Input should be greater than 0",
      "type": "greater_than"
    }
  ]
}
```

## Dependencies

- **boto3**: AWS SDK for SSM and SQS operations
- **requests**: GitHub API HTTP client
- **pydantic**: Request/response validation

See `lambda/analyze-pr/requirements.txt` for full dependency list.

## CloudWatch Logs

All logs written to: `/aws/lambda/{environment}-{app_name}-analyze-pr`

**Log examples:**

```
INFO - Analyzing PR #123 in owner/repo
INFO - Fetched PR #123 from owner/repo: Add new Lambda handler
INFO - Fetched 5 changed files for PR #123
INFO - Changed files: 5, Checks to run: 3
INFO - Queued check job: ADR_COMPLIANCE for PR #123
INFO - Posted comment to PR #123 in owner/repo
INFO - Successfully queued 3 checks for PR #123
```

## Testing

Comprehensive test suite with 25 unit tests covering:
- Pydantic schema validation
- GitHub API integration (with mocks)
- File parsing and check determination logic
- SQS job queueing
- End-to-end handler orchestration

Run tests:
```bash
cd lambda/tests
make test-unit
```

## Future Enhancements

- [ ] GitHub webhook integration for automatic triggering
- [ ] Check result aggregation and status updates
- [ ] Configurable check rules via SSM parameters
- [ ] Batch processing for large PRs
- [ ] Check result caching to avoid duplicate work

## Related Documentation

- [Architecture Overview](architecture.md)
- [ADR-002: Development Workflow](adr/ADR-002-development-workflow.md)
- [ADR-003: Testing Standards](adr/ADR-003-testing-standards.md)
