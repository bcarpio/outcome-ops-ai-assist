# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-10T17:15:31.871513

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

All Lambda handlers MUST follow these patterns:

```python
import boto3
import os
import json
import logging
from typing import Any, Dict

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

**Core requirements:**
- Structured logging with handler name prefix
- AWS clients initialized once per container (outside handler)
- Environment variables loaded at module level
- Try-except with `exc_info=True` for unexpected errors
- Type hints on functions
- Docstrings on handler and business logic functions

## API Gateway Handler Pattern

For API Gateway handlers specifically, ADR-004 provides a complete example including:

1. **Authentication**: JWT token validation using `decode_token(event)`
2. **Input Validation**: Pydantic models with Field validators
3. **Authorization**: User permission checks
4. **CORS Response Helper**: Standardized response format with CORS headers
5. **Configuration Loading**: SSM Parameter Store for environment-specific config

Example CORS response helper:
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

According to **lambda-size-analysis.md**, handlers should stay under **10KB** for effective code-map generation. The analysis shows that 70% of handlers (55 out of 79) are under 8KB, which is considered good for maintainability.

## Development Workflow

According to **claude-guidance.md**, before implementing any Lambda handler, developers should:

1. Query `outcome-ops-assist` for Lambda handler standards
2. Review the standards returned by the knowledge base
3. Read existing similar Lambda handlers for patterns
4. Review relevant documentation in the `docs/` directory
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
All log messages must include a handler name prefix for traceability:

```python
logger.info(f"[handler-name] Processing request: {json.dumps(event)}")
logger.error(f"[handler-name] Unexpected error: {str(e)}", exc_info=True)
```

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
Use a helper function to return consistent error responses with CORS headers:

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
- **401 Unauthorized**: `return _response(401, {"error": "Unauthorized"})`
- **400 Validation Error**: `return _response(400, {"error": "Validation error", "details": e.errors()})`
- **404 Not Found**: `return _response(404, {"error": "User not found"})`

## Event-Driven Handler Error Handling

According to **Document 3 (Operational Alarms Lambda)**, event-driven handlers should:

### Log Errors Without Failing the Batch
```python
try:
    # Process event
    pass
except Exception as e:
    logger.error(f"[❌ Alert Failure]: {str(e)}")
    # Does NOT throw exception - errors are caught and logged
```

**Key behavior:**
- ✅ Lambda continues processing remaining records
- ✅ Does not fail the entire batch
- ❌ No retry mechanism for failed alerts shown in example

## Monitoring Best Practices

According to **Document 4 (Lambda: Query KB)**, implement comprehensive logging for monitoring:

### Key Log Messages Pattern
```python
logger.info(f"[query-kb] Processing query: '...' (top X results)")  # Started
logger.info(f"[query-kb] Invoking vector-query Lambda...")  # Stage 1 start
logger.info(f"[query-kb] Found X relevant documents")  # Stage 1 complete
logger.info(f"[query-kb] No relevant documents found")  # Empty results
logger.info(f"[query-kb] Successfully generated answer with X sources")  # Complete
```

### CloudWatch Logs Access
```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-query-kb --follow
```

## Summary of Requirements

**All Lambda handlers MUST have:**
1. ✅ Structured logging with `logging.getLogger()`
2. ✅ Handler name prefix in all log messages (e.g., `[handler-name]`)
3. ✅ Try-except blocks with `exc_info=True` for stack traces
4. ✅ Type hints on functions
5. ✅ Docstrings on handler and business logic functions

**API Gateway handlers additionally need:**
- Standardized CORS response helper function
- Consistent error response format with appropriate status codes

**Event-driven handlers should:**
- Log errors without failing the entire batch (when appropriate)
- Continue processing remaining records after errors

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
  version = "8.1.2"  # Exact major.minor.patch version

  # ... configuration
}
```

**Version pinning requirements:**
- Pin exact major.minor.patch versions (e.g., `"8.1.2"`)
- NEVER use pessimistic operators (`~>`), unversioned modules, or `version = "*"`
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
        module.main_table.dynamodb_table_arn  # Specific resources, not wildcards
      ]
    }
  }
}
```

**Least privilege checklist (from ADR-001):**
- Specify exact actions (not `*`)
- Specify exact resources (not `arn:aws:service:region:account:*`)
- Use module outputs for ARNs (not hardcoded strings)
- Document why each permission is needed in comments
- Review quarterly - remove unused permissions

## 4. CloudWatch Logs Handling

**Do NOT create separate CloudWatch log group resources** (from ADR-001). The `terraform-aws-modules/lambda/aws` module automatically creates a CloudWatch log group for Lambda functions.

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

According to the Claude Guidance documents, before writing any Terraform code:

1. Query `outcome-ops-assist` for relevant standards
2. Review the standards returned by the knowledge base
3. Apply those standards to your implementation
4. When in doubt, query for examples or ask the user

### Testing Standards

- # Query: Test coverage requirements and best practices

# Test Coverage Requirements and Best Practices

## Coverage Requirements

According to **ADR-005: Testing Standards for Lambda Functions**, the coverage target is:

- **Minimum 80% coverage** across the codebase
- **Rationale**: Industry standard for production-grade open-source projects that builds trust with users and contributors while remaining sustainable (not chasing 100% which has diminishing returns)

According to **ADR-003: Testing Standards**, there is also a reference to a **70%+ code coverage requirement** that must be met before stories are marked DONE.

### Current Status

According to the **Coverage Audit (January 6, 2025)**:
- **Current Coverage: 55%** (2157 statements, 960 missed)
- **Gap: 25 percentage points** (540 additional statements needed to reach 80%)

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
   - **Target: As needed for critical paths**

According to **ADR-003**, the distribution should be:
- Unit tests: 60-70% of test suite
- Integration tests: 10-20% of tests
- Functional/API tests: 5-10% of tests

## Required Test Coverage for All Lambda Functions

According to **ADR-005**, every Lambda handler must test:

### 1. Happy Path (Primary Functionality)
Test successful execution with valid input, verifying status codes and response structure.

### 2. Input Validation
Test that handlers reject invalid input (missing required fields, malformed data) with appropriate 400 status codes.

### 3. AWS Service Errors
Test graceful handling of AWS service failures (DynamoDB errors, S3 failures, etc.) with proper 500 status codes.

### 4. Edge Cases
- Empty responses from dependencies
- Null/undefined values in input
- Boundary conditions (empty lists, zero values, max limits)
- Timeout scenarios for long-running operations

### 5. Error Recovery and Logging
- Verify errors are logged with sufficient context
- Test retry logic (if applicable)
- Verify graceful degradation

## Best Practices

### Testing Philosophy (ADR-003)

**Core principles:**
- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)
- **A story is DONE when tests covering new functionality are written, passing locally, and passing in CI**

### Test Structure (ADR-003)

Use the **Arrange-Act-Assert pattern**:
```python
def test_create_character_with_valid_input():
    # Arrange - Set up test data
    input_data = {"name": "Elvira", "gender": "female"}
    
    # Act - Execute the function
    result = create_character(input_data)
    
    # Assert - Verify the result
    assert result["character_id"] is not None
```

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

For Lambda functions that call Bedrock:

**Mock AI responses for deterministic tests** using @patch decorators for the Bedrock client.

**Test AI error handling:**
- Throttling errors (retry logic)
- Invalid model responses
- Token limit exceeded
- Service unavailable

### Test Naming Convention (ADR-003)

- Format: `test_<function_name>_<scenario>`
- Examples:
  - `test_create_character_with_valid_input`
  - `test_create_character_with_missing_name`
  - `test_dynamodb_put_handles_throttling`

### Test Organization (ADR-005)

```
lambda/tests/
├── unit/                      # Unit tests (fast, isolated)
│   ├── test_handler_name.py
│   └── test_module_name.py
├── integration/               # Integration tests (AWS services)
│   └── test_workflow_name.py
├── fixtures/                  # Shared test data
│   └── sample_data.py
├── conftest.py
```

### Framework and Tools (ADR-003)

- **Framework**: pytest with pytest-cov for coverage reporting
- **AWS Mocking**: moto library for AWS service mocking
- **Test one thing per test** - each test should verify a single behavior

## Implementation Steps

### Step 1: Create Pydantic request/response schemas ⏳

**Status:** pending
**Description:** Create Pydantic models for request validation (limit parameter) and response structure (documents list with metadata). Include Field validators for limit constraints (1-100).

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic Field validators for integer range constraints
- DynamoDB response schema patterns for list operations

---

### Step 2: Create main Lambda handler with DynamoDB scan logic ⏳

**Status:** pending
**Description:** Implement handler.py with environment variable loading, DynamoDB client initialization, request validation using Pydantic schemas, and scan operation with filter expression for documents with embeddings. Include response formatting and error handling.

**Files:**
- `lambda/list-recent-docs/handler.py`

**KB Queries:**
- DynamoDB scan with FilterExpression for attribute_exists
- Lambda handler patterns for DynamoDB scan operations
- Sorting DynamoDB scan results by timestamp in Python

---

### Step 3: Create requirements.txt for Lambda dependencies ⏳

**Status:** pending
**Description:** Define Python dependencies including boto3, pydantic, and any other required packages for the Lambda function.

**Files:**
- `lambda/list-recent-docs/requirements.txt`

---

### Step 4: Add Terraform Lambda module configuration ⏳

**Status:** pending
**Description:** Add Lambda function module to terraform/lambda.tf using terraform-aws-modules/lambda/aws version 7.14.0. Configure environment variables (ENV, APP_NAME, CODE_MAPS_TABLE), timeout (30s), memory (512MB), and IAM permissions for DynamoDB scan/query and SSM parameter access.

**KB Queries:**
- IAM policy statements for DynamoDB scan and query operations
- SSM parameter reference patterns in Terraform Lambda modules

---

### Step 5: Create unit tests for happy path scenarios ⏳

**Status:** pending
**Description:** Create test functions for successful request handling: valid request with limit=5, valid request with default limit (no limit provided), and empty table returning empty array. Mock DynamoDB scan responses using moto.

**Files:**
- `lambda/tests/unit/test_list_recent_docs.py`

**KB Queries:**
- moto mock_aws patterns for DynamoDB scan operations
- pytest fixtures for DynamoDB table setup with sample documents

---

### Step 6: Create unit tests for input validation errors ⏳

**Status:** pending
**Description:** Create test functions for validation error cases: limit=0 (below minimum), limit=101 (above maximum), and invalid limit type. Verify 400 status codes and error messages match specification.

**KB Queries:**
- Pydantic validation error testing patterns

---

### Step 7: Create unit tests for DynamoDB error handling ⏳

**Status:** pending
**Description:** Create test functions for AWS service errors: DynamoDB table not found, DynamoDB service unavailable, and scan operation throttling. Verify 500 status codes and error logging with exc_info=True.

**KB Queries:**
- moto patterns for simulating DynamoDB service errors
- Testing boto3 ClientError exceptions in Lambda handlers

---

### Step 8: Create unit tests for edge cases ⏳

**Status:** pending
**Description:** Create test functions for edge cases: table has fewer documents than requested limit, multiple document types returned (ADRs, READMEs, code maps), and documents from multiple repositories. Verify correct sorting by timestamp and metadata extraction.

**KB Queries:**
- Testing timestamp-based sorting in Python unit tests

---

### Step 9: Create handler documentation ⏳

**Status:** pending
**Description:** Create comprehensive documentation for the list-recent-docs handler including purpose, request/response formats, environment variables, IAM permissions, error handling, and usage examples via outcome-ops-assist CLI.

**Files:**
- `docs/lambda-list-recent-docs.md`

---
