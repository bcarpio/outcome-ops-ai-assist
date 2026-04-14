# ADR-012: Code Generation Verbosity Standards

## Status: Accepted

## Context

AI code generation often produces overly verbose output with excessive comments, examples, and explanations. This causes several problems:

1. **Truncation at 32K token limit**: Simple steps like "Add IAM permissions to terraform/lambda.tf" (expected ~200 lines) can generate 32K tokens of verbose output, hitting the max_tokens limit and failing.

2. **Wasted cost**: Each truncation wastes $0.50+ per attempt (input + output tokens) with no usable code generated.

3. **Double-processing overhead**: Generated code doesn't need inline tutorials when it will be reviewed by developers and tested by CI.

**Real Example:**
- Step 6: "Add IAM permissions for Lambda"
- Expected: ~200 lines of HCL policy definitions
- Actual: 32,000 tokens (hit limit), step failed
- Wasted: $0.51 on unusable output

The root cause is that AI models default to "educational mode" - explaining everything as if teaching a junior developer. This is counterproductive for code generation where:
- Code will be reviewed by humans anyway
- Tests validate correctness
- ADRs document the "why"
- Generated code just needs to work

## Decision

### 1. Concise Code by Default

Generated code should be **production-ready and concise**, not tutorial-style. The code itself should be clear enough that excessive comments aren't needed.

### 2. What to Include

**Docstrings:**
- Function/class docstrings: 1-2 lines describing purpose
- Module docstrings: Brief description of what the file contains

**Comments (only when needed):**
- Non-obvious logic explanations ("Retry 3x because DynamoDB is eventually consistent")
- ADR references ("Follows ADR-004 for error handling pattern")
- Warnings about gotchas ("Note: This must be called before X")

**Code standards:**
- Type hints (for Python)
- Consistent naming
- Clear function signatures

### 3. What to Exclude

**Never generate:**
- Line-by-line explanations of obvious code
- Multiple example scenarios in comments
- "You could also..." alternative approaches
- Tutorial-style explanations
- Redundant docstrings that just restate the function name
- Commented-out code "for reference"
- Explanations of standard library functions
- Block comments explaining what each section does

### 4. Examples

**Python - Too Verbose (Will Truncate):**

```python
def list_recent_docs(limit: int = 10) -> dict:
    """
    List recently ingested knowledge base documents.

    This function queries the DynamoDB table to retrieve recently ingested
    documents. It applies a filter to only return items that have embeddings,
    sorts them by timestamp in descending order (newest first), and limits
    the results based on the provided parameter.

    Args:
        limit (int): Maximum number of documents to return. Must be between
                    1 and 100. Defaults to 10 if not provided.

                    Examples:
                    - limit=5: Returns up to 5 documents
                    - limit=100: Returns up to 100 documents
                    - limit not provided: Returns up to 10 documents

    Returns:
        dict: A dictionary containing:
            - documents (list): List of document metadata dictionaries
            - total_returned (int): Number of documents returned
            - limit (int): The limit that was applied

            Each document dictionary contains:
            - pk (str): Partition key with repo identifier
            - sk (str): Sort key with document type and ID
            ...

    Raises:
        ValidationError: If limit is not between 1 and 100
        ClientError: If DynamoDB scan fails

    Example:
        >>> result = list_recent_docs(limit=5)
        >>> print(result['total_returned'])
        5

    Note:
        This function follows ADR-004 for error handling patterns...
    """
    # Load the DynamoDB table name from environment variable
    # This allows the function to work across different environments
    # (dev, staging, prod) without code changes
    table_name = os.environ.get('CODE_MAPS_TABLE')

    # Create a DynamoDB resource to interact with the table
    # We use resource instead of client for cleaner API
    dynamodb = boto3.resource('dynamodb')

    # ... 50 more lines of explanatory comments ...
```

**Python - Concise (Correct):**

```python
def list_recent_docs(limit: int = 10) -> dict:
    """List recently ingested KB documents, sorted by timestamp descending."""
    table_name = os.environ['CODE_MAPS_TABLE']

    response = dynamodb.scan(
        TableName=table_name,
        FilterExpression='attribute_exists(embedding)',
        Limit=limit
    )

    items = sorted(response['Items'], key=lambda x: x['timestamp'], reverse=True)

    return {
        'documents': [extract_metadata(item) for item in items],
        'total_returned': len(items),
        'limit': limit
    }
```

**Terraform - Too Verbose (Will Truncate):**

```hcl
# IAM Policy for list-recent-docs Lambda
#
# This policy grants the Lambda function the minimum permissions needed
# to perform its intended operations. Following the principle of least
# privilege, we only grant:
#
# 1. DynamoDB Scan - Required to query all documents in the table
#    - We use Scan instead of Query because documents span multiple
#      partition keys (different repos)
#    - The Limit parameter prevents full table scans
#    - Resource ARN is scoped to the specific code-maps table
#
# 2. DynamoDB Query - Required as fallback for single-repo queries
#    - Allows more efficient lookups when filtering by repo
#    - Resource ARN is scoped to the specific code-maps table
#
# 3. SSM GetParameter - Required to load table name from SSM
#    - Allows the Lambda to discover its DynamoDB table dynamically
#    - Resource ARN is scoped to the specific app's parameters
#    - Read-only access (GetParameter only, not PutParameter)
#
# Alternative approaches considered:
# - You could hardcode the table name in env vars instead of SSM
# - You could use Query with GSI instead of Scan for better performance
# - You could grant dynamodb:* but that violates least privilege

module "list_recent_docs_lambda" {
  # Module source and version
  # Using the official Terraform AWS Lambda module from HashiCorp registry
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 6.0"  # Use version 6.x to ensure compatibility

  # ... continues for hundreds more lines of comments ...
```

**Terraform - Concise (Correct):**

```hcl
module "list_recent_docs_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "~> 6.0"

  function_name = "${local.prefix}-list-recent-docs"
  handler       = "handler.handler"
  runtime       = "python3.12"

  attach_policy_statements = true
  policy_statements = {
    dynamodb_scan = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan", "dynamodb:Query"]
      resources = [aws_dynamodb_table.code_maps.arn]
    }
    ssm_params = {
      effect    = "Allow"
      actions   = ["ssm:GetParameter"]
      resources = ["arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
    }
  }
}
```

### 5. Type-Specific Guidelines

**Lambda Handlers:**
- Brief module docstring
- 1-line function docstrings
- Type hints on all functions
- No inline tutorials about AWS APIs

**Terraform/HCL:**
- No policy explanation comments
- Self-documenting resource names
- ADR references only where relevant (e.g., "# See ADR-004")

**Tests:**
- 1-line docstrings per test
- Test names should be descriptive enough
- No "this test verifies that..." comments
- Fixtures should be minimal

**TypeScript/JavaScript:**
- JSDoc only for public APIs
- No "// This function does X" comments
- Type definitions serve as documentation

## Consequences

### Positive

- **No truncation**: Simple steps won't hit 32K token limit
- **Lower cost**: ~50% fewer output tokens per step
- **Faster generation**: Less time generating verbose comments
- **Better signal-to-noise**: Generated code is easier to review
- **Production-ready**: Code looks like what experienced devs write

### Tradeoffs

- **Less hand-holding**: Junior devs reading generated code may need to look at ADRs
- **Requires good ADRs**: Documentation shifts from inline to ADRs (where it belongs)

## Implementation

### Starting today

1. Update code generation system prompt with verbosity guidelines
2. Add specific guidance for Terraform/IAM steps
3. Test on previously-truncated step types

### Prompt additions

Add to `CODE_GENERATION_SYSTEM_PROMPT`:

```
VERBOSITY: Generate CONCISE, production-ready code.
- Docstrings: 1-2 lines max, describe purpose only
- Comments: Only for non-obvious logic (not "what", but "why")
- No inline tutorials, examples, or alternative approaches
- No line-by-line explanations
- Code should be self-documenting through clear naming
```

Add type-specific guidance:

```
For Terraform/IaC:
- No policy explanation comments
- Resource names should be self-documenting
- Use locals for repeated values

For Tests:
- 1 line docstrings max
- Minimal fixtures
- No "this test verifies..." comments
```

## Related ADRs

- ADR-001: Creating ADRs (documentation belongs in ADRs, not inline)
- ADR-005: Testing Standards (test patterns)

## References

- [Output token limit issue](https://docs.aws.amazon.com/bedrock/latest/userguide/model-parameters-anthropic-claude-messages.html)
- [Clean Code principles](https://www.oreilly.com/library/view/clean-code-a/9780136083238/)

Version History:
- v1.0 (2025-12-02): Initial decision addressing truncation issues

<!-- Confluence sync -->
