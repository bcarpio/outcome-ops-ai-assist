# Lambda Handler: list-recent-docs

## Overview

The `list-recent-docs` Lambda handler retrieves a list of recently created or modified documents from the Outcome Ops system. It queries the DynamoDB `documents` table using a Global Secondary Index (GSI) optimized for time-based queries and returns document metadata including titles, authors, timestamps, and status.

### Key Features

- **Time-Based Querying**: Queries documents using the `status-timestamp-index` GSI
- **Pagination Support**: Supports paginated results with customizable limits
- **Status Filtering**: Filters documents by status (draft, published, archived)
- **Error Handling**: Comprehensive error handling with detailed error messages
- **Logging**: Structured logging for observability and debugging

## Architecture

### Data Flow

```
Client (outcome-ops-assist)
    ↓
API Gateway
    ↓
Lambda Handler (list-recent-docs)
    ↓
DynamoDB (documents table)
    ↓
GSI: status-timestamp-index
    ↓
Response with document list
```

### DynamoDB Schema

**Table**: `documents`

**Primary Key**:
- Partition Key: `document_id` (String)

**Global Secondary Index**: `status-timestamp-index`
- Partition Key: `status` (String)
- Sort Key: `updated_at` (String, ISO 8601 format)
- Projection: ALL

**Attributes**:
- `document_id` (String): Unique document identifier
- `title` (String): Document title
- `author` (String): Document author/creator
- `status` (String): Document status (draft, published, archived)
- `created_at` (String): Creation timestamp (ISO 8601)
- `updated_at` (String): Last update timestamp (ISO 8601)
- `content` (String): Document content
- `tags` (List): Document tags

## Request Format

### HTTP Method

`GET`

### Query Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|--------------------------------------------------------------|
| `limit` | Integer | No | 20 | Number of documents to return (1-100) |
| `status` | String | No | "published" | Filter by document status (draft, published, archived) |
| `last_evaluated_key` | String | No | None | Pagination token from previous response (JSON string) |

### Example Requests

**Basic Request**:
```bash
GET /list-recent-docs
```

**With Limit**:
```bash
GET /list-recent-docs?limit=10
```

**With Status Filter**:
```bash
GET /list-recent-docs?status=draft
```

**With Pagination**:
```bash
GET /list-recent-docs?limit=10&last_evaluated_key=%7B%22document_id%22%3A%22doc-123%22%2C%22updated_at%22%3A%222024-01-15T10%3A30%3A00Z%22%7D
```

## Response Format

### Success Response (200 OK)

```json
{
  "documents": [
    {
      "document_id": "doc-123",
      "title": "Quarterly Business Review",
      "author": "john.doe@example.com",
      "status": "published",
      "created_at": "2024-01-10T09:00:00Z",
      "updated_at": "2024-01-15T10:30:00Z",
      "tags": ["business", "quarterly", "review"]
    },
    {
      "document_id": "doc-124",
      "title": "Product Roadmap 2024",
      "author": "jane.smith@example.com",
      "status": "published",
      "created_at": "2024-01-12T11:15:00Z",
      "updated_at": "2024-01-14T16:45:00Z",
      "tags": ["product", "roadmap"]
    }
  ],
  "last_evaluated_key": {
    "document_id": "doc-124",
    "status": "published",
    "updated_at": "2024-01-14T16:45:00Z"
  },
  "count": 2,
  "limit": 20
}
```

### Field Descriptions

| Field | Type | Description |
|-------|------|--------------------------------------------------------------------------|
| `documents` | Array | List of document objects |
| `documents[].document_id` | String | Unique document identifier |
| `documents[].title` | String | Document title |
| `documents[].author` | String | Document author/creator |
| `documents[].status` | String | Document status (draft, published, archived) |
| `documents[].created_at` | String | Creation timestamp (ISO 8601) |
| `documents[].updated_at` | String | Last update timestamp (ISO 8601) |
| `documents[].tags` | Array | List of document tags |
| `last_evaluated_key` | Object | Pagination token for next page (null if no more results) |
| `count` | Integer | Number of documents returned in this response |
| `limit` | Integer | Maximum number of documents requested |

### Error Responses

**400 Bad Request**:
```json
{
  "error": "Invalid limit parameter. Must be between 1 and 100."
}
```

**400 Bad Request (Invalid Status)**:
```json
{
  "error": "Invalid status parameter. Must be one of: draft, published, archived."
}
```

**400 Bad Request (Invalid Pagination Token)**:
```json
{
  "error": "Invalid last_evaluated_key format. Must be a valid JSON object."
}
```

**500 Internal Server Error**:
```json
{
  "error": "An error occurred while retrieving documents."
}
```

## Environment Variables

| Variable | Required | Description | Example Value |
|---------|----------|---------------------------------------------------------------------------|------------------------------------------------|
| `DOCUMENTSETABLEN_AME` | Yes | Name of the DynamoDB documents table | `outcome-ops-documents` |
| `LOG_LEVEL` | No | Logging level (INFO, DEBUG, WARN, ERROR) | `INFO` |
| `AWS_REGION` | Auto | AWS region (automatically set by Lambda) | `us-east-1` |

## IAM Permissions

The Lambda execution role requires the following IAM permissions:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": [
        "dynamodb:Query"
      ],
      "Resource": [
        "arn:aws:dynamodb:{region}:{account-id}:table/outcome-ops-documents/index/status-timestamp-index"
      ]
    },
    {
      "Effect": "Allow",
      "Action": [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ],
      "Resource": "arn:aws:logs:{region}:{account-id}:log-group:/aws/lambda/list-recent-docs:*"
    }
  ]
}
```

### Required Permissions

- **dynamodb:Query**: Query the `status-timestamp-index` GSI on the documents table
- **logs:CreateLogGroup**: Create CloudWatch Log Groups
- **logs:CreateLogStream**: Create CloudWatch Log Streams
- **logs:PutLogEvents**: Write logs to CloudWatch

## Error Handling

The handler implements comprehensive error handling for various scenarios:

### Validation Errors

- **Invalid Limit**: Returns 400 if limit is not between 1 and 100
- **Invalid Status**: Returns 400 if status is not one of: draft, published, archived
- **Invalid Pagination Token**: Returns 400 if last_evaluated_key is not valid JSON

### DynamoDB Errors

- **ResourceNotFoundException**: Table or index does not exist - returns 500
- **ProvisionedThroughputExceededException**: Throttling error - returns 500
- **InternalServerError**: DynamoDB internal error - returns 500

### General Errors

- **Unexpected Exceptions**: Any unhandled exception returns 500 with a generic error message

### Error Logging

All errors are logged to CloudWatch with detailed context:

- Error type and message
- Request parameters
- Stack trace (in DEBUG mode)
- Timestamp

## Usage Examples

### Using outcome-ops-assist CLI

#### 1. List Recent Published Documents (Default)

```bash
outcome-ops-assist list-recent-docs
```

**Output**:
```
Found 2 recent documents:

1. Quarterly Business Review
   ID: doc-123
   Author: john.doe@example.com
   Status: published
   Created: 2024-01-10T09:00:00Z
   Updated: 2024-01-15T10:30:00Z
   Tags: business, quarterly, review

2. Product Roadmap 2024
   ID: doc-124
   Author: jane.smith@example.com
   Status: published
   Created: 2024-01-12T11:15:00Z
   Updated: 2024-01-14T16:45:00Z
   Tags: product, roadmap
```

#### 2. List Recent Draft Documents

```bash
outcome-ops-assist list-recent-docs --status draft
```

**Output**:
```
Found 3 recent draft documents:

1. Work in Progress Document
   ID: doc-125
   Author: alice.johnson@example.com
   Status: draft
   Created: 2024-01-16T08:00:00Z
   Updated: 2024-01-16T14:20:00Z
   Tags: wip

...
```

#### 3. Limit Results to 10 Documents

```bash
outcome-ops-assist list-recent-docs --limit 10
```

**Output**:
```
Found 10 recent documents:

1. Document Title 1
   ID: doc-101
   ...

2. Document Title 2
   ID: doc-102
   ...

...

More results available. Use --next-page to continue.
```

#### 4. Paginate Through Results

```bash
# Get first page
outcome-ops-assist list-recent-docs --limit 10 --save-pagination

# Get next page
outcome-ops-assist list-recent-docs --limit 10 --next-page
```

#### 5. JSON Output for Scripting

```bash
outcome-ops-assist list-recent-docs --output json | jq '.documents[].title'
```

**Output**:
```
"Quarterly Business Review"
"Product Roadmap 2024"
```

### Using cURL

#### 1. Basic Request

```bash
curl -X GET "https://api.example.com/list-recent-docs" \
  -H "Content-Type: application/json"
```

#### 2. With Query Parameters

```bash
curl -X GET "https://api.example.com/list-recent-docs?limit=10&status=draft" \
  -H "Content-Type: application/json"
```

#### 3. With Pagination

```bash
# First request
curl -X GET "https://api.example.com/list-recent-docs?limit=10" \
  -H "Content-Type: application/json" \
  | jq '.last_evaluated_key' > pagination_token.json

# Subsequent request
curl -X GET "https://api.example.com/list-recent-docs?limit=10&last_evaluated_key=$(cat pagination_token.json | jq -c '.' | jq -sRr @uri)" \
  -H "Content-Type: application/json"
```

## Performance Considerations

### DynamoDB Query Optimization

- **GSI Usage**: The handler uses the `status-timestamp-index` GSI for efficient time-based queries
- **ScanIndexForward**: Set to `false` to retrieve most recent documents first
- **Projection**: Uses `ALL` projection to retrieve all attributes

### Pagination

- **Default Limit**: 20 documents per request
- **Maximum Limit**: 100 documents per request
- **Pagination Token**: Use `last_evaluated_key` from response to fetch next page

### Expected Latency

- **Typical Response Time**: 50-200ms (depends on number of documents)
- **Cold Start**: 500ms-2s (first invocation after idle period)
- **Warm Start**: 50-200ms

## Monitoring and Observability

### CloudWatch Metrics

The handler automatically publishes the following metrics:

- **Invocations**: Total number of invocations
- **Errors**: Number of failed invocations
- **Duration**: Execution time in milliseconds
- **Throttles**: Number of throttled invocations

### Custom Metrics

Consider adding custom metrics for:

- Number of documents returned
- Query execution time
- Pagination usage
- Status filter distribution

### Logging

All logs are written to CloudWatch Logs in structured JSON format:

```json
{
  "timestamp": "2024-01-16T10:30:00Z",
  "level": "INFO",
  "message": "Querying documents",
  "status": "published",
  "limit": 20,
  "has_pagination_token": false
}
```

### Alerts

Recommended CloudWatch alerts:

1. **High Error Rate**: Alert if error rate > 5% over 5 minutes
2. **High Latency**: Alert if p95 latency > 1s
3. **Throttling**: Alert if throttles > 0 over 5 minutes
4. **DynamoDB Errors**: Alert on ProvisionedThroughputExceededException

## Testing

### Unit Tests

The handler includes comprehensive unit tests covering:

- Successful query with default parameters
- Custom limit parameter
- Status filtering (draft, published, archived)
- Pagination with last_evaluated_key
- Empty results
- Invalid limit validation
- Invalid status validation
- Invalid pagination token validation
- DynamoDB error handling
- General exception handling

Run tests:
```bash
pytest tests/unit/test_list_recent_docs.py -v
```

### Integration Tests

Integration tests verify:

- End-to-end functionality with real DynamoDB table
- API Gateway integration
- IAM permissions
- Environment variable configuration

Run integration tests:
```bash
pytest tests/integration/test_list_recent_docs.py -v
```

### Load Testing

For load testing, use tools like:

- **Artillery**: for HTTP load testing
- **Locust**: for distributed load testing
- **AWS Lambda Power Tuning**: for optimizing memory and cost

## Troubleshooting

### Common Issues

1. **Table Not Found**
   - Verify `DOCUMENTS_TABLE_NAME` environment variable
   - Check that the DynamoDB table exists
   - Verify IAM permissions

2. **Index Not Found**
   - Verify that `status-timestamp-index` GSI exists
   - Check GSI configuration (partition key: `status`, sort key: `updated_at`)

3. **Throttling Errors**
   - Increase DynamoDB table capacity
   - Enable DynamoDB auto-scaling
   - Implement exponential backoff in client

4. **Empty Results**
   - Verify data exists in the table
   - Check status filter parameter
   - Verify `updated_at` timestamps are populated

5. **Invalid Pagination Token**
   - Ensure token is properly URL-encoded
   - Verify token is valid JSON
   - Check that token contains required fields (`document_id`, `status`, `updated_at`)

### Debugging

Enable debug logging:

```bash
# Set LOG_LEVEL environment variable to DEBUG
aws lambda update-function-configuration \
  --function-name list-recent-docs \
  --environment "Variables={LOG_LEVEL=DEBUG,DOCUMENTSETABLEE_NAME=outcome-ops-documents}"
```

View logs:
```bash
aws logs tail /aws/lambda/list-recent-docs --follow
```

## Related Documentation

- [DynamoDB Query Operations](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/Query.html)
- [DynamoDB Global Secondary Indexes](https://docs.aws.amazon.com/amazondynamodb/latest/developerguide/GSI.html)
- [AWS Lambda Best Practices](https://docs.aws.amazon.com/lambda/latest/dg/best-practices.html)
- [API Gateway Integration](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-lambda-integrations.html)
- [CloudWatch Logs Insights](https://docs.aws.amazon.com/AmazonCloudWatch/latest/logs/AnalyzingLogData.html)

## Version History

- **v1.0.0** (2024-01-16): Initial release
  - Basic query functionality
  - Status filtering
  - Pagination support
  - Error handling
  - Structured logging

## Support

For issues or questions:

- Create an issue in the GitHub repository
- Contact the Outcome Ops team
- Check the [FAQ](docs/faq.md)
