# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-10T13:11:05.377928

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

Based on **ADR-004: Lambda Handler Standards**, the project defines comprehensive standards across three Lambda types:

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

### Required Elements:
1. **Structured logging** with handler name prefix
2. **AWS clients initialized once per container** (outside handler function)
3. **Environment variables loaded at module level**
4. **Try-except with `exc_info=True`** for unexpected errors
5. **Type hints** on functions
6. **Docstrings** on handler and business logic functions

### Standard Structure:
```python
# lambda/my_handler/handler.py
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

## API Gateway Handler Pattern

According to ADR-004, API Gateway handlers must include:

### 1. **Authentication**
```python
claims = decode_token(event)
user_email = claims.get("email") if claims else None
if not user_email:
    return _response(401, {"error": "Unauthorized"})
```

### 2. **Pydantic Validation**
```python
class UpdateProfileInput(BaseModel):
    display_name: str = Field(..., min_length=1, max_length=100)
    bio: Optional[str] = Field(None, max_length=500)

try:
    input_data = UpdateProfileInput(**body)
except ValidationError as e:
    return _response(400, {"error": "Validation error", "details": e.errors()})
```

### 3. **CORS Response Helper**
```python
def _response(status_code, body):
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

### 4. **SSM Parameter Store Configuration**
```python
# Load configuration from SSM (happens once per container)
table_param = f"/{env}/{app_name}/dynamodb/table"
TABLE_NAME = ssm.get_parameter(Name=table_param)["Parameter"]["Value"]
table = dynamodb.Table(TABLE_NAME)
```

## Size Guidelines

According to the **lambda-size-analysis.md** document:

- **Target**: Handlers should be under **10KB** for effective code-map generation
- **Acceptable**: Under 8KB is ideal (70% of handlers meet this)
- **Refactoring needed**: 10 handlers exceed 10KB and require refactoring

### Refactoring Pattern
When handlers exceed size limits, follow the **character_feed refactor pattern**:
1. Phase 1: Extract utility functions
2. Phase 2: Create route modules
3. Phase 3: Refactor main handler
4. Phase 4: Update tests
5. Phase 5: Deploy

## Development Workflow

According to **claude-guidance.md**, before implementing any Lambda:

1. **Query standards first**:
```bash
/home/bcarpio/Projects/github/outcome-ops-ai-assist/scripts/outcome-ops-assist "What are our Lambda handler standards?"
```

2. **Review existing similar handlers** for patterns
3. **Check relevant ADRs** for architectural decisions
4. **When in doubt, ask the user**

### Key Principle
**"Query Standards Before Implementation"** - Always consult the knowledge base before writing code to ensure compliance with established patterns.
- # Query: Lambda error handling and logging best practices

# Lambda Error Handling and Logging Best Practices

Based on the provided context, here are the standards for Lambda error handling and logging:

## Core Logging Standards (All Lambda Types)

According to **ADR-004: Lambda Handler Standards**, all Lambda handlers MUST follow these logging patterns:

### 1. Structured Logging Setup
```python
import logging

# Setup logging
logger = logging.getLogger()
logger.setLevel(logging.INFO)
```

### 2. Handler Name Prefix
- Use structured logging with handler name prefix in all log messages
- Example: `logger.info(f"[handler-name] Processing request: {json.dumps(event)}")`

### 3. Error Logging with Stack Traces
All handlers must include try-except blocks with `exc_info=True` for unexpected errors:

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

The `exc_info=True` parameter ensures full stack traces are captured in CloudWatch Logs.

## Event-Driven Handler Error Handling

According to **ADR-004**, event-driven handlers (SQS, EventBridge, S3, scheduled) have specific requirements:
- **Error handling**: Must implement proper error handling
- **Retry logic**: Should include retry logic
- **Idempotency**: Must be idempotent

### Example from Operational Alarms Handler

The **moderation_miss_alert handler** (Document 3) demonstrates error handling for event-driven Lambdas:

```python
# Error Handling Pattern
try:
    # Process event
    sns_client.publish(...)
    s3_client.put_object(...)
except Exception as e:
    logger.error(f"[❌ Alert Failure]: {str(e)}")
    # Does NOT throw exception - errors are caught and logged
```

**Key behaviors:**
- ✅ Lambda continues processing remaining records
- ✅ Does not fail the entire batch
- ❌ No retry mechanism for failed alerts (noted as limitation)
- Console logs use clear indicators: `[✅ Alert Sent]` for success, `[❌ Alert Failure]` for errors

## Monitoring Best Practices

According to the **Query KB Lambda documentation** (Document 4), key log messages should indicate pipeline stages:

```python
logger.info("[query-kb] Processing query: '...' (top X results)")  # Started
logger.info("[query-kb] Invoking vector-query Lambda...")  # Stage 1 start
logger.info("[query-kb] Found X relevant documents")  # Stage 1 complete
logger.info("[query-kb] No relevant documents found")  # Empty results
logger.info("[query-kb] Invoking ask-claude Lambda...")  # Stage 2 start
logger.info("[query-kb] Successfully generated answer with X sources")  # Complete
```

## Summary of Requirements

From **ADR-004**, all Lambda handlers MUST include:
1. ✅ Structured logging with handler name prefix
2. ✅ Logger configured at module level (outside handler function)
3. ✅ Try-except blocks with `exc_info=True` for unexpected errors
4. ✅ Log the incoming event at INFO level
5. ✅ Log errors at ERROR level with full context

**Note:** The context does not provide specific guidance on:
- Dead letter queue configuration
- Retry policies
- Alarm thresholds
- Log retention policies
- Specific error codes or error response formats for internal workers

### Terraform Standards

- # Query: Terraform Lambda module configuration standards

# Terraform Lambda Module Configuration Standards

Based on the provided context, here are the standards for configuring Terraform Lambda modules:

## 1. Use Community Modules

**All new Lambda functions MUST use the `terraform-aws-modules/lambda/aws` community module** (from ADR-001). This module is:
- Battle-tested and maintained by the Terraform community
- Pre-configured with security best practices
- Provides consistent interfaces across AWS services

## 2. Version Pinning (Required)

**All module source blocks MUST include explicit version pinning:**

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

(Note: Document 1 shows version "8.1.2" while Document 2 shows "7.2.1" - use exact pinning appropriate to your environment)

## 3. Least Privilege IAM Policies

**Every Lambda function must operate with minimum required permissions** using the `attach_policy_statements` pattern:

```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "my-function"
  handler       = "index.handler"
  runtime       = "python3.12"

  attach_policy_statements = true
  policy_statements = {
    # Only specific DynamoDB actions needed
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",    # Only read what's needed
        "dynamodb:Query"       # Not Scan, not PutItem unless required
      ]
      resources = [
        module.main_table.dynamodb_table_arn  # Specific table, not all tables
      ]
    }

    # Only specific S3 actions and paths
    s3 = {
      effect = "Allow"
      actions = [
        "s3:GetObject"        # Read only, not Write
      ]
      resources = [
        "${module.images_bucket.s3_bucket_arn}/*"  # Specific bucket, not all buckets
      ]
    }
  }
}
```

**Least privilege checklist (from ADR-001):**
- Specify exact actions (not *)
- Specify exact resources (not arn:aws:service:region:account:*)
- Use module outputs for ARNs (not hardcoded strings)
- Document why each permission is needed in comments
- Review quarterly - remove unused permissions

## 4. CloudWatch Logs (Important)

**DO NOT create separate `aws_cloudwatch_log_group` resources** when using the Lambda module.

According to ADR-001 Section 3a:
- The `terraform-aws-modules/lambda/aws` module **automatically creates a CloudWatch log group**
- Creating a separate resource causes duplicate log groups

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
  # ... configuration
}

# The Lambda module automatically creates the log group
```

## 5. Query Standards Before Implementation

According to the Claude Guidance documents, before writing Terraform code:

1. Query `outcome-ops-assist` for relevant standards
2. Review the standards returned by the knowledge base
3. Apply those standards to your implementation

```bash
outcome-ops-assist "What are our Terraform module standards?"
```
- # Query: Terraform resource naming conventions

Based on the provided context, I can identify the following Terraform resource naming conventions:

## Module Naming Convention

According to **ADR-002** and the examples in **ADR-001**, Lambda functions should follow this pattern:

```hcl
function_name = "${var.environment}-${var.app_name}-example-function"
```

This creates names like:
- `dev-outcome-ops-ai-assist-my-function` (for dev environment)
- `prd-outcome-ops-ai-assist-my-function` (for production environment)

## Plan File Naming Convention

According to **ADR-004**, plan output files should follow this naming pattern:

- Dev environment: `terraform.dev.out`
- Production environment: `terraform.prd.out`

**ADR-004** explicitly states: "Never commit plan files to git (already in .gitignore)"

## SSM Parameter Naming Convention

From the examples in **ADR-001** and **ADR-002**, SSM parameters follow this pattern:

```
arn:aws:ssm:${var.aws_region}:${account_id}:parameter/${var.environment}/${var.app_name}/*
```

This creates paths like `/dev/outcome-ops-ai-assist/github/token` as shown in the **deployment.md** document.

## Limitations

The provided context does not specify naming conventions for:
- DynamoDB tables
- S3 buckets
- SQS queues
- API Gateway resources
- CloudFront distributions
- IAM roles (beyond the general pattern shown)

The documents focus more on module usage patterns and security practices rather than comprehensive naming standards for all resource types.

### Testing Standards

- # Query: Testing standards and patterns

# Testing Standards and Patterns

Based on the provided documentation, here are the comprehensive testing standards and patterns:

## Testing Philosophy and Core Principles

According to **ADR-003**, a story is DONE when tests covering new functionality are written, passing locally, and passing in CI. Core principles include:

- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)
- Follow the Test Pyramid (many unit tests, fewer integration tests, minimal functional tests)

## Coverage Targets

**ADR-005** establishes:
- **Minimum coverage: 80%** (industry standard for production-grade open-source projects)
- Rationale: Builds trust with users/contributors, catches critical bugs, sustainable (not chasing 100%)

**ADR-003** mentions a **70%+ coverage target** for the test suite.

## Test Pyramid Structure

According to **ADR-005**, the test distribution should be:

1. **Unit Tests (90% of total)** - Fast, isolated, many
   - Test individual functions and classes
   - Mock all AWS services using moto
   - Should be fast (< 100ms per test ideally)
   - Should comprise 60-70% of test suite

2. **Integration Tests (10% of total)** - Real AWS services, fewer
   - Test interactions between components
   - May invoke external services (AWS DynamoDB, S3, etc.)
   - Slower to run (multiple seconds)
   - Should be limited in scope (10-20% of tests)

3. **Functional Tests (minimal)** - End-to-end, limited scope
   - Test critical user workflows
   - Slowest and most expensive (5-10% of tests)
   - Used for critical user journeys

## Testing Framework and Structure

**Framework:** pytest with pytest-cov for coverage reporting (per **ADR-003**)

**Directory structure:**
```
lambda/tests/
├── conftest.py          # Pytest configuration
├── pytest.ini           # Pytest settings
├── Makefile            # Test runner commands
├── unit/               # Unit tests (fast, isolated)
│   └── test_*.py
├── integration/        # Integration tests (AWS services)
│   └── test_*_flow.py
└── fixtures/           # Shared test data
    └── sample_data.py
```

## Required Test Coverage for Lambda Functions

**ADR-005** specifies that every Lambda handler must test:

1. **Happy Path** - Successful execution with valid input
2. **Input Validation** - Handler rejects invalid input
3. **AWS Service Errors** - Graceful handling of AWS service failures
4. **Edge Cases** - Empty responses, null values, boundary conditions, timeouts
5. **Error Recovery and Logging** - Errors logged with context, retry logic, graceful degradation

## Testing Patterns

### 1. Test Structure: Arrange-Act-Assert Pattern

According to **ADR-003**:
```python
def test_create_character_with_valid_input():
    # Arrange - Set up test data
    input_data = {"name": "Elvira", "gender": "female"}
    
    # Act - Execute the function
    result = create_character(input_data)
    
    # Assert - Verify the result
    assert result["character_id"] is not None
    assert result["name"] == "Elvira"
```

### 2. Test Naming Convention

**ADR-003** specifies:
- Format: `test_<function_name>_<scenario>`
- Examples:
  - `test_create_character_with_valid_input`
  - `test_create_character_with_missing_name`
  - `test_dynamodb_put_handles_throttling`

### 3. AWS Service Mocking

**ADR-005** emphasizes using **moto for AWS mocking (NOT @patch decorators)**:

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

### 4. Dynamic Module Loading

**ADR-005** recommends dynamic module loading for test isolation:

```python
import importlib.util

# Load handler module to avoid import-time side effects
handler_path = os.path.join(os.path.dirname(__file__), '../../my-lambda/handler.py')
spec = importlib.util.spec_from_file_location("my_handler", handler_path)
handler_module = importlib.util.module_from_spec(spec)
sys.modules['my_handler'] = handler_module
spec.loader.exec_module(handler_module)
```

### 5. Testing AI-Powered Features

For Lambda functions calling Bedrock, **ADR-005** recommends:

**Mock AI responses for deterministic tests:**
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

**Test AI error handling:**
- Throttling errors (retry logic)
- Invalid model responses
- Token limit exceeded
- Service unavailable

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
```

## Best Practices

**ADR-003** emphasizes:
- **Test one thing per test** - Each test should verify a single behavior
- **Test negative conditions** - Bad inputs, error cases, exceptions
- **Test error messages** - They must be useful and clear
- Tests should be fast for unit tests (< 100ms ideally)
- Use mocking/stubbing for dependencies in unit tests
- # Query: Test coverage requirements and best practices

# Test Coverage Requirements and Best Practices

## Coverage Requirements

According to **ADR-005: Testing Standards for Lambda Functions**, the project has established:

- **Minimum Coverage Target: 80%**
  - This is the industry standard for production-grade open-source projects
  - Builds trust with users and contributors
  - Catches critical bugs before production
  - Considered sustainable (not chasing 100% which has diminishing returns)

**Current Status** (from the Coverage Audit - January 6, 2025):
- Current coverage: **55%** (2157 statements, 960 missed)
- Gap: **25 percentage points** (540 additional statements needed to reach 80%)

## Test Pyramid Structure

According to ADR-005, follow the testing pyramid with emphasis on unit tests:

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

ADR-003 provides similar guidance with a slightly different breakdown:
- Unit tests: 60-70% of test suite
- Integration tests: 10-20% of tests
- Functional/API tests: 5-10% of tests

## Required Test Coverage for All Lambda Functions

According to ADR-005, every Lambda handler must test:

### 1. **Happy Path (primary functionality)**
```python
def test_handler_success():
    """Test successful execution with valid input"""
    event = {"query": "test query"}
    response = handler(event, context)
    assert response["statusCode"] == 200
    assert "body" in response
```

### 2. **Input Validation**
```python
def test_handler_missing_required_field():
    """Test handler rejects invalid input"""
    event = {}  # Missing required field
    response = handler(event, context)
    assert response["statusCode"] == 400
    assert "error" in json.loads(response["body"])
```

### 3. **AWS Service Errors**
```python
@patch('handler_module.dynamodb_client')
def test_handler_dynamodb_error(mock_dynamodb):
    """Test handler handles AWS service failures gracefully"""
    mock_dynamodb.get_item.side_effect = ClientError(
        {"Error": {"Code": "ServiceException"}}, "get_item"
    )
    response = handler(event, context)
    assert response["statusCode"] == 500
```

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

### Testing Philosophy (from ADR-003)

**Core principles:**
- A story is DONE when tests covering new functionality are written, passing locally, and passing in CI
- Code should be written to be testable (favor single-purpose functions)
- Tests should be written close in time to writing the code (not after)
- All tests must pass before committing
- Test negative conditions (bad inputs, error cases, exceptions)
- Test error messages (they must be useful and clear)
- Run tests continuously during development (shift-left approach)

### Test Structure (from ADR-003)

**Use Arrange-Act-Assert pattern:**

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

### AWS Service Mocking (from ADR-005)

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
    assert result is not None
```

### Testing AI-Powered Features (from ADR-005)

For Lambda functions that call Bedrock:

**Mock AI responses for deterministic tests:**

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

**Test AI error handling:**
- Throttling errors (retry logic)
- Invalid model responses
- Token limit exceeded
- Service unavailable

### Test Naming Convention (from ADR-003)

- Format: `test_<function_name>_<scenario>`
- Examples:
  - `test_create_character_with_valid_input`
  - `test_create_character_with_missing_name`
  - `test_dynamodb_put_handles_throttling`

### Test Organization (from ADR-003)

```
lambda/tests/
├── unit/                      # Unit tests (fast, isolated)
│   ├── test_handler_name.py
│   └── test_module_name.py
├── integration/               # Integration tests (AWS services)
│   └── test_workflow_name.py
├── fixtures/                  # Shared test data
│   └── sample_data.py
└── conftest.py               # Pytest configuration
```

## Implementation Steps

### Step 1: Create Pydantic request/response schemas ⏳

**Status:** pending
**Description:** Define Pydantic models for request validation (limit parameter) and response structure (documents list with metadata). This establishes the data contract for the handler.

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic validation patterns for DynamoDB pagination parameters

---

### Step 2: Create DynamoDB query utility functions ⏳

**Status:** pending
**Description:** Implement utility functions to load table name from SSM, scan DynamoDB with filter for documents with embeddings, and sort results by timestamp. Keep business logic separate from handler.

**Files:**
- `lambda/list-recent-docs/db_utils.py`

**KB Queries:**
- DynamoDB scan with FilterExpression for attribute_exists patterns
- Sorting DynamoDB scan results by timestamp in Python

---

### Step 3: Create main Lambda handler ⏳

**Status:** pending
**Description:** Implement the main handler function that validates input using Pydantic schemas, calls DynamoDB utility functions, formats response, and handles errors according to Lambda handler standards.

**Files:**
- `lambda/list-recent-docs/handler.py`

---

### Step 4: Create Lambda requirements file ⏳

**Status:** pending
**Description:** Define Python dependencies including boto3, pydantic, and any other required packages with version pinning.

**Files:**
- `lambda/list-recent-docs/requirements.txt`

---

### Step 5: Create unit tests for schemas ⏳

**Status:** pending
**Description:** Test Pydantic validation for valid/invalid limit values, default values, and edge cases (0, 101, missing limit).

**Files:**
- `lambda/tests/unit/test_list_recent_docs_schemas.py`

**KB Queries:**
- Testing Pydantic validation errors and edge cases

---

### Step 6: Create unit tests for DynamoDB utilities ⏳

**Status:** pending
**Description:** Test DynamoDB query functions with moto mocking: successful scans, empty results, error handling, and filtering logic.

**Files:**
- `lambda/tests/unit/test_list_recent_docs_db_utils.py`

**KB Queries:**
- Moto patterns for mocking DynamoDB scan operations with filters

---

### Step 7: Create unit tests for handler ⏳

**Status:** pending
**Description:** Test main handler function covering happy path, input validation errors, DynamoDB errors, edge cases (no documents, fewer than limit), and error logging.

**Files:**
- `lambda/tests/unit/test_list_recent_docs_handler.py`

---

### Step 8: Add Terraform Lambda configuration ⏳

**Status:** pending
**Description:** Add Lambda function module to terraform/lambda.tf with IAM permissions for DynamoDB scan/query and SSM parameter access, environment variables, and proper version pinning.

**KB Queries:**
- IAM policy statements for DynamoDB scan with specific table ARN
- Lambda module configuration for SSM parameter access with KMS decrypt

---

### Step 9: Create handler documentation ⏳

**Status:** pending
**Description:** Document the handler's purpose, request/response formats, environment variables, IAM permissions, error handling, and usage examples.

**Files:**
- `docs/lambda-list-recent-docs.md`

---
