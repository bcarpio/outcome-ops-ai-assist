# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-09T18:45:36.074220

## Issue Description

# Code Generation Request

## Cached Standards

<!-- Standards retrieved during plan generation (avoid re-querying) -->

### Lambda Standards

- # Query: Lambda handler standards and patterns

### Terraform Standards

- # Query: Terraform Lambda module configuration standards

### Testing Standards

- # Query: Testing standards and patterns

## Implementation Steps

### Step 1: Create Pydantic request/response schemas ✅

**Status:** completed
**Description:** Define input validation schema for limit parameter (1-100, default 10) and response schema for document list with metadata fields

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic validation patterns for integer constraints and defaults
- DynamoDB document metadata schema examples

**Completed:** 2025-11-09T18:48:14.814668

**Cost:** $0.069384 (1588 input tokens, 4308 output tokens)

---

### Step 2: Implement main Lambda handler 🔄

**Status:** in_progress
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

## Total Cost

**Total:** $0.069384
**Input Tokens:** 1,588
**Output Tokens:** 4,308