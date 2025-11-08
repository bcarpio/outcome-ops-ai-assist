# Lambda: Vector Query

Performs semantic search over the knowledge base using embedding similarity.

## Purpose

The vector-query Lambda function performs vector similarity search to find the most relevant documents for a given natural language query. It's a core component of the RAG (Retrieval Augmented Generation) pipeline.

## Trigger

- **Invoked by**: query-kb orchestrator Lambda
- **Invocation type**: Synchronous (RequestResponse)
- **Typical latency**: 2-5 seconds

## How It Works

1. **Receive query**: Accepts natural language question and optional topK parameter
2. **Generate embedding**: Uses Bedrock Titan Embeddings v2 to create 1024-dimensional vector
3. **Scan documents**: Retrieves all documents with embeddings from DynamoDB
4. **Calculate similarity**: Computes cosine similarity between query and each document
5. **Rank results**: Returns top K most similar documents with scores

## Input

```json
{
  "query": "How should API Gateway routes be defined?",
  "topK": 5
}
```

**Parameters**:
- `query` (required): Natural language question
- `topK` (optional): Number of results to return (default: 5)

## Output

```json
[
  {
    "score": 0.93,
    "text": "API Gateway routes are defined inline in infra/apigw.tf...",
    "source": "ADR: api-gateway-standards",
    "type": "adr",
    "repo": "outcome-ops-ai-assist",
    "file_path": "docs/adr/ADR-001.md"
  },
  {
    "score": 0.89,
    "text": "All routes must use inline integrations...",
    "source": "README.md - hpe-journey",
    "type": "readme",
    "repo": "hpe-journey",
    "file_path": "README.md"
  }
]
```

**Fields**:
- `score`: Cosine similarity (0-1, higher is more similar)
- `text`: First 1000 characters of document content
- `source`: Formatted source for citation (e.g., "ADR: ADR-001")
- `type`: Document type (adr, readme, doc, code-map, etc.)
- `repo`: Source repository name
- `file_path`: Original file path in repository

## Configuration

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/dynamodb/code-maps-table` - DynamoDB table name

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)

## IAM Permissions Required

- `dynamodb:Scan`, `dynamodb:Query`, `dynamodb:GetItem` - Read documents from DynamoDB
- `ssm:GetParameter` - Load configuration
- `kms:Decrypt` - Decrypt SSM parameters
- `bedrock:InvokeModel` - Generate query embeddings with Titan v2

## Algorithm Details

### Cosine Similarity

The vector search uses cosine similarity to measure document relevance:

```
similarity = (A · B) / (||A|| × ||B||)
```

Where:
- A = query embedding vector
- B = document embedding vector
- · = dot product
- || || = magnitude (L2 norm)

**Score interpretation**:
- 0.9-1.0: Highly relevant
- 0.7-0.9: Relevant
- 0.5-0.7: Somewhat relevant
- <0.5: Not very relevant

### Performance

**Current implementation**:
- Full table scan with pagination
- O(n) time complexity where n = number of documents
- Typical scan time: 500ms-2s for 100-500 documents

**Future optimizations**:
- DynamoDB index on embedding type
- Approximate nearest neighbor (ANN) search
- Dedicated vector database (Pinecone, Weaviate)

## Error Handling

**Missing query**:
```json
{
  "statusCode": 400,
  "body": {"error": "Missing required field: query"}
}
```

**No documents in knowledge base**:
```json
{
  "statusCode": 200,
  "body": []
}
```

**Bedrock API failure**:
```json
{
  "statusCode": 500,
  "body": {"error": "Failed to generate query embedding"}
}
```

**Unexpected error**:
```json
{
  "statusCode": 500,
  "body": {"error": "Internal server error"}
}
```

## Monitoring

**CloudWatch Logs**:
```bash
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-vector-query --follow
```

**Key Log Messages**:
- `[vector-query] Generated query embedding with 1024 dimensions` - Embedding created
- `[vector-query] Scanned X documents from DynamoDB` - Documents retrieved
- `[vector-query] Found X results, returning top Y` - Search complete
- `[vector-query] No documents found in knowledge base` - Empty table

**CloudWatch Metrics**:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Invocations \
  --dimensions Name=FunctionName,Value=dev-outcome-ops-ai-assist-vector-query \
  --start-time 2025-01-01T00:00:00Z \
  --end-time 2025-01-15T00:00:00Z \
  --period 3600 \
  --statistics Sum
```

## Testing

**Manual test**:
```bash
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-vector-query \
  --payload '{"query": "How should Lambda error handling work?", "topK": 3}' \
  response.json

cat response.json | jq '.'
```

**Expected response**:
- Status code: 200
- Body: Array of 0-3 documents with scores
- Execution time: 2-5 seconds

## Common Issues

**1. Empty results**
- Cause: No documents ingested or query too specific
- Fix: Run ingest-docs Lambda, try broader query

**2. Low relevance scores (all <0.5)**
- Cause: Query uses different terminology than documentation
- Fix: Rephrase query, check documentation terminology

**3. Timeout after 5 minutes**
- Cause: Too many documents in DynamoDB (>10,000)
- Fix: Increase timeout, implement pagination, use vector database

## Related

- **Handler code**: `lambda/vector-query/handler.py`
- **Consumer**: `docs/lambda-query-kb.md` (orchestrator)
- **Terraform**: `terraform/lambda.tf` (vector_query_lambda module)
- **DynamoDB table**: `terraform/dynamodb.tf`
