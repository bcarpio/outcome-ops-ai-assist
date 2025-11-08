---
name: List Recent KB Documents - Example Issue
about: Example code generation request for listing recent knowledge base documents
title: '[Lambda]: Add list-recent-docs handler for KB verification'
labels: ['approved-for-generation']
assignees: ''
---

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
