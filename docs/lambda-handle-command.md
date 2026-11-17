# Lambda: Handle Command

Processes commands issued via PR comments (e.g., `outcomeops: fix readme`).

## Purpose

The handle-command Lambda function processes bot commands from PR comments. It:

- **Parses commands** from PR comment text
- **Dispatches to handlers** for specific command types
- **Posts results** as PR comments

## Trigger

The handle-command Lambda is invoked via GitHub Actions workflow on `issue_comment` events. See [Setup Guide](#setup-guide) below.

## Input Schema

```json
{
  "command": "outcomeops: fix readme",
  "pr_number": 123,
  "repo": "owner/repo",
  "commenter": "username",
  "branch": "feature-branch"
}
```

**Validation:**
- `command` must be non-empty
- `pr_number` must be a positive integer
- `repo` must be in format `owner/repo`
- `commenter` must be non-empty
- `branch` must be non-empty

## Supported Commands

| Command | Description |
|---------|-------------|
| `outcomeops: help` | Display available commands |
| `outcomeops: fix readme` | Update README based on PR changes |
| `outcomeops: fix tests` | Generate or update tests for changed code |
| `outcomeops: fix adr` | Update ADR documentation based on architectural changes |
| `outcomeops: fix license` | Fix license headers and compliance issues |
| `outcomeops: regenerate` | Regenerate the entire PR (code, tests, docs) |

Commands are case-insensitive and support variations (e.g., `fix-readme`, `fixreadme`).

## GitHub Integration

### Authentication
GitHub Personal Access Token retrieved from SSM Parameter Store:
- Parameter: `/{environment}/{app_name}/github/token`
- Scope required: `repo` (write access for comments)

### Example PR Comments

**Help command:**
```markdown
## OutcomeOps Commands

Available commands (comment on a PR):

| Command | Description |
|---------|-------------|
| `outcomeops: help` | Show this help message |
| `outcomeops: fix readme` | Update README based on code changes in this PR |
| `outcomeops: fix tests` | Generate or update tests for changed code |
| `outcomeops: fix adr` | Update ADR documentation based on architectural changes |
| `outcomeops: fix license` | Fix license headers and compliance issues |
| `outcomeops: regenerate` | Regenerate the entire PR (code, tests, docs) |

**Usage:** Comment on any PR with one of the commands above.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
```

**Fix readme command (Phase 1):**
```markdown
## OutcomeOps: Fix README

Received command to fix README for PR #123.

**Status:** Command received
**Branch:** `feature-branch`

*Note: README fix functionality is coming soon.*

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
```

**Unknown command:**
```markdown
## OutcomeOps: Unknown Command

I don't recognize the command: `make coffee`

Run `outcomeops: help` to see available commands.

---
*Powered by [OutcomeOps AI Assist](https://outcomeops.ai)*
```

## Setup Guide

To enable PR comment commands in your repository, add a GitHub Actions workflow that listens for `issue_comment` events.

### Prerequisites

1. **AWS OIDC Identity Provider** configured in your AWS account
2. **IAM Role** with permission to invoke the handle-command Lambda
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
      "Resource": "arn:aws:lambda:us-west-2:ACCOUNT_ID:function:prd-outcome-ops-ai-assist-handle-command"
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

Create `.github/workflows/pr-commands.yml`:

```yaml
name: PR Commands

on:
  issue_comment:
    types: [created]

permissions:
  id-token: write      # Required for OIDC
  contents: read       # Required to checkout code
  pull-requests: read  # Required to read PR details

env:
  AWS_REGION: us-west-2
  ENVIRONMENT: prd

jobs:
  handle-command:
    name: Handle PR Command
    runs-on: ubuntu-latest
    environment: prd
    # Only run on PR comments (not issue comments) that start with "outcomeops:"
    if: |
      github.event.issue.pull_request &&
      startsWith(github.event.comment.body, 'outcomeops:')

    steps:
      - name: Get PR details
        id: pr
        uses: actions/github-script@v7
        with:
          script: |
            const pr = await github.rest.pulls.get({
              owner: context.repo.owner,
              repo: context.repo.repo,
              pull_number: context.issue.number
            });
            core.setOutput('branch', pr.data.head.ref);

      - name: Configure AWS credentials using OIDC
        uses: aws-actions/configure-aws-credentials@v4
        with:
          role-to-assume: ${{ secrets.AWS_OIDC_ROLE_ARN }}
          aws-region: ${{ env.AWS_REGION }}
          role-session-name: GitHubActions-PR-Commands

      - name: Invoke Handle Command Lambda
        run: |
          FUNCTION_NAME="${{ env.ENVIRONMENT }}-outcome-ops-ai-assist-handle-command"

          # Build payload
          PAYLOAD=$(cat <<EOF
          {
            "command": $(echo '${{ github.event.comment.body }}' | head -1 | jq -Rs .),
            "pr_number": ${{ github.event.issue.number }},
            "repo": "${{ github.repository }}",
            "commenter": "${{ github.event.comment.user.login }}",
            "branch": "${{ steps.pr.outputs.branch }}"
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

          # Check for errors in response
          if grep -q '"statusCode": 4' /tmp/response.json; then
            echo "Command failed"
            exit 1
          fi

          echo "Command processed successfully"
```

### Workflow Details

**Trigger:** `issue_comment` event when a comment is created

**Condition:** Only runs when:
1. The comment is on a PR (not an issue)
2. The comment body starts with `outcomeops:`

**Steps:**
1. Get PR branch name from GitHub API
2. Assume AWS role via OIDC
3. Invoke Lambda with command details
4. Report success/failure

## Error Handling

- **Invalid input**: Returns 400 with validation details
- **GitHub API errors**: Logs error, raises exception
- **SSM parameter missing**: Logs error, raises exception

## CloudWatch Logs

All logs written to: `/aws/lambda/{environment}-{app_name}-handle-command`

**Log examples:**

```
INFO - Handle command invoked: {"command": "outcomeops: help", ...}
INFO - Parsed command: type=help, action=help
INFO - Posted comment to PR #123 in owner/repo
```

## Dependencies

- **boto3**: AWS SDK for SSM operations
- **requests**: GitHub API HTTP client
- **pydantic**: Request/response validation

See `lambda/handle-command/requirements.txt` for full dependency list.

## Testing

Run unit tests:
```bash
cd lambda/tests
make test-unit
```

## Future Enhancements

- [ ] Implement full `fix readme` functionality
- [ ] Implement `fix tests` command
- [ ] Implement `fix adr` command
- [ ] Implement `fix license` command
- [ ] Implement `regenerate` command
- [ ] Support command arguments (e.g., `outcomeops: fix readme --verbose`)
- [ ] Add commenter authorization (restrict to PR author or maintainers)

## Related Documentation

- [Architecture Overview](architecture.md)
- [Lambda: Analyze PR](lambda-analyze-pr.md) - Similar workflow setup pattern

<!-- Confluence sync -->
