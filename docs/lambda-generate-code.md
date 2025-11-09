# Lambda: Generate Code

Orchestrates AI-powered code generation from GitHub issues using Claude via Bedrock. Handles GitHub webhooks, creates execution plans, and generates code asynchronously via SQS.

## Purpose

The generate-code Lambda function integrates with GitHub to automatically generate code from approved issues. It:

- **Receives GitHub webhooks** when issues are labeled with `approved-for-generation`
- **Creates git branches** for generated code
- **Sends async messages** to SQS for plan generation (avoids webhook timeout)
- **Generates execution plans** using Claude via Bedrock + knowledge base context
- **Executes steps** to generate files, commit changes, and create pull requests

## Architecture

The Lambda uses a dual-path architecture to avoid GitHub webhook timeouts:

```
Path 1: GitHub Webhook (API Gateway)
  └─> Verify signature
  └─> Parse webhook payload
  └─> Create git branch
  └─> Send "generate_plan" message to SQS
  └─> Return 200 OK (<10 seconds)

Path 2: SQS Message Processing
  ├─> Action: "generate_plan"
  │   └─> Query knowledge base for context
  │   └─> Generate execution plan with Claude
  │   └─> Commit plan to branch
  │   └─> Send "execute_step" message for step 1
  │
  └─> Action: "execute_step"
      └─> Get plan from branch
      └─> Query knowledge base for step context
      └─> Generate code with Claude
      └─> Commit files to branch
      └─> Send message for next step OR create PR
```

## Triggers

### GitHub Webhook (Primary)

**Event:** Issue labeled with `approved-for-generation`

**Endpoint:** `POST /github/webhook/generate-code`

**Required Headers:**
- `x-hub-signature-256`: HMAC signature for verification

**Webhook Payload:**
```json
{
  "action": "labeled",
  "label": {
    "name": "approved-for-generation"
  },
  "issue": {
    "number": 123,
    "title": "Add list-recent-docs handler",
    "body": "User story and requirements...",
    "html_url": "https://github.com/owner/repo/issues/123",
    "state": "open"
  },
  "repository": {
    "name": "repo",
    "full_name": "owner/repo",
    "owner": {"login": "owner"},
    "default_branch": "main"
  }
}
```

### SQS Message (Internal)

The Lambda also processes SQS messages for async plan generation and step execution.

**Message Schema:**
```json
{
  "action": "generate_plan",  // or "execute_step"
  "issue_number": 123,
  "issue_title": "Add list-recent-docs handler",
  "issue_description": "Full issue body...",
  "repo_full_name": "owner/repo",
  "branch_name": "123-add-list-recent-docs-handler",
  "current_step": 0,  // 0 for generate_plan
  "total_steps": 0,   // Unknown for generate_plan
  "base_branch": "main"
}
```

## Message Flow

### 1. Webhook Receipt (Fast Path)
- Validates webhook signature
- Creates git branch: `{issue_number}-{kebab-case-title}`
- Sends "generate_plan" message to SQS FIFO queue
- Returns 200 OK immediately to GitHub

### 2. Plan Generation (Async)
- Queries knowledge base 3 times:
  - Repository standards (ADRs, conventions)
  - Similar handlers for patterns
  - Infrastructure templates
- Generates execution plan with Claude
- Breaks work into discrete steps
- Commits plan to branch as `issues/code-gen-plan-{issue_number}.md`
- Sends "execute_step" message for step 1

### 3. Step Execution (Async Loop)
For each step:
- Queries knowledge base for step-specific context
- Generates code files with Claude
- Commits files to branch
- Updates plan with progress
- If more steps: sends message for next step
- If done: creates pull request

## Configuration

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `ENV` | Environment (dev/prd) | `dev` |
| `APP_NAME` | Application name | `outcome-ops-ai-assist` |

### SSM Parameters

| Parameter | Description |
|-----------|-------------|
| `/{env}/{app}/github/token` | GitHub personal access token (encrypted) |
| `/{env}/{app}/github/webhook-secret` | Webhook secret for signature verification (encrypted) |
| `/{env}/{app}/sqs/code-generation-queue-url` | SQS FIFO queue URL |

### IAM Permissions

**SSM:** Read parameters (GitHub token, webhook secret, queue URL)
**SQS:** Send messages to code generation queue
**Bedrock:** InvokeModel on Claude Sonnet 4.5 (including inference profiles)
**Lambda:** Invoke query-kb Lambda for knowledge base access

## Branch Naming

Branches are automatically created from issue numbers and titles:

| Issue Title | Branch Name |
|-------------|-------------|
| `Add list-recent-docs handler` | `123-add-list-recent-docs-handler` |
| `[Lambda]: Fix timeout in query-kb` | `456-lambda-fix-timeout-in-query-kb` |
| `Implement OAuth 2.0 support` | `789-implement-oauth-2-0-support` |

**Rules:**
- Lowercase
- Alphanumeric and hyphens only
- Leading/trailing hyphens removed
- Max practical length (~50 chars)

## Error Handling

### Webhook Errors
Always returns 200 OK to prevent GitHub retries, even on errors:

```json
{
  "message": "Internal error",
  "error": "Branch creation failed: ref already exists"
}
```

### SQS Processing Errors
Failed messages are:
1. Retried up to 2 times (maxReceiveCount)
2. Moved to Dead Letter Queue (DLQ)
3. Logged with full stack traces

### Common Errors

**Branch Already Exists:**
- Occurs if issue is re-labeled
- Safe to ignore (existing branch is reused)

**Bedrock AccessDeniedException:**
- Check IAM permissions include inference profile ARNs
- Required: `arn:aws:bedrock:*:*:inference-profile/*`

**Signature Validation Failed:**
- Webhook secret mismatch in SSM
- Check `/{{env}}/{{app}}/github/webhook-secret`

## Example Issue Template

See [docs/example-code-generation-issue.md](example-code-generation-issue.md) for a detailed template showing:
- User story format
- Handler specifications
- Request/response schemas
- AWS resource requirements
- Business logic description
- Test scenarios

## EventBridge Notifications

After every successful PR creation the Lambda emits an EventBridge event so downstream automation (run-tests, PR commenters, etc.) can react without polling.

- **Bus:** `${env}-${app}-bus`
- **Source:** `outcomeops.generate-code`
- **Detail Type:** `OutcomeOps.CodeGeneration.Completed`
- **Detail Payload:**
  - `issueNumber`, `issueTitle`, `repoFullName`
  - `branchName`, `baseBranch`, `planFile`
  - `prNumber`, `prUrl`, `commitSha`
  - `environment`, `appName`, `eventVersion`

The `run-tests` Lambda subscribes to this event to clone the branch and run `make test`. Additional consumers can be added via Terraform by pointing new rules at the same bus.

## Performance

### Webhook Response Time
- **Before:** 200+ seconds (synchronous plan generation)
- **After:** <10 seconds (async via SQS)

### Total Generation Time
- **Plan generation:** ~2-3 minutes (3 KB queries + Claude)
- **Per step execution:** ~1-2 minutes (KB query + Claude + commit)
- **5-step feature:** ~10-15 minutes total

### Cost Optimization
- Knowledge base queries reuse vector embeddings
- Claude Sonnet 4.5: ~$3/million input tokens, ~$15/million output tokens
- Typical feature: 50K-100K tokens = $1-3 per generation

## Monitoring

### CloudWatch Logs
- **Log group:** `/aws/lambda/{{env}}-{{app}}-generate-code`
- **Retention:** 7 days

### Key Log Messages
```
[INFO] Processing issue #123 in owner/repo
[INFO] Generated branch name: 123-add-feature
[INFO] Sent plan generation message to SQS. MessageId: abc-123
[INFO] Generating plan for issue #123
[INFO] Executing step 1/5 for issue #123
```

### Metrics to Monitor
- Webhook response time (should be <10s)
- SQS message processing duration (plan: <3min, step: <2min)
- Bedrock API errors
- Knowledge base query latency

## Deployment

The Lambda is deployed via Terraform with:
- **Timeout:** 900 seconds (15 minutes for plan generation)
- **Memory:** 1024 MB (higher for Bedrock calls)
- **Concurrency:** Controlled by SQS batch size (1 message at a time)
- **Event source:** SQS FIFO queue + API Gateway

## Related Documentation

- [Example Code Generation Issue Template](example-code-generation-issue.md)
- [Lambda: Query KB](lambda-query-kb.md) - Knowledge base search
- [ADR-003: Git Commit Standards](adr/ADR-003-git-commit-standards.md) - Commit conventions
