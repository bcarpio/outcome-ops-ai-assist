# Lambda: Analyze PR

Orchestrates Pull Request analysis by detecting changed files, determining necessary checks, and queueing jobs for async processing.

## Purpose

The analyze-pr Lambda function integrates with GitHub to automatically analyze Pull Requests. It:

- **Fetches PR details** from GitHub API (PR metadata, changed files)
- **Parses changed files** to determine which checks are relevant
- **Queues check jobs** to SQS FIFO for async execution
- **Posts status comments** to PRs with analysis progress

## Trigger

The analyze-pr Lambda can be triggered in three ways:

- **Manual**: `aws lambda invoke --function-name prd-outcome-ops-ai-assist-analyze-pr --payload '{"pr_number": 123, "repository": "owner/repo"}' response.json`
- **AI-Generated PRs**: Automatically triggered by the run-tests Lambda after creating a PR
- **Human-Created PRs**: Via GitHub Actions workflow (see [Setup for Human PRs](#setup-for-human-prs) below)

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

### License Compliance
**Triggered by:** All source code files (.py, .js, .ts, .go, .java, .rb, .php, .rs, .c, .cpp, .h, .hpp, .scala, .kt, .swift, .cs, .sh, .bash, .md)

**Purpose:** Detect prohibited licenses, ensure copyright headers, and detect AI code similarity to copyrighted OSS per ADR-008

**Files checked:**
- All source code files in the PR
- Markdown files (for code blocks)
- Excludes: node_modules/, vendor/, dist/, build/, generated files

**Phase 1: License Header Detection (Always Runs)**
- Scans for GPL, AGPL, SSPL license text (FAIL)
- Scans for LGPL, MPL license text (WARN - legal review)
- Verifies new files have copyright headers (WARN if missing)
- Allows MIT, Apache-2.0, BSD, ISC licenses (PASS)

**Phase 2: AI Code Similarity Detection (AI-Generated Commits Only)**
- Filters to commits with "Generated with Claude Code" marker
- Quick pattern matching for known GPL algorithms (80-90% cost savings)
- Claude semantic analysis for detected patterns only
- Returns WARN for HIGH/MEDIUM confidence matches to GPL/AGPL/SSPL projects
- Advisory-only (never FAIL) to allow human review

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

Running 4 checks:
- ADR COMPLIANCE
- README FRESHNESS
- TEST COVERAGE
- LICENSE COMPLIANCE

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

## Setup for Human PRs

To automatically analyze human-created PRs, add a GitHub Actions workflow to your repository. This workflow triggers when PRs are opened or updated by humans, but **not** for PRs created by GitHub Apps (like the OutcomeOps AI Assist bot) - those are handled automatically by the run-tests Lambda.

### Prerequisites

1. **AWS OIDC Identity Provider** configured in your AWS account
2. **IAM Role** with permission to invoke the analyze-pr Lambda
3. **GitHub Environment** with the role ARN stored as a secret

### Step 1: Create IAM Role for GitHub Actions

Create an IAM role that allows GitHub Actions to invoke the Lambda:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "lambda:InvokeFunction",
      "Resource": "arn:aws:lambda:us-west-2:ACCOUNT_ID:function:prd-outcome-ops-ai-assist-analyze-pr"
    }
  ]
}
```

Configure the trust policy for GitHub OIDC:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::ACCOUNT_ID:oidc-provider/token.actions.githubusercontent.com"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringEquals": {
          "token.actions.githubusercontent.com:aud": "sts.amazonaws.com"
        },
        "StringLike": {
          "token.actions.githubusercontent.com:sub": "repo:YOUR_ORG/YOUR_REPO:*"
        }
      }
    }
  ]
}
```

### Step 2: Configure GitHub Repository

1. Go to **Settings > Environments** and create a `prd` environment
2. Add a secret `AWS_OIDC_ROLE_ARN` with the ARN of the IAM role created above

### Step 3: Add the Workflow File

Create `.github/workflows/pr-analysis.yml`:

```yaml
name: PR Analysis

on:
  pull_request:
    types: [opened, synchronize, reopened]
    branches:
      - main

permissions:
  id-token: write      # Required for OIDC
  contents: read       # Required to checkout code
  pull-requests: write # Required to post comments

env:
  AWS_REGION: us-west-2
  ENVIRONMENT: prd

jobs:
  analyze-pr:
    name: Analyze Pull Request
    runs-on: ubuntu-latest
    environment: prd

    steps:
      - name: Checkout code
        uses: actions/checkout@v4

      - name: Configure AWS credentials using OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_OIDC_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}
          role-session-name: GitHubActions-PR-Analysis

      - name: Invoke PR Analysis Lambda
        run: |
          FUNCTION_NAME="${{ env.ENVIRONMENT }}-outcome-ops-ai-assist-analyze-pr"

          PAYLOAD=$(cat <<EOF
          {
            "pr_number": ${{ github.event.pull_request.number }},
            "repository": "${{ github.repository }}"
          }
          EOF
          )

          echo "Invoking Lambda: $FUNCTION_NAME"
          echo "Payload: $PAYLOAD"

          aws lambda invoke \
            --function-name "$FUNCTION_NAME" \
            --payload "$PAYLOAD" \
            --cli-binary-format raw-in-base64-out \
            /tmp/response.json

          echo "Lambda response:"
          cat /tmp/response.json

          if [ $? -eq 0 ]; then
            echo "PR analysis started successfully"
            echo "Results will be posted as comments on the PR"
          else
            echo "PR analysis failed"
            exit 1
          fi
```

### Why No Duplicate Analysis?

GitHub Actions workflows do not trigger for events created by GitHub Apps. When the OutcomeOps AI Assist bot creates a PR:

1. The `pull_request` event is attributed to the GitHub App, not a user
2. GitHub suppresses workflow triggers for app-created events (to prevent infinite loops)
3. Only the run-tests Lambda triggers analyze-pr for these PRs

For human-created PRs:
1. The `pull_request` event is attributed to the human user
2. The GitHub Actions workflow triggers normally
3. The run-tests Lambda is not involved (no AI code generation)

This ensures each PR is analyzed exactly once, regardless of who created it.

## Future Enhancements

- [ ] Check result aggregation and status updates
- [ ] Configurable check rules via SSM parameters
- [ ] Batch processing for large PRs
- [ ] Check result caching to avoid duplicate work

## Related Documentation

- [Architecture Overview](architecture.md)
- [ADR-002: Development Workflow](adr/ADR-002-development-workflow.md)
- [ADR-003: Testing Standards](adr/ADR-003-testing-standards.md)

<!-- Confluence sync -->
