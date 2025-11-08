# Claude Guidance for OutcomeOps AI Assist

This document provides guidance for AI assistants (Claude) working on this codebase. It captures project-specific conventions and workflows.

## Using the Knowledge Base

Instead of reading long documentation, query the knowledge base using `outcome-ops-assist`:

```bash
# Query standards and conventions
outcome-ops-assist "What are our Lambda handler standards?"
outcome-ops-assist "What are our Terraform module standards?"
outcome-ops-assist "What are our Git commit standards?"
outcome-ops-assist "How should I structure tests?"
```

**When to use outcome-ops-assist:**
- Before implementing any Lambda handler
- Before writing Terraform code
- When unclear about coding standards
- To check architectural patterns
- To understand repo conventions

**The knowledge base contains:**
- All ADRs (Architecture Decision Records) from outcome-ops-adrs repo
- READMEs from all application repos
- Code maps and architectural summaries
- Terraform infrastructure patterns
- Lambda handler patterns
- Testing best practices

## General Development Principles

### Ask, Don't Guess

Always ask questions when you need clarity. Never make assumptions or guess about:
- Requirements or acceptance criteria
- Implementation approaches when multiple options exist
- Architectural decisions
- Module outputs, API signatures, or data schemas
- Unclear user intent or ambiguous requests

If something is unclear, use the AskUserQuestion tool to clarify before proceeding.

### Verify, Don't Assume

When working with external modules, libraries, or APIs:
- Read the actual source code to verify outputs, parameters, and behavior
- Query the knowledge base first before making assumptions
- Check Terraform module sources when available
- Test assumptions before committing code

### Query Standards Before Implementation

Before writing ANY code:

1. Query `outcome-ops-assist` for relevant standards (Terraform, Lambda, Git, etc.)
2. Review the standards returned by the knowledge base
3. Apply those standards to your implementation
4. When in doubt, query for examples or ask the user

Example workflow:

```bash
# 1. Query for Lambda standards
outcome-ops-assist "What are our Lambda handler standards?"

# 2. Read the response (includes validation, testing, documentation requirements)

# 3. Implement the Lambda following those standards

# 4. Query for testing approach
outcome-ops-assist "How should I test Lambda handlers?"
```

## Git Commit Standards

### No Emojis in Commits or Documentation

Do not include emojis, icons, or special Unicode characters in:
- Git commit messages
- Markdown documentation files (.md)
- README files
- Code comments

Example:

```bash
# Bad - Contains emoji
fix(lambda): use PK and SK for DynamoDB items

🤖 Generated with Claude Code

# Good - No emojis
fix(lambda): use PK and SK for DynamoDB items

Generated with Claude Code (https://claude.com/claude-code)
```

### Conventional Commits Format

Use Conventional Commits format. Query for full standards:

```bash
outcome-ops-assist "What are our Git commit message standards?"
```

## Local Development Workflow

### Python Development

This project uses Python 3.12 for Lambda handlers.

**Before committing:**

1. **Run tests** - Verify all tests pass:
   ```bash
   cd lambda/tests
   make test-unit
   ```

2. **Check for syntax errors**:
   ```bash
   python -m py_compile lambda/*/handler.py
   ```

3. **Verify imports work**:
   ```bash
   python -c "import sys; sys.path.insert(0, 'lambda/vector-query'); import handler"
   ```

### Terraform Development

**What to run locally:**
- `terraform fmt` - Format Terraform files
- `terraform validate` - Validate configuration
- `terraform plan` - Preview changes

**Never run locally:**
- `terraform apply` - Run in CI/CD only
- `terraform destroy` - Run manually with approval only

### Git Push Policy

**Do not push to remote unless explicitly requested by the user.**

After creating commits, always ask:
> "The commit is ready. Would you like me to push it?"

Only run `git push` after user confirmation.

## Project-Specific Conventions

### Directory Structure

```
.
├── docs/                      # Documentation
│   ├── lambda-*.md           # Lambda documentation
│   └── claude-guidance.md    # This file
├── lambda/
│   ├── vector-query/         # Semantic search Lambda
│   ├── ask-claude/           # RAG generation Lambda
│   ├── query-kb/             # Orchestrator Lambda
│   ├── ingest-docs/          # Document ingestion Lambda
│   ├── generate-code-maps/   # Code mapping Lambda
│   ├── process-batch-summary/# Batch processing Lambda
│   └── tests/                # Centralized tests
│       ├── unit/             # Unit tests
│       ├── integration/      # Integration tests
│       └── fixtures/         # Test fixtures
├── scripts/
│   └── outcome-ops-assist    # CLI tool for querying KB
├── terraform/                # Infrastructure as Code
│   ├── main.tf              # Main Terraform config
│   ├── lambda.tf            # Lambda modules
│   ├── dynamodb.tf          # DynamoDB tables
│   └── variables.tf         # Variables
└── README.md
```

### DynamoDB Key Schema

When using DynamoDB tables, they are created with:
- **Partition Key**: `PK` (or `pk` depending on table)
- **Sort Key**: `SK` (or `sk` depending on table)

Always include both keys in DynamoDB items:

```python
# Good - Includes PK and SK
dynamodb_client.put_item(
    TableName=CODE_MAPS_TABLE,
    Item={
        'PK': {'S': doc_id},
        'SK': {'S': 'METADATA'},
        'content': {'S': content},
        # ... other attributes
    }
)

# Bad - Missing keys or wrong names
dynamodb_client.put_item(
    TableName=CODE_MAPS_TABLE,
    Item={
        'id': {'S': doc_id},  # Wrong - table uses PK, not id
        # ...
    }
)
```

### Lambda Handler Standards

Follow ADR-004: Lambda Handler Standards. All Lambdas should include:

1. **Standard imports**:
   ```python
   import json
   import logging
   import os
   import boto3
   from botocore.exceptions import ClientError
   ```

2. **Logging setup**:
   ```python
   logger = logging.getLogger()
   logger.setLevel(logging.INFO)
   ```

3. **AWS clients initialized once**:
   ```python
   # Initialize once per container (outside handler)
   dynamodb_client = boto3.client("dynamodb")
   bedrock_client = boto3.client("bedrock-runtime")
   ```

4. **Environment variables**:
   ```python
   ENVIRONMENT = os.environ.get("ENV", "dev")
   APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
   ```

5. **Structured logging**:
   ```python
   logger.info(f"[lambda-name] Processing request: {event}")
   ```

### Lambda IAM Policy Requirements

**ALWAYS include KMS decrypt permissions for Lambdas that access encrypted resources.**

All Lambdas that interact with DynamoDB, S3, or SSM Parameter Store with KMS encryption MUST include `kms:Decrypt` in their IAM policy statements.

**Required for all Lambdas:**

```hcl
policy_statements = {
  kms = {
    effect    = "Allow"
    actions   = ["kms:Decrypt"]
    resources = [module.kms_keys_service.arn]
  }
  # ... other policy statements
}
```

**Checklist when creating new Lambdas:**
- [ ] Does it access DynamoDB? Add `kms:Decrypt`
- [ ] Does it access S3 with KMS encryption? Add `kms:Decrypt`
- [ ] Does it read SSM parameters with KMS? Add `kms:Decrypt`

### Testing Standards

Follow ADR-003: Testing Standards. All tests should:

1. **Use Arrange-Act-Assert pattern**:
   ```python
   def test_cosine_similarity_identical_vectors():
       # Arrange
       vec = [1.0, 2.0, 3.0]

       # Act
       result = cosine_similarity(vec, vec)

       # Assert
       assert pytest.approx(result, rel=1e-5) == 1.0
   ```

2. **Mock external dependencies**:
   ```python
   @patch('handler.bedrock_client')
   def test_generate_embedding_success(self, mock_bedrock):
       # ...
   ```

3. **Use descriptive test names**:
   ```python
   def test_handler_returns_404_when_no_results_found():
       # ...
   ```

4. **Keep tests fast** - Unit tests should run in < 100ms each

5. **Test negative cases** - Always test error conditions

### Check Existing Module Versions Before Adding New Resources

**CRITICAL:** Before adding a new module to an existing Terraform file, ALWAYS read the file first to check what version is already in use.

When adding a new Lambda, S3 bucket, DynamoDB table, or any other module to an existing Terraform file:

1. Read the entire file first to see existing module versions
2. Use the exact same version as other modules of the same type
3. Never use version ranges like `~>` or `>=`
4. Never guess the version - always verify

Example workflow:

```bash
# Correct workflow:
# 1. Read existing lambda.tf first
grep "version =" terraform/lambda.tf
# Shows: version = "8.1.2" (used by all Lambdas)

# 2. Use that exact version for new Lambda
module "new_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"  # Same as existing Lambdas
  ...
}
```

### Bedrock and Claude Configuration

When using Bedrock and Claude:

1. **Use inference profiles for cross-region access**:
   ```python
   modelId = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"
   ```

2. **Include retry logic with exponential backoff**:
   ```python
   for attempt in range(max_retries):
       try:
           response = bedrock_client.converse(...)
           return response
       except ClientError as e:
           if e.response.get("Error", {}).get("Code") == "ThrottlingException":
               wait_time = 2 ** attempt  # 1s, 2s, 4s
               time.sleep(wait_time)
   ```

3. **Set appropriate inference config**:
   ```python
   inferenceConfig = {
       "temperature": 0.3,  # Low for factual responses
       "maxTokens": 2000,
   }
   ```

### CloudWatch Logs Retention

All Lambda functions MUST have logs retention configured:

```hcl
module "my_lambda" {
  # ...
  cloudwatch_logs_retention_in_days = 7
}
```

## Common Pitfalls to Avoid

### 1. Not Querying Knowledge Base First
**Problem:** Writing code without checking standards
**Solution:** ALWAYS query `outcome-ops-assist` for relevant standards before implementing

### 2. Not Checking Existing Module Versions
**Problem:** Adding new module with different version than existing modules in same file
**Solution:** ALWAYS read the Terraform file first and use the same version as existing modules

### 3. Wrong Module Output Names
**Problem:** Assuming output names from docs (e.g., `table_name`)
**Solution:** Read actual `outputs.tf` to verify, or query `outcome-ops-assist` for examples

### 4. Missing DynamoDB Keys
**Problem:** Using `id` field instead of `PK` and `SK`
**Solution:** Always include `PK` and `SK` in items (project convention)

### 5. Running Terraform Apply Locally
**Problem:** Running `terraform apply` or deployments locally
**Solution:** Let the CI/CD pipeline handle deployments

### 6. Pushing Without Permission
**Problem:** Running `git push` without asking
**Solution:** Always ask before pushing commits

### 7. Missing KMS Decrypt Permissions
**Problem:** Lambda fails with `AccessDeniedException` when accessing DynamoDB/S3/SSM
**Solution:** ALWAYS add `kms:Decrypt` policy statement to Lambda IAM policies

### 8. Not Using outcome-ops-assist for Standards
**Problem:** Reading long ADR documents manually or guessing standards
**Solution:** Query `outcome-ops-assist` for any standard before implementation

### 9. Test Import Issues with importlib
**Problem:** Tests hang or fail due to module loading issues
**Solution:** Use `importlib.util` to load Lambda handlers dynamically in tests:

```python
import importlib.util
import sys

# Load handler module
handler_path = os.path.join(os.path.dirname(__file__), '../../lambda-name/handler.py')
spec = importlib.util.spec_from_file_location("lambda_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['lambda_handler'] = handler_module
spec.loader.exec_module(handler_module)
```

### 10. Using @patch with Dynamically Loaded Modules
**Problem:** Tests hang when using `@patch` decorator with importlib-loaded modules
**Solution:** Use `moto` decorators (`@mock_aws()`) instead of `@patch` for boto3 clients

## Questions and Escalation

If you encounter:
- Missing infrastructure features
- Outdated ADR documentation
- Conflicting guidance
- Unclear requirements

**Stop and ask the user for clarification.** Do not guess or work around issues silently.

## Additional Resources

- **Query standards:** `outcome-ops-assist "your question here"`
- **Project README:** `/README.md`
- **Test README:** `/lambda/tests/README.md`
- **ADRs:** Query via `outcome-ops-assist` instead of reading directly
- **Lambda docs:** `/docs/lambda-*.md`

---

This document is a living guide. Add new learnings and patterns as they emerge. When organizational standards change, they should be updated in ADRs and queried via `outcome-ops-assist`, not hardcoded here.
