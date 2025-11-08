# CLI Usage Guide: outcome-ops-assist

The `outcome-ops-assist` CLI tool provides a command-line interface for:
- **Querying the knowledge base** - Ask questions about architectural decisions, coding standards, and patterns using RAG
- **Analyzing Pull Requests** - Run AI-powered checks on GitHub PRs for compliance, duplication, and quality

## Installation

The CLI tool is located at `scripts/outcome-ops-assist` and should be added to your PATH for easy access.

### Add to PATH

```bash
# Option 1: Symlink to a directory in your PATH
sudo ln -s /path/to/outcome-ops-ai-assist/scripts/outcome-ops-assist /usr/local/bin/outcome-ops-assist

# Option 2: Add to your shell profile
echo 'export PATH="$PATH:/path/to/outcome-ops-ai-assist/scripts"' >> ~/.bashrc
source ~/.bashrc

# Option 3: Copy to a bin directory
cp scripts/outcome-ops-assist ~/.local/bin/
chmod +x ~/.local/bin/outcome-ops-assist
```

### Verify Installation

```bash
outcome-ops-assist --help
```

## Prerequisites

1. **AWS Credentials**: Configure AWS credentials with access to invoke Lambda functions
   ```bash
   aws configure
   # or
   export AWS_PROFILE=your-profile
   ```

2. **Deployed Infrastructure**: The Lambda functions must be deployed:
   - For KB queries: `{env}-outcome-ops-ai-assist-query-kb`, `vector-query`, `ask-claude`
   - For PR analysis: `{env}-outcome-ops-ai-assist-analyze-pr`, `process-pr-check`

3. **Knowledge Base**: The knowledge base must be populated with ADRs and code maps

4. **GitHub Token** (for PR analysis): Stored in SSM Parameter Store at `/{env}/{app_name}/github/token`

## Basic Usage

The CLI supports multiple modes:
- **Knowledge base queries** (default) - Ask questions about standards and patterns
- **PR analysis** (`analyze-pr` command) - Run AI-powered checks on GitHub PRs
- **Documentation ingestion** (`ingest-docs` command) - Populate the knowledge base

### Knowledge Base Query

```bash
outcome-ops-assist "What are our Lambda handler standards?"
```

**Output:**
```
🤖 Querying knowledge base...
Environment: dev
Query: What are our Lambda handler standards?

📚 Answer:

Based on the provided context, here are the Lambda handler standards from ADR-004:

## Handler Structure and Entry Point

Every Lambda handler follows a standardized structure that includes:
...

📖 Sources:
  • ADR: ADR-004-lambda-handler-standards
  • ADR: ADR-003-testing-standards
  • Code map - outcome-ops-ai-assist
```

### Query with Options

```bash
# Specify environment
outcome-ops-assist "How should I structure tests?" --env prd

# Increase number of results
outcome-ops-assist "What Terraform modules should I use?" --topK 10

# Combine options
outcome-ops-assist "How should API routes be defined?" --topK 5 --env dev
```

### PR Analysis

```bash
# Analyze a GitHub Pull Request
outcome-ops-assist analyze-pr 123 owner/repo

# Specify environment
outcome-ops-assist analyze-pr 456 myorg/myrepo --env prd
```

**Output:**
```
🔍 Analyzing Pull Request...
Environment: dev
Repository: owner/repo
PR Number: 123

✅ Analysis started for PR #123

Queued 3 check(s) for processing
Results will be posted as comments on the PR when complete

View PR: https://github.com/owner/repo/pull/123
```

## Command-Line Options

| Option | Description | Default | Example |
|--------|-------------|---------|---------|
| `--env` | Environment (dev, prd) | `dev` (from `$ENVIRONMENT`) | `--env prd` |
| `--topK` | Number of top results to retrieve | `5` | `--topK 10` |
| `--help`, `-h` | Show help message | - | `--help` |

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `AWS_PROFILE` | AWS profile to use for authentication | (none) |
| `ENVIRONMENT` | Environment name (dev, prd) | `dev` |
| `APP_NAME` | Application name | `outcome-ops-ai-assist` |

### Using Environment Variables

```bash
# Set AWS profile
AWS_PROFILE=dev outcome-ops-assist "Your question"

# Set environment
ENVIRONMENT=prd outcome-ops-assist "Your question"

# Combine multiple variables
AWS_PROFILE=production ENVIRONMENT=prd outcome-ops-assist "Your question"
```

## PR Analysis

The `analyze-pr` command triggers AI-powered checks on GitHub Pull Requests.

### How It Works

1. **Invokes `analyze-pr` Lambda** - Fetches PR metadata and changed files from GitHub
2. **Queues check jobs** - Sends jobs to SQS for async processing
3. **Workers process checks** - `process-pr-check` Lambda executes 5 different checks
4. **Results posted** - Check results appear as GitHub PR comments

### Available Checks

| Check | Description | Trigger Condition |
|-------|-------------|-------------------|
| **ADR Compliance** | Verifies code follows documented architectural standards using Claude | Lambda handlers or Terraform files changed |
| **Architectural Duplication** | Detects similar functionality across repos using similarity analysis | New or modified Lambda handlers |
| **Breaking Changes** | Identifies dependencies that may be affected | Modified or removed Lambda handlers |
| **README Freshness** | Ensures README documents new infrastructure | Changes in `lambda/`, `terraform/`, or `docs/` |
| **Test Coverage** | Verifies new handlers have corresponding tests | New Lambda handler files added |

### Usage Examples

```bash
# Analyze a PR in your organization
outcome-ops-assist analyze-pr 42 myorg/myrepo

# Analyze a PR in production environment
outcome-ops-assist analyze-pr 123 owner/repo --env prd

# The command returns immediately after queueing checks
# Results appear as GitHub PR comments within 1-2 minutes
```

### Check Results

Results are posted as individual comments on the PR:

**Example: ADR Compliance PASS**
```markdown
✅ ADR COMPLIANCE: All files follow ADR standards

Details:
- lambda/new-handler/handler.py: Uses Pydantic schemas
- terraform/main.tf: Follows infrastructure conventions

Check completed at 2025-01-15T10:00:00Z
```

**Example: README Freshness WARN**
```markdown
⚠️ README FRESHNESS: README.md not updated

Details:
- Infrastructure files changed but README not updated

Check completed at 2025-01-15T10:00:00Z
```

### Error Handling

**Invalid PR number:**
```bash
$ outcome-ops-assist analyze-pr abc owner/repo
Error: PR number must be a positive integer
```

**Invalid repository format:**
```bash
$ outcome-ops-assist analyze-pr 123 invalid
Error: Repository must be in format 'owner/repo'
```

**Lambda not found:**
```bash
$ outcome-ops-assist analyze-pr 123 owner/repo
❌ Error: Failed to invoke Lambda function: dev-outcome-ops-ai-assist-analyze-pr
Make sure the function exists and you have permissions to invoke it
```

### Workflow Integration

**Pre-merge checklist:**
```bash
# 1. Create PR on GitHub
# 2. Trigger analysis
outcome-ops-assist analyze-pr $PR_NUMBER $REPOSITORY

# 3. Wait for check results to appear as comments
# 4. Address any FAIL or WARN findings
# 5. Merge when all checks pass
```

**CI/CD Integration:**
```yaml
# .github/workflows/pr-analysis.yml
name: PR Analysis
on:
  pull_request:
    types: [opened, synchronize]

jobs:
  analyze:
    runs-on: ubuntu-latest
    steps:
      - name: Analyze PR
        run: |
          outcome-ops-assist analyze-pr \
            ${{ github.event.pull_request.number }} \
            ${{ github.repository }}
        env:
          AWS_PROFILE: ${{ secrets.AWS_PROFILE }}
          ENVIRONMENT: dev
```

## Documentation Ingestion

The `ingest-docs` command populates the knowledge base with documentation from configured repositories.

### How It Works

1. **Invokes `ingest-docs` Lambda** - Fetches documentation from GitHub repositories
2. **Processes documents** - Ingests ADRs, READMEs, and technical docs
3. **Generates embeddings** - Creates vector embeddings using Bedrock Titan
4. **Stores in knowledge base** - Saves to DynamoDB and S3 for semantic search

### Usage

```bash
# Ingest all configured repositories
outcome-ops-assist ingest-docs

# Ingest specific repository only
outcome-ops-assist ingest-docs outcome-ops-ai-assist

# Specify environment
outcome-ops-assist ingest-docs --env prd

# Ingest specific repo in production
outcome-ops-assist ingest-docs my-repo --env prd
```

### Output

**Full ingestion:**
```
📚 Ingesting documentation...
Environment: dev
Repositories: All configured repos

✅ Documentation ingestion complete

Repositories processed: 2
Total documents ingested: 15
```

**Single repository:**
```
📚 Ingesting documentation...
Environment: dev
Repository: outcome-ops-ai-assist

✅ Documentation ingestion complete

Repositories processed: 1
Total documents ingested: 8
```

### What Gets Ingested

The command ingests documentation from repositories configured in `terraform/{env}.tfvars`:

**From all repos:**
- ADRs (Architecture Decision Records) - `docs/adr/*.md`
- READMEs - `README.md`, `docs/README.md`

**From application repos:**
- Lambda documentation - `docs/lambda-*.md`
- Architecture docs - `docs/architecture.md`
- Deployment guides - `docs/deployment.md`

### When to Use

```bash
# Initial setup - populate knowledge base
outcome-ops-assist ingest-docs

# After updating ADRs or documentation
outcome-ops-assist ingest-docs outcome-ops-ai-assist

# After onboarding a new repository
# (first add to terraform/{env}.tfvars, deploy, then ingest)
outcome-ops-assist ingest-docs new-repo-name

# Scheduled refresh (automated via EventBridge)
# No manual action needed - runs hourly by default
```

### Error Handling

**Lambda not found:**
```bash
$ outcome-ops-assist ingest-docs
❌ Error: Failed to invoke Lambda function: dev-outcome-ops-ai-assist-ingest-docs
Make sure the function exists and you have permissions to invoke it
```

**Repository not in allowlist:**
```bash
# The Lambda will log a warning and process 0 repos if the requested
# repo is not in the configured allowlist (terraform/{env}.tfvars)
```

### Related Commands

Use with Makefile for convenience:
```bash
# Ingest specific repository
make ingest-docs-repo REPO=outcome-ops-ai-assist

# Set environment
ENVIRONMENT=prd make ingest-docs-repo REPO=my-repo
```

## Common Use Cases

### 1. Architecture and Standards

Query architectural decisions and coding standards:

```bash
# Lambda standards
outcome-ops-assist "What are our Lambda handler standards?"

# Testing standards
outcome-ops-assist "How should I structure tests?"

# Error handling
outcome-ops-assist "What's our standard for error handling in Lambdas?"

# DynamoDB patterns
outcome-ops-assist "How should I use DynamoDB keys (PK/SK)?"
```

### 2. Terraform and Infrastructure

Get guidance on infrastructure code:

```bash
# Terraform standards
outcome-ops-assist "What Terraform modules should I use for Lambda?"

# Module versions
outcome-ops-assist "What version of terraform-aws-lambda should I use?"

# IAM policies
outcome-ops-assist "What IAM permissions do Lambdas need for DynamoDB?"

# KMS encryption
outcome-ops-assist "Do I need KMS decrypt permissions for my Lambda?"
```

### 3. Development Workflow

Understand development processes:

```bash
# Git workflow
outcome-ops-assist "What are our Git commit message standards?"

# Code review
outcome-ops-assist "What should I check during code review?"

# Testing approach
outcome-ops-assist "Should I use unit tests or integration tests?"

# Deployment process
outcome-ops-assist "How do I deploy infrastructure changes?"
```

### 4. Debugging and Troubleshooting

Get help with common issues:

```bash
# Lambda errors
outcome-ops-assist "Why is my Lambda getting AccessDeniedException?"

# DynamoDB issues
outcome-ops-assist "How do I fix DynamoDB ValidationException?"

# Bedrock throttling
outcome-ops-assist "How should I handle Bedrock throttling errors?"

# Test failures
outcome-ops-assist "Why are my tests hanging with @patch decorator?"
```

### 5. Code Examples

Request code examples and patterns:

```bash
# Handler examples
outcome-ops-assist "Show me an example Lambda handler with error handling"

# Terraform examples
outcome-ops-assist "Show me an example Lambda module in Terraform"

# Test examples
outcome-ops-assist "Show me an example unit test for a Lambda handler"

# DynamoDB examples
outcome-ops-assist "Show me an example of DynamoDB put_item with PK and SK"
```

## Advanced Usage

### Piping and Output Processing

```bash
# Save output to file
outcome-ops-assist "What are our standards?" > standards.txt

# Extract just the answer (skip emoji headers)
outcome-ops-assist "Your question" | grep -A 100 "📚 Answer:"

# Search for specific term in output
outcome-ops-assist "Lambda patterns" | grep -i "error"
```

### Batch Queries

```bash
# Query multiple questions in sequence
cat questions.txt | while read -r question; do
  echo "Q: $question"
  outcome-ops-assist "$question"
  echo "---"
done
```

### Integration with Scripts

```bash
#!/bin/bash
# pre-commit hook that checks standards

# Query for commit message standards
STANDARDS=$(outcome-ops-assist "What are our Git commit message standards?")

# Validate commit message against standards
if ! grep -q "feat:\|fix:\|docs:" .git/COMMIT_EDITMSG; then
  echo "Commit message doesn't follow standards:"
  echo "$STANDARDS"
  exit 1
fi
```

## Output Format

The CLI returns structured output:

```
🤖 Querying knowledge base...        # Query status
Environment: dev                      # Environment being queried
Query: Your question here             # Your question echoed back

📚 Answer:                            # Answer section start

<Claude-generated answer with context from knowledge base>

📖 Sources:                           # Sources section start
  • ADR: ADR-001-example              # Source citation 1
  • Code map - repository-name        # Source citation 2
  • README.md - repository-name       # Source citation 3
```

### Exit Codes

| Code | Meaning |
|------|---------|
| `0` | Success - answer generated |
| `1` | Error - missing query, Lambda invocation failed, or no answer received |

## Troubleshooting

### Error: Lambda function not found

```
Error: Failed to invoke Lambda function: dev-outcome-ops-ai-assist-query-kb
Make sure the function exists and you have permissions to invoke it
```

**Solutions:**
1. Verify the Lambda exists:
   ```bash
   aws lambda list-functions | grep outcome-ops-ai-assist
   ```

2. Check your environment variable:
   ```bash
   echo $ENVIRONMENT  # Should be "dev" or "prd"
   ```

3. Verify AWS credentials:
   ```bash
   aws sts get-caller-identity
   ```

### Error: No answer received

```
❌ Error: No answer received from knowledge base
```

**Possible causes:**
1. **Knowledge base is empty** - Run ingestion:
   ```bash
   aws lambda invoke \
     --function-name dev-outcome-ops-ai-assist-ingest-docs \
     response.json
   ```

2. **Vector search returned no results** - Try:
   - Rephrasing your question
   - Using different keywords
   - Checking if relevant ADRs exist

3. **Claude generation failed** - Check CloudWatch logs:
   ```bash
   aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ask-claude --follow
   ```

### Slow responses

**Normal response times:**
- Vector search: 2-3 seconds
- Claude generation: 5-10 seconds
- **Total: 7-13 seconds**

If responses take longer:
1. Check Bedrock throttling in CloudWatch
2. Verify Lambda concurrency limits
3. Check DynamoDB read capacity

### Permission denied errors

```
An error occurred (AccessDeniedException) when calling the Invoke operation
```

**Solution:** Ensure your AWS credentials have `lambda:InvokeFunction` permission:

```json
{
  "Effect": "Allow",
  "Action": "lambda:InvokeFunction",
  "Resource": "arn:aws:lambda:*:*:function:*-outcome-ops-ai-assist-*"
}
```

## Best Practices

### 1. Be Specific

```bash
# ❌ Too vague
outcome-ops-assist "How do I code?"

# ✅ Specific
outcome-ops-assist "How should I structure a Lambda handler for DynamoDB queries?"
```

### 2. Use Natural Language

```bash
# ❌ Too keyword-focused
outcome-ops-assist "lambda dynamodb pk sk"

# ✅ Natural question
outcome-ops-assist "How should I use partition keys and sort keys in DynamoDB?"
```

### 3. Query Standards Before Implementing

```bash
# Before writing a Lambda handler
outcome-ops-assist "What are our Lambda handler standards?"

# Before writing Terraform
outcome-ops-assist "What Terraform module version should I use for Lambda?"

# Before writing tests
outcome-ops-assist "How should I structure unit tests for Lambda handlers?"
```

### 4. Save Frequently Used Queries

Create shell aliases:

```bash
# Add to ~/.bashrc or ~/.zshrc
alias oo-lambda="outcome-ops-assist 'What are our Lambda handler standards?'"
alias oo-test="outcome-ops-assist 'How should I structure tests?'"
alias oo-terraform="outcome-ops-assist 'What Terraform standards should I follow?'"
alias oo-git="outcome-ops-assist 'What are our Git commit message standards?'"
```

## Integration with Development Workflow

### Pre-Implementation Checklist

Before implementing any feature:

```bash
# 1. Query relevant standards
outcome-ops-assist "What are the standards for [feature type]?"

# 2. Look for existing patterns
outcome-ops-assist "Show me examples of [similar feature]"

# 3. Check testing requirements
outcome-ops-assist "How should I test [feature type]?"

# 4. Review deployment process
outcome-ops-assist "How do I deploy [infrastructure component]?"
```

### Code Review Checklist

During code review:

```bash
# Verify against standards
outcome-ops-assist "What are the standards for [component being reviewed]?"

# Check for missing requirements
outcome-ops-assist "What IAM permissions does [lambda name] need?"

# Validate test coverage
outcome-ops-assist "What should I test in [component type]?"
```

## Examples Library

### Architecture Questions

```bash
outcome-ops-assist "What is the RAG pipeline architecture?"
outcome-ops-assist "How does the knowledge base ingestion work?"
outcome-ops-assist "What AWS services are used in this project?"
outcome-ops-assist "How are embeddings generated and stored?"
```

### Lambda Development

```bash
outcome-ops-assist "What Python version should I use for Lambda?"
outcome-ops-assist "How should I structure Lambda handler imports?"
outcome-ops-assist "What logging format should Lambda handlers use?"
outcome-ops-assist "How should I handle errors in Lambda functions?"
```

### Testing

```bash
outcome-ops-assist "What testing framework should I use?"
outcome-ops-assist "How do I mock AWS services in tests?"
outcome-ops-assist "Should I use moto or @patch for boto3 mocking?"
outcome-ops-assist "How do I avoid test hangs with importlib?"
```

### Infrastructure

```bash
outcome-ops-assist "How should I configure CloudWatch Logs retention?"
outcome-ops-assist "What KMS permissions do Lambdas need?"
outcome-ops-assist "How should I version Terraform modules?"
outcome-ops-assist "What SSM parameters are required?"
```

## Related Documentation

**Lambda Functions:**
- **[Lambda: Query KB](lambda-query-kb.md)** - Knowledge base query pipeline
- **[Lambda: Analyze PR](lambda-analyze-pr.md)** - PR analysis orchestration
- **[Lambda: Process PR Check](lambda-process-pr-check.md)** - PR check worker and AI-powered analysis

**General:**
- **[Architecture Overview](architecture.md)** - System design and components
- **[Getting Started](getting-started.md)** - Initial setup guide
- **[Claude Guidance](claude-guidance.md)** - Development best practices

---

**Built for developer velocity.** Query your knowledge base. Analyze your PRs. Code with confidence.
