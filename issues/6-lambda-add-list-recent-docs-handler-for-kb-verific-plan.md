# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-09T18:45:36.074220

## Issue Description

# Code Generation Request

## User Story

```
As a DevOps engineer,
I want to list recently ingested knowledge base documents via API,
So that I can verify KB ingestion is working correctly.
```

---

## Handler Specification

**Handler Name:** `list-recent-docs`

**Trigger Type:** Direct Invoke (via outcome-ops-assist CLI)

**Timeout:** 30 seconds

**Memory:** 512 MB

---

## Request Payload

```json
{
  "limit": 10
}
```

**Validation Rules:**

- `limit`: Optional, integer, min: 1, max: 100, default: 10

---

## Response Payload

```json
{
  "documents": [
    {
      "pk": "repo#outcome-ops-ai-assist",
      "sk": "adr#ADR-001-create-adrs",
      "doc_type": "adr",
      "file_path": "docs/adr/ADR-001-create-adrs.md",
      "ingested_at": "2025-01-08T12:00:00Z",
      "repo": "outcome-ops-ai-assist"
    },
    {
      "pk": "repo#outcome-ops-ai-assist",
      "sk": "readme#root",
      "doc_type": "readme",
      "file_path": "README.md",
      "ingested_at": "2025-01-08T12:00:00Z",
      "repo": "outcome-ops-ai-assist"
    }
  ],
  "total_returned": 2,
  "limit": 10
}
```

**Status Codes:**

- 200: Success
- 400: Invalid limit value
- 500: DynamoDB error

---

## AWS Resources Needed

**DynamoDB Tables:**

- `outcome-ops-ai-assist-{env}-code-maps` (existing) - Scan/Query access

**SSM Parameters:**

- `/{env}/outcome-ops-ai-assist/dynamodb/code-maps-table` - Read access

---

## Environment Variables

| Name             | Description          | Example Value                                 |
| ---------------- | -------------------- | --------------------------------------------- |
| `ENV`            | Environment name     | `dev`                                         |
| `APP_NAME`       | Application name     | `outcome-ops-ai-assist`                       |
| `CODE_MAPS_TABLE`| DynamoDB table name  | `dev-outcome-ops-ai-assist-code-maps`         |

---

## Business Logic

1. Load CODE_MAPS_TABLE from environment/SSM
2. Validate request payload (limit must be 1-100)
3. Scan DynamoDB table with:
   - FilterExpression: Attribute exists(embedding) to only get documents with embeddings
   - Limit: Based on request limit parameter
4. Sort results by timestamp field descending (newest first)
5. Extract document metadata from each item:
   - PK (partition key with repo identifier)
   - SK (sort key with document type and ID)
   - doc_type (adr, readme, doc, code_map)
   - file_path
   - timestamp (ingested_at)
   - repo (repository name)
6. Return list of documents with metadata

**Error Handling:**

- Invalid limit (< 1 or > 100) → 400 with validation error message
- DynamoDB scan error → 500, log error with traceback
- No documents found → 200 with empty documents array

**Special Cases:**

- If limit not provided → Use default of 10
- Documents returned in reverse chronological order (newest first)
- Only return items that have embeddings (actual documents, not metadata-only entries)

---

## Test Scenarios

### Success Cases

1. Valid request with limit=5 → 200 with up to 5 documents
2. Valid request with no limit → 200 with up to 10 documents (default)
3. No documents in table → 200 with empty array `{"documents": [], "total_returned": 0, "limit": 10}`

### Error Cases

1. Invalid limit value (0) → 400 with `{"error": "Invalid limit: must be between 1 and 100"}`
2. Invalid limit value (101) → 400 with `{"error": "Invalid limit: must be between 1 and 100"}`
3. Missing limit (not provided) → 200 with default limit of 10
4. DynamoDB table not found → 500 with error logged
5. DynamoDB unavailable → 500 with error logged

### Edge Cases

1. Table has fewer documents than limit → Return all available documents
2. Multiple document types (ADRs, READMEs, code maps) → Return all types, sorted by timestamp
3. Documents from multiple repos → Return all, grouped by timestamp not repo

---

## Expected Implementation

**Files to create:**

- `lambda/list-recent-docs/handler.py` - Main Lambda handler
- `lambda/list-recent-docs/requirements.txt` - Python dependencies (boto3)
- `lambda/tests/unit/test_list_recent_docs.py` - Unit tests
- `docs/lambda-list-recent-docs.md` - Handler documentation

**Files to modify:**

- `terraform/lambda.tf` - Add new Lambda function module
- Add IAM permissions for DynamoDB scan/query

**Terraform requirements:**

- Use `terraform-aws-modules/lambda/aws` version `7.14.0` (same as other Lambdas)
- Add to `terraform/lambda.tf` (don't create new file)
- IAM permissions needed:
  - `dynamodb:Scan` on code-maps table
  - `dynamodb:Query` on code-maps table
  - `ssm:GetParameter` on SSM paths
  - `kms:Decrypt` for encrypted SSM parameters
- Environment variables:
  - `ENV` = var.environment
  - `APP_NAME` = var.app_name
  - `CODE_MAPS_TABLE` from SSM parameter

---

## Notes

This is a **test story** to validate the code generation pipeline. Keep it simple:

- No complex integrations (just DynamoDB)
- No external APIs
- Uses existing DynamoDB table structure
- Simple scan/query logic
- Follow existing patterns from `ingest-docs` and `query-kb` handlers


## Cached Standards

<!-- Standards retrieved during plan generation (avoid re-querying) -->

### Lambda Standards

- # Query: Lambda handler standards and patterns

# Lambda Handler Standards and Patterns

According to **ADR-004: Lambda Handler Standards**, Lambda handlers are categorized into three types, each with specific requirements:

## Lambda Types

### 1. API Gateway Handlers
- **Invocation**: HTTP requests via API Gateway
- **Examples**: `/api/profile`, `/api/create-character`, `/api/save-settings`
- **Requirements**: CORS headers, JWT authentication, authorization, Pydantic validation

### 2. Event-Driven Handlers
- **Invocation**: AWS service events (SQS, EventBridge, S3, scheduled)
- **Examples**: SQS queue consumers, scheduled jobs, S3 event processors
- **Requirements**: Error handling, retry logic, idempotency

### 3. Internal Workers
- **Invocation**: Direct Lambda-to-Lambda invocation
- **Examples**: `query-kb` orchestrator, `vector-query`, `ask-claude`
- **Requirements**: Input validation, structured responses, error handling

## Core Standards (ALL Lambda Types)

According to ADR-004, all Lambda handlers MUST follow these patterns:

1. **Structured logging** with handler name prefix
2. **AWS clients initialized once per container** (outside handler function)
3. **Environment variables loaded at module level**
4. **Try-except with `exc_info=True`** for unexpected errors
5. **Type hints on functions**
6. **Docstrings on handler and business logic functions**

### Standard Structure Example:
```python
import boto3
import os
import json
import logging

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Initialize AWS clients (once per container)
dynamodb = boto3.resource("dynamodb")
ssm = boto3.client("ssm")

# Load environment variables
ENVIRONMENT = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "my-app")

def handler(event, context):
    """Main Lambda entry point"""
    try:
        logger.info(f"[handler-name] Processing request: {json.dumps(event)}")
        result = process_event(event)
        return result
    except Exception as e:
        logger.error(f"[handler-name] Unexpected error: {str(e)}", exc_info=True)
        raise
```

## API Gateway Handler Pattern

According to ADR-004, API Gateway handlers must include:

1. **Authentication**: JWT token validation using `decode_token(event)`
2. **Input Validation**: Pydantic models with Field constraints
3. **Authorization**: User permission checks
4. **CORS Response Helper**: Standardized response function with CORS headers
5. **SSM Parameter Store**: Configuration loaded once per container

### CORS Response Format:
```python
def _response(status_code, body):
    """Return standardized API response with CORS headers"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Idempotency-Key",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST,PUT,DELETE"
        },
        "body": json.dumps(body)
    }
```

## Size Guidelines

According to the **lambda-size-analysis.md** document:
- **Target**: Handlers should be under **10KB** for effective code-map generation
- **Acceptable**: Under 8KB is ideal (70% of handlers meet this)
- **Action Required**: Handlers over 10KB need refactoring

The document identifies 10 handlers exceeding the 10KB limit that require refactoring, with the largest being `character_management` at 28KB.

## Development Workflow

According to **claude-guidance.md**, before implementing any Lambda handler:

1. Query `outcome-ops-assist` for relevant standards
2. Review the standards returned by the knowledge base
3. Read existing similar Lambda handlers for patterns
4. Review relevant documentation in the `docs/` directory
5. When in doubt, ask the user

The guidance emphasizes: **"Query Standards Before Implementation"** and **"Ask, Don't Guess"**.
- # Query: Lambda error handling and logging best practices

# Lambda Error Handling and Logging Best Practices

Based on the provided context, here are the standards for Lambda error handling and logging:

## Core Logging Standards (ALL Lambda Types)

According to **ADR-004: Lambda Handler Standards**, all Lambda handlers MUST follow these logging patterns:

### 1. Structured Logging Setup
```python
import logging

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
```

### 2. Handler Name Prefix
All log messages must include a handler name prefix for easy filtering:

```python
logger.info(f"[handler-name] Processing request: {json.dumps(event)}")
logger.error(f"[handler-name] Unexpected error: {str(e)}", exc_info=True)
```

**Example from query-kb Lambda:**
- `[query-kb] Processing query: '...' (top X results)`
- `[query-kb] Invoking vector-query Lambda...`
- `[query-kb] Found X relevant documents`

### 3. Error Handling Pattern
All handlers must use try-except blocks with `exc_info=True` for unexpected errors:

```python
def handler(event, context):
    """Main Lambda entry point"""
    try:
        logger.info(f"[handler-name] Processing request: {json.dumps(event)}")
        
        # Business logic here
        result = process_event(event)
        
        return result
        
    except Exception as e:
        logger.error(f"[handler-name] Unexpected error: {str(e)}", exc_info=True)
        raise
```

## API Gateway Handler Error Handling

According to **ADR-004**, API Gateway handlers have specific error response requirements:

### Standardized Error Responses
All API responses must use a standardized response helper with CORS headers:

```python
def _response(status_code, body):
    """Return standardized API response with CORS headers"""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Idempotency-Key",
            "Access-Control-Allow-Methods": "OPTIONS,GET,POST,PUT,DELETE"
        },
        "body": json.dumps(body)
    }
```

### Common Error Scenarios

**Authentication Errors (401):**
```python
if not user_email:
    return _response(401, {"error": "Unauthorized"})
```

**Validation Errors (400):**
```python
try:
    input_data = UpdateProfileInput(**body)
except ValidationError as e:
    return _response(400, {"error": "Validation error", "details": e.errors()})
```

**Not Found (404):**
```python
if not user_item:
    return _response(404, {"error": "User not found"})
```

## Event-Driven Handler Error Handling

According to the **moderation_miss_alert handler documentation**, event-driven Lambdas should:

### 1. Catch and Log Errors Without Failing
```python
try:
    # Process event
    sns_client.publish(...)
    s3_client.put_object(...)
except Exception as e:
    logger.error(f"[❌ Alert Failure]: {str(e)}")
    # Does NOT throw exception - errors are caught and logged
```

### 2. Continue Processing Remaining Records
- Lambda continues processing remaining records
- Does not fail the entire batch
- Errors are logged but don't stop execution

### 3. Use Descriptive Log Messages
```python
logger.info(f"[✅ Alert Sent] GPT block on {user_email} ({ip_address})")
logger.info(f"[✅ Recorded In S3] GPT block on {user_email} ({ip_address})")
logger.error(f"[❌ Alert Failure]: {error_message}")
```

## Internal Worker Lambda Error Handling

According to the **query-kb Lambda documentation**, internal worker Lambdas should return structured error responses:

### Error Response Patterns

**Missing Required Fields (400):**
```json
{
    "statusCode": 400,
    "body": "{\"error\": \"Missing required field: query\"}"
}
```

**Service Failures (500):**
```json
{
    "statusCode": 500,
    "body": "{\"error\": \"Failed to search knowledge base\"}"
}
```

**Not Found (404):**
```json
{
    "statusCode": 404,
    "body": {
        "answer": "I couldn't find any relevant information...",
        "sources": []
    }
}
```

## Key Requirements Summary

According to **ADR-004**, all Lambda handlers MUST have:

1. **Structured logging** with handler name prefix
2. **Try-except blocks** with `exc_info=True` for unexpected errors
3. **Type hints** on functions
4. **Docstrings** on handler and business logic functions
5. **Standardized error responses** (for API Gateway handlers)
6. **Graceful error handling** (for event-driven handlers - log but don't fail batch)

## Monitoring

According to the **query-kb documentation**, CloudWatch Logs should be monitored with:

```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-query-kb --follow
```

Key log messages should clearly indicate:
- Processing start
- Stage transitions
- Success/failure states
- Error conditions

### Terraform Standards

- # Query: Terraform Lambda module configuration standards

# Terraform Lambda Module Configuration Standards

Based on the provided context, here are the standards for configuring Terraform Lambda modules:

## 1. Use Community Modules

**All new Lambda functions MUST use the `terraform-aws-modules/lambda/aws` community module** (from ADR-001). This module is:
- Battle-tested and maintained by the Terraform community
- Pre-configured with security best practices
- Provides consistent interfaces across AWS services

## 2. Pin Exact Module Versions

**All module source blocks MUST include explicit version pinning** (from ADR-001):

```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"  # Exact version pinning
  
  # ... configuration
}
```

**Version pinning requirements:**
- Pin exact major.minor.patch versions (e.g., `version = "8.1.2"`)
- NEVER use pessimistic operators (~>), unversioned modules, or `version = "*"`
- Update versions explicitly and intentionally via code review
- This prevents breaking changes from automatic module updates

## 3. Implement Least Privilege IAM Policies

**Every Lambda function must operate with minimum required permissions** (from ADR-001):

```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "my-function"
  handler       = "index.handler"
  runtime       = "python3.12"

  attach_policy_statements = true
  policy_statements = {
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",    # Only specific actions needed
        "dynamodb:Query"
      ]
      resources = [
        module.main_table.dynamodb_table_arn  # Specific resources only
      ]
    }
  }
}
```

**Least privilege checklist:**
- Specify exact actions (not *)
- Specify exact resources (not arn:aws:service:region:account:*)
- Use module outputs for ARNs (not hardcoded strings)
- Document why each permission is needed in comments
- Review quarterly - remove unused permissions

## 4. CloudWatch Logs Handling

**DO NOT create separate CloudWatch log groups** when using the Lambda module (from ADR-001):

The `terraform-aws-modules/lambda/aws` module automatically creates a CloudWatch log group. Creating a separate `aws_cloudwatch_log_group` resource will result in duplicate log groups.

**Incorrect pattern:**
```hcl
# DON'T DO THIS - creates duplicate log group
resource "aws_cloudwatch_log_group" "my_lambda_logs" {
  name              = "/aws/lambda/${module.my_lambda.lambda_function_name}"
  retention_in_days = 30
}
```

**Correct pattern:**
```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "my-function"
  # The Lambda module automatically creates the log group
}
```

## 5. Query Standards Before Implementation

According to the Claude Guidance documents, before writing ANY Terraform code:

1. Query `outcome-ops-assist` for relevant standards
2. Review the standards returned by the knowledge base
3. Apply those standards to your implementation
4. When in doubt, query for examples or ask the user
- # Query: Terraform resource naming conventions

Based on the provided context, I can identify the following Terraform resource naming conventions:

## Function and Resource Naming

According to **ADR-001** (Document 2), Lambda functions should follow this naming pattern:

```hcl
function_name = "${var.environment}-${var.app_name}-example-function"
```

This creates names like: `dev-outcome-ops-ai-assist-example-function`

The pattern uses:
- `${var.environment}` - Environment identifier (dev, prd)
- `${var.app_name}` - Application name
- Descriptive function name

## Plan File Naming

According to **ADR-004** (Document 3) and **ADR-002** (Document 4), Terraform plan files follow this convention:

- Dev environment: `terraform.dev.out`
- Prd environment: `terraform.prd.out`

**Important note**: Plan files should never be committed to git (already in .gitignore).

## Variable File Naming

According to the **Deployment Guide** (Document 5), Terraform variable files use:
- `dev.tfvars` - Development environment variables
- `prd.tfvars` - Production environment variables

## Limitations

The provided context does not include comprehensive naming conventions for:
- Module names
- Other AWS resources (S3 buckets, DynamoDB tables, IAM roles, etc.)
- Variable naming standards
- Output naming standards

The context focuses primarily on Lambda function naming and deployment artifact naming rather than a complete naming convention standard.

### Testing Standards

- # Query: Testing standards and patterns

# Testing Standards and Patterns

Based on the provided context, here are the comprehensive testing standards and patterns:

## Testing Philosophy

According to **ADR-003**, a story is DONE when tests covering new functionality are written, passing locally, and passing in CI. Core principles include:

- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)

## Test Pyramid Structure

According to **ADR-005**, the testing pyramid should be followed with emphasis on unit tests:

1. **Unit Tests (majority)** - Fast, isolated, many
   - Target: 90% of total test count
   - Test individual functions and classes
   - Mock all AWS services using moto
   - Should be fast (< 100ms per test ideally)
   - Should comprise 60-70% of test suite

2. **Integration Tests (moderate)** - Real AWS services, fewer
   - Target: 10% of total test count
   - Test interactions between components
   - May invoke external services (AWS DynamoDB, S3, etc.)
   - Slower to run (multiple seconds)
   - Should be limited in scope (10-20% of tests)

3. **Functional/API Tests (minimal)** - End-to-end, limited scope
   - Target: 5-10% of tests
   - Test critical user workflows
   - Slowest and most expensive

## Coverage Requirements

**ADR-005** specifies an **80% minimum coverage target** for production-grade open-source projects.

## Required Test Coverage for Lambda Functions

According to **ADR-005**, every Lambda handler must test:

1. **Happy Path** - Successful execution with valid input
2. **Input Validation** - Handler rejects invalid input
3. **AWS Service Errors** - Handler handles AWS service failures gracefully
4. **Edge Cases** - Empty responses, null/undefined values, boundary conditions, timeout scenarios
5. **Error Recovery and Logging** - Errors logged with context, retry logic, graceful degradation

## Testing Framework and Structure

According to **ADR-003**:

**Framework:** pytest with pytest-cov for coverage reporting

**Directory structure:**
```
lambda/
├── [function-name]/
│   ├── handler.py
│   └── requirements.txt
└── tests/
    ├── conftest.py                 # Pytest configuration
    ├── pytest.ini                  # Pytest settings
    ├── Makefile                    # Test runner commands
    ├── unit/
    │   ├── __init__.py
    │   └── test_*.py
    ├── integration/
    │   ├── __init__.py
    │   └── test_*_flow.py
    └── fixtures/
        ├── __init__.py
        └── sample_data.py
```

## Test Naming Conventions

According to **ADR-003**:

- Test files: `test_<lambda_name>.py` and `test_<lambda_name>_flow.py`
- Test functions: `test_<function_name>_<scenario>`
- Examples:
  - `test_create_character_with_valid_input`
  - `test_create_character_with_missing_name`
  - `test_dynamodb_put_handles_throttling`

## Testing Patterns

### 1. Arrange-Act-Assert Pattern

According to **ADR-003**:

```python
def test_create_character_with_valid_input():
    # Arrange - Set up test data
    input_data = {
        "name": "Elvira",
        "gender": "female",
        "species": "Elf"
    }

    # Act - Execute the function
    result = create_character(input_data)

    # Assert - Verify the result
    assert result["character_id"] is not None
    assert result["name"] == "Elvira"
    assert result["status"] == "pending_generation"
```

### 2. AWS Service Mocking

According to **ADR-005**, use **moto for AWS mocking (NOT @patch decorators)**:

```python
from moto import mock_aws

@mock_aws()
def test_dynamodb_integration():
    # Create real (mocked) DynamoDB table
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(...)

    # Test handler with mocked AWS
    result = handler(event, context)
    assert result is not None
```

### 3. Dynamic Module Loading

According to **ADR-005**, for test isolation:

```python
import importlib.util

# Load handler module to avoid import-time side effects
handler_path = os.path.join(os.path.dirname(__file__), '../../my-lambda/handler.py')
spec = importlib.util.spec_from_file_location("my_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['my_handler'] = handler_module
spec.loader.exec_module(handler_module)
```

### 4. Testing AI-Powered Features

According to **ADR-005**, for Lambda functions that call Bedrock:

```python
@patch('handler_module.bedrock_client')
def test_ai_feature(mock_bedrock):
    # Arrange: Mock AI response
    mock_response = Mock()
    mock_response.__getitem__.return_value = Mock(
        read=Mock(return_value=json.dumps({
            "content": [{"text": "Expected AI response"}]
        }).encode())
    )
    mock_bedrock.invoke_model.return_value = mock_response

    # Act & Assert
    result = generate_summary(text)
    assert "Expected AI response" in result
```

Test AI error handling including throttling errors, invalid model responses, token limit exceeded, and service unavailable scenarios.

## Enforcement

According to the **tests-summary for outcome-ops-ai-assist**:

- Automated PR checks run as part of PR validation
- Scans changed files to identify new handlers
- Matches handlers with corresponding test files
- **Fails PR if tests are missing** (enforces ADR-003)
- Tests must pass before committing and merging

## Running Tests

According to **ADR-003**:

```bash
# Navigate to lambda tests directory
cd lambda/tests

# Run all tests
make test

# Run only unit tests
make test-unit

# Run only integration tests
make test-integration

# Run with coverage report
make test-coverage

# Run specific test file
pytest unit/test_ingest_docs.py -v

# Run specific test
pytest unit/test_ingest_docs.py::TestComputeContentHash::test_compute_content_hash_valid_content -v
```
- # Query: Test coverage requirements and best practices

# Test Coverage Requirements and Best Practices

## Coverage Requirements

According to **ADR-005: Testing Standards for Lambda Functions**, the project has established:

- **Minimum Coverage Target: 80%**
- **Rationale**: Industry standard for production-grade open-source projects that builds trust with users and contributors while remaining sustainable (not chasing 100% which has diminishing returns)

According to the **Coverage Audit (January 6, 2025)**, the current coverage is **55%** (2157 statements, 960 missed), creating a **gap of 25 percentage points** requiring **540 additional statements to be covered** to reach the 80% target.

**ADR-003: Testing Standards** also mentions a **minimum 70%+ code coverage** expectation, though ADR-005 supersedes this with the 80% target.

## Test Pyramid Structure

According to **ADR-005**, follow the testing pyramid with emphasis on unit tests:

1. **Unit Tests (majority)** - Fast, isolated, many
   - Test individual functions and classes
   - Mock all AWS services using moto
   - **Target: 90% of total test count**

2. **Integration Tests (moderate)** - Real AWS services, fewer
   - Test interactions between components
   - Use LocalStack or real dev AWS resources
   - **Target: 10% of total test count**

3. **Functional Tests (minimal)** - End-to-end, limited scope
   - Test critical user workflows
   - Target: As needed for critical paths

**ADR-003** provides similar guidance with different percentages:
- Unit tests: 60-70% of test suite
- Integration tests: 10-20% of tests
- Functional/API tests: 5-10% of tests

## Required Test Coverage for All Lambda Functions

According to **ADR-005**, every Lambda handler must test:

### 1. **Happy Path (primary functionality)**
Test successful execution with valid input

### 2. **Input Validation**
Test handler rejects invalid input (missing required fields, etc.)

### 3. **AWS Service Errors**
Test handler handles AWS service failures gracefully (DynamoDB errors, S3 failures, etc.)

### 4. **Edge Cases**
- Empty responses from dependencies
- Null/undefined values in input
- Boundary conditions (empty lists, zero values, max limits)
- Timeout scenarios for long-running operations

### 5. **Error Recovery and Logging**
- Verify errors are logged with sufficient context
- Test retry logic (if applicable)
- Verify graceful degradation

## Best Practices

### Testing Philosophy (ADR-003)

**Core principles:**
- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- **A story is DONE when tests covering new functionality are written, passing locally, and passing in CI**
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)

### Test Structure (ADR-003)

Use the **Arrange-Act-Assert pattern**:
- **Arrange** - Set up test data
- **Act** - Execute the function
- **Assert** - Verify the result

### Test Naming Convention (ADR-003)
- Format: `test_<function_name>_<scenario>`
- Examples: `test_create_character_with_valid_input`, `test_dynamodb_put_handles_throttling`

### AWS Service Mocking (ADR-005)

**Use moto for AWS mocking (NOT @patch decorators):**
```python
from moto import mock_aws

@mock_aws()
def test_dynamodb_integration():
    # Create real (mocked) DynamoDB table
    dynamodb = boto3.client("dynamodb", region_name="us-west-2")
    dynamodb.create_table(...)
    
    # Test handler with mocked AWS
    result = handler(event, context)
```

### Testing AI-Powered Features (ADR-005)

For Lambda functions that call Bedrock (Claude, Titan embeddings):

- **Mock AI responses for deterministic tests** using `@patch` decorators
- **Test AI error handling**: Throttling errors (retry logic), invalid model responses, token limit exceeded, service unavailable

### Test Organization (ADR-003)

**Directory structure:**
```
lambda/tests/
├── unit/                      # Unit tests (fast, isolated)
│   ├── test_handler_name.py
│   └── test_module_name.py
├── integration/               # Integration tests (AWS services)
│   └── test_workflow_name.py
├── fixtures/                  # Shared test data
│   └── sample_data.py
├── conftest.py               # Pytest configuration
```

### Test Execution (ADR-003)

Unit tests should be:
- **Fast** (< 100ms per test ideally)
- **Isolated** (no external service dependencies)
- **Independent** (can run in any order)

## Enforcement

According to the **Test Suite Summary**, testing standards are enforced through:
- **Automated PR Checks** that scan changed files to identify new handlers
- **Validation** that handlers have corresponding test files
- **PR blocking** if tests are missing (enforces ADR-003)
- Tests must be written before a story is considered DONE

## Implementation Steps

### Step 1: Create Pydantic request/response schemas ⏳

**Status:** pending
**Description:** Define input validation schema for limit parameter (1-100, default 10) and response schema for document list with metadata fields

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic validation patterns for integer constraints and defaults
- DynamoDB document metadata schema examples

---

### Step 2: Implement main Lambda handler ⏳

**Status:** pending
**Description:** Create handler.py with environment variable loading, DynamoDB client initialization, request validation, and response formatting following Lambda handler standards

**Files:**
- `lambda/list-recent-docs/handler.py`

**KB Queries:**
- DynamoDB scan with filter expression examples for attribute_exists
- Lambda handler patterns for loading SSM parameters

---

### Step 3: Create requirements.txt ⏳

**Status:** pending
**Description:** Define Python dependencies including boto3 and pydantic with version constraints matching other Lambda handlers

**Files:**
- `lambda/list-recent-docs/requirements.txt`

---

### Step 4: Create unit tests for happy path scenarios ⏳

**Status:** pending
**Description:** Write tests for valid requests with different limit values, default limit behavior, and empty table scenario using moto for DynamoDB mocking

**Files:**
- `lambda/tests/unit/test_list_recent_docs.py`

**KB Queries:**
- Moto examples for mocking DynamoDB scan operations
- Pytest patterns for testing Lambda handlers with multiple scenarios

---

### Step 5: Add unit tests for validation and error cases ⏳

**Status:** pending
**Description:** Extend test file with validation error tests (invalid limit values), DynamoDB error handling, and edge cases (fewer documents than limit, multiple document types)

**KB Queries:**
- Testing DynamoDB error scenarios with moto
- Pydantic validation error testing patterns

---

### Step 6: Add Terraform Lambda configuration ⏳

**Status:** pending
**Description:** Add list-recent-docs Lambda module to terraform/lambda.tf with DynamoDB scan/query permissions, SSM parameter access, and environment variables

**KB Queries:**
- IAM policy examples for DynamoDB scan and query operations
- Terraform Lambda module configuration for direct invoke handlers

---

### Step 7: Create handler documentation ⏳

**Status:** pending
**Description:** Document handler purpose, request/response formats, environment variables, IAM permissions, error scenarios, and usage examples

**Files:**
- `docs/lambda-list-recent-docs.md`

---
