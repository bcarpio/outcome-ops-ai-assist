# Lambda: List Recent Docs

Lists the most recently ingested knowledge base documents, newest first.

## Purpose

Lets an operator confirm that KB ingestion is working, meaning documents
are actually landing in the vector index after a sync, without opening
the AWS console. Read-only.

## Trigger

- **Invoked by**: CLI (`outcome-ops-assist list-recent-docs`), or direct
  Lambda invocation
- **Invocation type**: Synchronous (RequestResponse)
- **Lambda type**: Internal worker (ADR-004)

## How It Works

1. Load the S3 Vectors bucket and index names from SSM
   (`/{env}/{app}/s3vectors/bucket-name`, `.../index-name`), cached per
   container.
2. Page through `ListVectors` with metadata
   (`shared.retrieval.list_recent_documents`).
3. Keep only chunk 0 of each document, so every document appears once.
   Optionally keep only one workspace.
4. Track the newest `limit` documents by the `timestamp` metadata that
   `process-documents` writes at ingest time (bounded heap, O(limit)
   memory).
5. Return them newest first, with scan statistics.

S3 Vectors has no server-side ordering, so every call walks the whole
index. The walk stops at `MAX_SCAN` vectors (default 200,000), and the
response then sets `truncated: true`.

## Request

```json
{
  "limit": 10,
  "workspace_id": "optional-workspace-id"
}
```

| Field | Type | Required | Rules |
|-------|------|----------|-------|
| `limit` | int | no | 1-100, default 10 |
| `workspace_id` | string | no | non-empty when given |

## Response

```json
{
  "statusCode": 200,
  "body": {
    "documents": [
      {
        "key": "<workspace_id>#github#...#ADR-004.md_chunk_0",
        "workspace_id": "<workspace_id>",
        "repo": "owner/repo",
        "doc_type": "adr",
        "file_path": "workspaces/<workspace_id>/github/owner/repo/docs/adr/ADR-004.md",
        "ingested_at": "2026-11-18T13:19:41.123456+00:00"
      }
    ],
    "count": 1,
    "scanned": 48211,
    "truncated": false
  }
}
```

| Status | Meaning |
|--------|---------|
| 200 | Success (an empty `documents` list means nothing is ingested yet) |
| 400 | Request failed validation; `body.details` lists the field errors |
| 502 | SSM or S3 Vectors call failed; `body.error` names the AWS error code |
| 500 | Unexpected error |

## Configuration

| Env var | Purpose | Default |
|---------|---------|---------|
| `ENV` | Environment prefix for SSM paths | `dev` |
| `APP_NAME` | App prefix for SSM paths | `outcome-ops-ai-assist` |
| `MAX_SCAN` | Max vectors examined per call | `200000` |

## IAM

- `ssm:GetParameter` on the two S3 Vectors parameters only
- `s3vectors:ListVectors`, `s3vectors:GetVectors` on the KB vector bucket

## Infrastructure

`terraform/lambda-list-recent-docs.tf`. Primary region only: the
function writes nothing, so like `query-kb` it has no secondary-region
mirror.
