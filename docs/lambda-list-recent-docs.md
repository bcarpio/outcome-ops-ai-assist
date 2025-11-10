# Lambda Handler: List Recent Documents

## Overview

The `list-recent-docs` Lambda handler provides a RESTful API endpoint to retrieve the most recently uploaded documents from an S3 bucket. It supports pagination and returns document metadata including filenames, upload timestamps, and file sizes.

## Purpose

- Provide a list of recently uploaded documents
- Support pagination for large document collections
- Return structured metadata for each document
- Enable front-end applications to display document lists

## Architecture

```
API Gateway
    |
    v
Lambda Function (list-recent-docs)
    |
    v
S3 Bucket (document-storage)
```

## Request Format

### HTTP Method
`GET`

### Endpoint
`/documents/recent`

### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `limit` | integer | No | 10 | Number of documents to return (1-100) |
| `next_token` | string | No | - | Pagination token from previous response |

### Example Requests

```bash
# Get first 10 documents
curl -X GET "https://api.example.com/documents/recent"

# Get first 20 documents
curl -X GET "https://api.example.com/documents/recent?limit=20"

# Get next page of documents
curl -X GET "https://api.example.com/documents/recent?limit=20&next_token=eyJ0b2tlbiI6ImV4YW1wbGUifQ=="
```

## Response Format

### Success Response (200 OK)

```json
{
  "documents": [
    {
      "filename": "report-2024-01-15.pdf",
      "uploaded_at": "2024-01-15T14:30:00Z",
      "size_bytes": 1048576,
      "s3_key": "documents/report-2024-01-15.pdf"
    },
    {
      "filename": "invoice-12345.pdf",
      "uploaded_at": "2024-01-15T12:15:00Z",
      "size_bytes": 524288,
      "s3_key": "documents/invoice-12345.pdf"
    }
  ],
  "count": 2,
  "next_token": "eyJ0b2tlbiI6ImV4YW1wbGUifQ==",
  "has_more": true
}
```

### Response Fields

| Field | Type | Description |
|-------|------|-------------|
| `documents` | array | List of document objects |
| `documents[].filename` | string | Original filename |
| `documents[].uploaded_at` | string | ISO 8601 timestamp of upload |
| `documents[].size_bytes` | integer | File size in bytes |
| `documents[].s3_key` | string | S3 object key |
| `count` | integer | Number of documents in current response |
| `next_token` | string | Token for next page (omitted if no more pages) |
| `has_more` | boolean | Indicates if more documents are available |

### Error Responses

**400 Bad Request**

```json
{
  "error": "Invalid limit parameter",
  "message": "Limit must be between 1 and 100"
}
```

**500 Internal Server Error**

```json
{
  "error": "Internal server error",
  "message": "An unexpected error occurred"
}
```

## Environment Variables

| Variable | Required | Description | Example |
|---------|----------|-------------|--------|
| `BUCKET_NAME` | Yes | S3 bucket name containing documents | `my-document-bucket` |
| `LOG_LEVEL` | No | Logging level (DEBUG, INFO, WARNING, ERROR) | `IOFO` |
| `DEFAULT_LIMIT` | No | Default number of documents to return | `10` |
| `MAX_LIMIT` | No | Maximum allowed limit value | `100` |

## IAM Permissions

The Lambda function requires the following IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "s3:ListBucket",
        "s3:GetObject",
        "s3:GetObjectMetadata"
      ],
      "Resource": [
        "arn:aws:s3:::${BUCKET_NAME}",
        "arn:aws:s3:::${BUCKET_NAME}/*"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:*:*:*"
    }
  ]
}
```

### Required Permissions Breakdown

- **s3:ListBucket**: List objects in the bucket
- **s3:GetObject**: Read object data (if needed for future enhancements)
- **s3:GetObjectMetadata**: Retrieve object metadata
- **logs:***: Write logs to CloudWatch

## Error Handling

### Error Types

1. **Validation Errors** (HTTP 400)
   - Invalid limit parameter (not a number, out of range)
   - Invalid next_token format

2. **S3 Errors** (HTTP 500)
   - Bucket not found
   - Access denied
   - S3 service unavailable

3. **Internal Errors** (HTTP 500)
   - Unexpected exceptions
   - Configuration errors

### Error Response Structure

All error responses follow this structure:

```json
{
  "error": "Error type or category",
  "message": "Human-readable error description"
}
```

### Error Logging

All errors are logged to CloudWatch with:
- Error type and message
- Request ID for tracing
- Stack trace for internal errors
- Relevant context (bucket name, parameters, etc.)

## Usage Examples

### Python

```python
import requests

def get_recent_documents(limit=10, next_token=None):
    url = "https://api.example.com/documents/recent"
    params = {"limit": limit}
    
    if next_token:
        params["next_token"] = next_token
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    
    return response.json()

# Get first page
result = get_recent_documents(limit=20)
print(f"Found {result['count']} documents")

# Get next page if available
if result.get("has_more"):
    next_page = get_recent_documents(
        limit=20,
        next_token=result["next_token"]
    )
    print(f"Next page: {next_page['count']} documents")
```

### JavaScript/TypeScript

```typescript
interface Document {
  filename: string;
  uploaded_at: string;
  size_bytes: number;
  s3_key: string;
}

interface DocumentsResponse {
  documents: Document[];
  count: number;
  next_token?: string;
  has_more: boolean;
}

async function getRecentDocuments(
  limit: number = 10,
  nextToken?: string
): Promise<DocumentsResponse> {
  const url = new URL("https://api.example.com/documents/recent");
  url.searchParams.append("limit", limit.toString());
  
  if (nextToken) {
    url.searchParams.append("next_token", nextToken);
  }
  
  const response = await fetch(url.toString());
  
  if (!response.ok) {
    const error = await response.json();
    throw new Error(`${error.error}: ${error.message}`);
  }
  
  return await response.json();
}

// Usage
(async () => {
  try {
    const result = await getRecentDocuments(20);
    console.log(`Found ${result.count} documents`);
    
    // Get next page if available
    if (result.has_more) {
      const nextPage = await getRecentDocuments(20, result.next_token);
      console.log(`Next page: ${nextPage.count} documents`);
    }
  } catch (error) {
    console.error("Error fetching documents:", error);
  }
})();
```

### cURL

```bash
##!/bin/bash

# Function to fetch documents
fetch_documents() {
  local limit=${1:-10}
  local next_token="$2"
  local url="https://api.example.com/documents/recent?limit=${limit}"
  
  if [ -n "${next_token}" ]; then
    url="${url}&next_token=${next_token}"
  fi
  
  curl -s -X GET "${url}" | jq .
}

# Get first 20 documents
fetch_documents 20

# Get next page (use token from previous response)
# fetch_documents 20 "eyJ0b2tlbiI6ImV4YW1wbGUifQ=="
```

## Performance Considerations

- **Cold Start**: First invocation may take 1-2s due to Lambda initialization
- **Warm Invocations**: Typically < 500ms for most queries
- **Large Buckets**: Performance degrades with buckets containing >10,000 objects
- **Pagination**: Use smaller limits (10-50) for better response times

## Monitoring

### CloudWatch Metrics

- **Invocations**: Total number of function invocations
- **Errors**: Number of failed invocations
- **Duration**: Execution time per invocation
- **Throttles**: Number of throttled invocations
- **Concurrent Executions**: Number of simultaneous executions

### Custom Metrics

The handler logs additional metrics:
- Number of documents returned
- S3 API call latency
- Pagination token usage
- Validation error frequency

### Alerts

Recommended CloudWatch alarms:

1. **High Error Rate**: Errors > 5% of invocations
2. **High Duration**: Duration > 5s
3. **Throttling**: Any throttled invocations
4. **No Invocations**: No invocations for > 24 hours (if expected traffic)

## Troubleshooting

### Common Issues

1. **Empty Response**
   - Cause: No documents in bucket
   - Solution: Verify bucket contains objects

2. **Access Denied**
   - Cause: Insufficient IAM permissions
   - Solution: Verify Lambda execution role has required S3 permissions

3. **Slow Response**
   - Cause: Large number of objects in bucket
   - Solution: Use smaller limit values, consider indexing solution

4. **Invalid Pagination Token**
   - Cause: Token expired or corrupted
   - Solution: Start from first page without token

### Debugging

Enable debug logging:

```bash
# Set environment variable
LOG_LEVEL=DEBUG
```

Debug logs include:
- Request parameters
- S3 API call details
- Pagination token details
- Response construction steps

## Security

### Best Practices

1. **Least Privilege**: Only grant necessary S3 permissions
2. **Encryption**: Ensure S3 bucket uses encryption at rest
3. **API Authentication**: Use API Gateway authorizers (Cognito, Lambda, etc.)
4. **Rate Limiting**: Configure API Gateway throttling
5. **Input Validation**: Validate all query parameters

### Data Privacy

- Does not expose file contents, only metadata
- Consider adding user-based filtering for multi-tenant scenarios
- Log sanitization to prevent sensitive data leakage

## Deployment

### Terraform

```hcl
module "list_recent_docs_lambda" {
  source = "../../modules/lambda"

  function_name = "list-recent-docs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  environment_variables = {
    BUCKET_NAME   = var.document_bucket_name
    LOG_LEVEL     = "INFO"
    DEFAULT_LIMIT = "10"
    MAX_LIMIT     = "100"
  }

  source_dir = "../../src/lambda/list-recent-docs"

  iam_policy_documents = [
    data.aws_iam_policy_document.s3_read_access.json
  ]

  tags = {
    Environment = var.environment
    Project     = "rag-system"
  }
}

data "aws_iam_policy_document" "s3_read_access" {
  statement {
    effect = "Allow"
    actions = [
      "s3:ListBucket",
      "s3:GetObject",
      "s3:GetObjectMetadata"
    ]
    resources = [
      "var.document_bucket_arn",
      "${var.document_bucket_arn}/*"
    ]
  }
}
```

### Deployment Steps

1. Package Lambda function
2. Apply Terraform configuration
3. Configure API Gateway integration
4. Test endpoint
5. Set up monitoring and alerts

## Testing

### Unit Tests

```python
# Tests located in tests/unit/test_list_recent_docs.py
pytest tests/unit/test_list_recent_docs.py
```

### Integration Tests

```python
# Tests located in tests/integration/test_list_recent_docs.py
pytest tests/integration/test_list_recent_docs.py
```

### Manual Testing

```bash
# Test basic functionality
curl -X GET "https://api.example.com/documents/recent"

# Test pagination
curl -X GET "https://api.example.com/documents/recent?limit=5"

# Test error handling
curl -X GET "https://api.example.com/documents/recent?limit=200"
```

## Related Documentation

- [AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/)
- [Boto3 S3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html)
- [API Gateway Integration](https://docs.aws.amazon.com/apigateway/)
- [CloudWatch Logs](https://docs.aws.amazon.com/cloudwatch/)

## Support

For issues or questions:
- Check CloudWatch logs for error details
- Review IAM permissions
- Verify environment variable configuration
- Contact the development team

## Version History

- **v1.0.0** (Initial Release)
  - Basic document listing functionality
  - Pagination support
  - Error handling
  - CloudWatch logging
