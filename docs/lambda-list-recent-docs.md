# Lambda Handler: List Recent Documents

## Overview

This Lambda function retrieves a list of recently uploaded documents from an S3 bucket, sorted by upload date in descending order. It supports pagination and filtering by file type.

**Purpose:**
- Provide a list of recently uploaded documents
- Support pagination for large datasets
- Allow filtering by file type (e.g., PDF, DOCX, TXT)
- Return metadata including file size, upload date, and content type

**API Endpoint:** `GET /recent-documents`

## Request Format

### HTTP Method
`GET`

### Query Parameters

| Parameter | Type | Required | Default | Description |
|----------|------|---------|--------|-----------|
| `limit` | integer | No | 10 | Number of documents to return (150) |
| `offset` | integer | No | 0 | Number of documents to skip for pagination |
| `fileType` | string | No | - | Filter by file extension (e.g., "pdf", "docx", "txt") |

### Example Requests

```http
GET /recent-documents?limit=20&offset=0

GET /recent-documents?limit=10&fileType=pdf

GET /recent-documents?limit=5&offset=10&fileType=docx
```

## Response Format

### Success Response (200 OK)

```json
{
  "statusCode": 200,
  "body": {
    "documents": [
      {
        "key": "documents/2024/01/report.pdf",
        "fileName": "report.pdf",
        "size": 1048576,
        "lastModified": "2024-01-15T10:30:00Z",
        "contentType": "application/pdf",
        "etag": "\"abc123def456\""
      },
      {
        "key": "documents/2024/01/notes.txt",
        "fileName": "notes.txt",
        "size": 2048,
        "lastModified": "2024-01-14T15:45:00Z",
        "contentType": "text/plain",
        "etag": "\"def456ghi789\""
      }
    ],
    "pagination": {
      "limit": 10,
      "offset": 0,
      "total": 2,
      "hasMore": false
    },
    "filters": {
      "fileType": null
    }
  }
}
```

### Error Responses

#### 400 Bad Request

```json
{
  "statusCode": 400,
  "body": {
    "error": "Invalid request",
    "message": "limit must be between 1 and 50",
    "details": {
      "parameter": "limit",
      "provided": 100,
      "allowedRange": "1-50"
    }
  }
}
```

#### 500 Internal Server Error

```json
{
  "statusCode": 500,
  "body": {
    "error": "Internal server error",
    "message": "Failed to retrieve documents from S3",
    "requestId": "abc123-def456-ghi789"
  }
}
```

#### 503 Service Unavailable

```json
{
  "statusCode": 503,
  "body": {
    "error": "Service unavailable",
    "message": "S3 service is temporarily unavailable",
    "requestId": "abc123-def456-ghi789"
  }
}
```

## Environment Variables

| Variable | Required | Description | Example |
|----------|----------|-----------|--------|
| `DOCUMENT_BUCKET` | Yes | S3 bucket name containing documents | `my-document-bucket` |
| `MAX_LIMIT` | No | Maximum number of documents per request | `50` (default) |
| `DEFAULT_LIMIT` | No | Default number of documents if not specified | `10` (default) |
| `LOG_LEVEL` | No | Logging level (DEBUG, INFO, WARN, ERROR) | `INFO` (default) |
| `AWS_REGION` | Yes (auto-set) | AWS region for S3 client | `us-east-1` |

## IAM Permissions

### Required Permissions

The Lambda execution role must have the following permissions:

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
        "arn:aws:s3:::DOCUMENT_BUCKET",
        "arn:aws:s3:::DOCUMENT_BUCKET/*"
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

### Permission Breakdown

- **s3:ListBucket**: Required to list objects in the bucket
- **s3:GetObject**: Required to read object metadata
- **s3:GetObjectMetadata**: Required to retrieve detailed metadata
- **logs:***: Standard CloudWatch Logs permissions for Lambda

## Error Scenarios

### 1. Invalid Query Parameters

**Scenario:** User provides invalid `limit` or `offset` values

**Response:**
- Status Code: 400
- Error Message: "Invalid request: limit must be between 1 and 50"
**Handling:** Validate query parameters before processing

### 2. S3 Bucket Not Found

**Scenario:** The specified S3 bucket does not exist

**Response:**
- Status Code: 500
- Error Message: "Failed to retrieve documents: bucket not found"
**Handling:** Catch `NoSuchBucket` exception and return appropriate error

### 3. Insufficient Permissions

**Scenario:** Lambda role lacks necessary S3 permissions

**Response:**
- Status Code: 500
- Error Message: "Failed to retrieve documents: access denied"
**Handling:** Catch `AccessDenied` exception and log details for debugging

### 4. S3 Service Unavailable

**Scenario:** S3 service is temporarily unavailable

**Response:**
- Status Code: 503
- Error Message: "S3 service is temporarily unavailable"
**Handling:** Implement retry logic with exponential backoff

### 5. Empty Bucket

**Scenario:** No documents found in the bucket

**Response:**
- Status Code: 200
- Body: Empty `documents` array, `total: 0`
**Handling:** Return successful response with empty results

### 6. Timeout

**Scenario:** Lambda execution timeout (large bucket)

**Response:**
- Status Code: 504
- Error Message: "Request timed out"
**Handling:** Implement pagination and optimize S3 list operations

## Usage Examples

### Example 1: Basic Request

```bash
curl -X GET "https://api.example.com/recent-documents"
```

```json
{
  "documents": [
    {
      "key": "documents/2024/01/report.pdf",
      "fileName": "report.pdf",
      "size": 1048576,
      "lastModified": "2024-01-15T10:30:00Z",
      "contentType": "application/pdf"
    }
  ],
  "pagination": {
    "limit": 10,
    "offset": 0,
    "total": 1,
    "hasMore": false
  }
}
```

### Example 2: Paginated Request

```bash
curl -X GET "https://api.example.com/recent-documents?limit=20&offset=20"
```

```json
{
  "documents": [
    // Documents 21-40
  ],
  "pagination": {
    "limit": 20,
    "offset": 20,
    "total": 100,
    "hasMore": true
  }
}
```

### Example 3: Filtered Request

```bash
curl -X GET "https://api.example.com/recent-documents?fileType=pdf&limit=5"
```

```json
{
  "documents": [
    {
      "key": "documents/2024/01/report.pdf",
      "fileName": "report.pdf",
      "size": 1048576,
      "lastModified": "2024-01-15T10:30:00Z",
      "contentType": "application/pdf"
    }
  ],
  "pagination": {
    "limit": 5,
    "offset": 0,
    "total": 1,
    "hasMore": false
  },
  "filters": {
    "fileType": "pdf"
  }
}
```

### Example 4: Python Client

```python
import requests

def get_recent_documents(limit=10, offset=0, file_type=None):
    """
    Retrieve recent documents from the API.
    
    Args:
        limit: Number of documents to retrieve
        offset: Number of documents to skip
        file_type: Optional file type filter
    
    Returns:
        dict: API response containing documents and pagination info
    """
    url = "https://api.example.com/recent-documents"
    
    params = {
        "limit": limit,
        "offset": offset
    }
    
    if file_type:
        params["fileType"] = file_type
    
    response = requests.get(url, params=params)
    response.raise_for_status()
    
    return response.json()

# Usage
try:
    result = get_recent_documents(limit=20, file_type="pdf")
    print(f"Found {len(result['documents'])} documents")
    
    for doc in result["documents"]:
        print(f"  - {doc['fileName']} ({doc['size']} bytes)")
except requests.exceptions.HTTPError as e:
    print(f"Error: {e}")
```

### Example 5: JavaScript/Node.js Client

```javascript
const axios = require('axios');

async function getRecentDocuments(limit = 10, offset = 0, fileType = null) {
  try {
    const params = { limit, offset };
    
    if (fileType) {
      params.fileType = fileType;
    }
    
    const response = await axios.get(
      'https://api.example.com/recent-documents',
      { params }
    );
    
    return response.data;
  } catch (error) {
    console.error('Error fetching documents:', error.message);
    throw error;
  }
}

// Usage
(async () => {
  const result = await getRecentDocuments(20, 0, 'pdf');
  console.log(`Found ${result.documents.length} documents`);
  
  result.documents.forEach(doc => {
    console.log(`  - ${doc.fileName} (${doc.size} bytes)`);
  });
})();
```

## Performance Considerations

1. **Pagination**: Use appropriate `limit` values to avoid timeouts
2. **Caching**: Consider caching results for frequently accessed data
3. **Filtering**: Use `fileType` filter to reduce response size
4. **Indexing**: For large buckets, consider using S3 Inventory or a database index

## Monitoring and Logging

### CloudWatch Metrics

- **Invocations**: Total number of Lambda invocations
- **Errors**: Number of failed invocations
- **Duration**: Execution time per invocation
- **Throttles**: Number of throttled requests

### Custom Metrics

- `DocumentsRetrieved`: Number of documents returned
- `S3ListDuration`: Time taken to list S3 objects
- `FilterApplied`: Number of requests with filters

### Log Format

```json
{
  "timestamp": "2024-01-15T10:30:00Z",
  "level": "INFO",
  "message": "Retrieved documents",
  "requestId": "abc123-def456-ghi789",
  "details": {
    "limit": 10,
    "offset": 0,
    "fileType": null,
    "documentsCount": 5,
    "durationMs": 123
  }
}
```

## Testing

### Unit Tests

Run unit tests:

```bash
pytest tests/unit/test_list_recent_docs.py
```

### Integration Tests

Run integration tests:

```bash
pytest tests/integration/test_list_recent_docs_integration.py
```

### Local Testing

Test locally using SAM CLI:

```bash
sam local invoke ListRecentDocsFunction \
  --event tests/events/list_recent_docs_event.json ## Deployment

### Using Terraform

```hcl
module "list_recent_docs_lambda" {
  source = "../../modules/lambda"

  function_name = "list-recent-documents"
  handler       = "lambda_function.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 256

  environment_variables = {
    DOCUMENT_BUCKET = var.document_bucket_name
    MAX_LIMIT        = "50"
    DEFAULT_LIMIT    = "10"
    LOG_LEVEL        = "INFO"
  }

  iam_policy_statements = [
    {
      effect = "Allow"
      actions = [
        "s3:ListBucket",
        "s3:GetObject",
        "s3:GetObjectMetadata"
      ]
      resources = [
        var.document_bucket_arn,
        "${var.document_bucket_arn}/*"
      ]
    }
  ]

  tags = var.tags
}
```

## Troubleshooting

### Common Issues

1. **No documents returned**:
   - Verify bucket name is correct
   - Check if bucket contains objects
   - Verify IAM permissions

2. **Access denied error**:
   - Verify Lambda execution role has required permissions
   - Check S3 bucket policy
   - Verify bucket exists in the same region

3. **Timeout errors**:
   - Reduce `limit` parameter
   - Optimize S3 list operations
   - Increase Lambda timeout

## Related Documentation

- [AWS Lambda Developer Guide](https://docs.aws.amazon.com/lambda/)
- [AWS S3 API Reference](https://docs.aws.amazon.com/AmazonS3/latest/API/)
- [Boto3 S3 Documentation](https://boto3.amazonaws.com/v1/documentation/api/latest/reference/services/s3.html)
- [Terraform AWS Provider](https://registry.terraform.io/providers/hashicorp/aws/latest/docs)

## Support

For issues or questions, please:
- Check the troubleshooting section above
- Review CloudWatch logs for detailed error messages
- Contact the development team
