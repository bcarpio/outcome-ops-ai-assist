# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-10T17:15:31.871513

## Issue Description

# Code Generation Request

## Cached Standards

<!-- Standards retrieved during plan generation (avoid re-querying) -->

### Lambda Standards

- # Query: Lambda handler standards and patterns

### Terraform Standards

- # Query: Terraform Lambda module configuration standards

### Testing Standards

- # Query: Test coverage requirements and best practices

## Implementation Steps

### Step 1: Create Pydantic request/response schemas ✅

**Status:** completed
**Description:** Create Pydantic models for request validation (limit parameter) and response structure (documents list with metadata). Include Field validators for limit constraints (1-100).

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic Field validators for integer range constraints
- DynamoDB response schema patterns for list operations

**Completed:** 2025-11-10T17:19:41.981204

**Cost:** $0.044712 (1284 input tokens, 2724 output tokens)

---

### Step 2: Create main Lambda handler with DynamoDB scan logic 🔄

**Status:** in_progress
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

## Total Cost

**Total:** $0.044712
**Input Tokens:** 1,284
**Output Tokens:** 2,724