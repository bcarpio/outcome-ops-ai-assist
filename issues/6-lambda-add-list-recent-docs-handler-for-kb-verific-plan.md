# Code Generation Plan

**Issue:** #6 - [Lambda]: Add list-recent-docs handler for KB verification
**Branch:** `6-lambda-add-list-recent-docs-handler-for-kb-verific`
**Repository:** bcarpio/outcome-ops-ai-assist
**Created:** 2025-11-10T13:11:05.377928

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
**Description:** Define Pydantic models for request validation (limit parameter) and response structure (documents list with metadata). This establishes the data contract for the handler.

**Files:**
- `lambda/list-recent-docs/schemas.py`

**KB Queries:**
- Pydantic validation patterns for DynamoDB pagination parameters

**Completed:** 2025-11-10T13:13:04.667874

**Cost:** $0.072222 (729 input tokens, 4669 output tokens)

---

### Step 2: Create DynamoDB query utility functions ✅

**Status:** completed
**Description:** Implement utility functions to load table name from SSM, scan DynamoDB with filter for documents with embeddings, and sort results by timestamp. Keep business logic separate from handler.

**Files:**
- `lambda/list-recent-docs/db_utils.py`

**KB Queries:**
- DynamoDB scan with FilterExpression for attribute_exists patterns
- Sorting DynamoDB scan results by timestamp in Python

**Completed:** 2025-11-10T13:15:32.025190

**Cost:** $0.089313 (1451 input tokens, 5664 output tokens)

---

### Step 3: Create main Lambda handler ✅

**Status:** completed
**Description:** Implement the main handler function that validates input using Pydantic schemas, calls DynamoDB utility functions, formats response, and handles errors according to Lambda handler standards.

**Files:**
- `lambda/list-recent-docs/handler.py`

**Completed:** 2025-11-10T13:17:52.838538

**Cost:** $0.121725 (395 input tokens, 8036 output tokens)

---

### Step 4: Create Lambda requirements file ✅

**Status:** completed
**Description:** Define Python dependencies including boto3, pydantic, and any other required packages with version pinning.

**Files:**
- `lambda/list-recent-docs/requirements.txt`

**Completed:** 2025-11-10T13:18:15.728866

**Cost:** $0.002928 (381 input tokens, 119 output tokens)

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

## Total Cost

**Total:** $0.286188
**Input Tokens:** 2,956
**Output Tokens:** 18,488