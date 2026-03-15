# Lambda: Query KB

Orchestrates the full RAG pipeline for knowledge base queries.

## Purpose

The query-kb Lambda function is the single entry point for querying the OutcomeOps AI Assist knowledge base. It orchestrates the complete RAG (Retrieval Augmented Generation) pipeline by performing vector search via S3 Vectors and coordinating Claude answer generation.

## Trigger

- **Invoked by**:
  - CLI tool (`scripts/outcome-ops-assist`)
  - Code generation Lambda (with `raw=true`)
  - Direct Lambda invocation
- **Invocation type**: Synchronous (RequestResponse)
- **Typical latency**: 7-20 seconds

## How It Works

```
User Query
    ↓
[query-kb Lambda]
    ↓
1. Generate query embedding (Bedrock Titan v2)
    ↓
2. Query S3 Vectors for similar documents (<100ms)
    → Over-fetch candidates (topK * 4, capped at RERANK_MAX_FETCH)
    ↓
3. Rerank results using Cohere Rerank 3.5 via Bedrock
    → Cross-attention scoring for precision ranking
    → Filter to top K most relevant documents
    → Falls back to cosine order if reranking fails
    ↓
4. Check if results found
    → If empty: Return "not found" message
    → If found: Continue to step 5
    ↓
5. Invoke ask-claude Lambda
    → Generate answer from reranked context
    ↓
6. Return answer + sources
```

## Input

```json
{
  "query": "How should API Gateway routes be defined?",
  "topK": 5,
  "repo": "spring-petclinic",
  "raw": false
}
```

**Parameters**:
- `query` (required): Natural language question
- `topK` (optional): Number of documents to retrieve (default: 5)
- `repo` (optional): Filter results to a specific repository (e.g., "spring-petclinic")
- `raw` (optional): If true, returns vector search results directly without Claude synthesis (default: false)

## Output

**Success response**:
```json
{
  "statusCode": 200,
  "body": {
    "answer": "API routes should be defined using Terraform with inline integrations in infra/apigw.tf, in accordance with ADR-APIGW-V5 v2.1. This approach centralizes route definitions and ensures consistency across deployments.",
    "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"]
  }
}
```

**Raw mode response** (when `raw=true`):
```json
{
  "statusCode": 200,
  "body": {
    "results": [
      {
        "score": 0.93,
        "text": "API Gateway routes are defined inline in infra/apigw.tf...",
        "source": "ADR: api-gateway-standards",
        "type": "adr",
        "repo": "outcome-ops-ai-assist",
        "file_path": "docs/adr/ADR-001.md"
      }
    ]
  }
}
```

Raw mode is used by code generation to avoid double-processing - the results will be passed to Claude for code generation anyway, so we skip the synthesis step.

**No results found**:
```json
{
  "statusCode": 404,
  "body": {
    "answer": "I couldn't find any relevant information in the knowledge base to answer this question. This could mean:\n\n1. The topic hasn't been documented yet\n2. The relevant documentation hasn't been ingested\n3. The query uses different terminology than the documentation\n\nTry rephrasing your question or check if the relevant ADRs and documentation have been added to the knowledge base.",
    "sources": [],
    "results": []
  }
}
```

## Configuration

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/s3vectors/bucket-name` - S3 Vectors bucket name
- `/{env}/{app_name}/s3vectors/index-name` - S3 Vectors index name
- `/{env}/{app_name}/lambda/ask-claude-arn` - Ask Claude Lambda ARN

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)
- `BEDROCK_RERANK_MODEL_ID` - Rerank model ID (default: `cohere.rerank-v3-5:0`)
- `RERANK_ENABLED` - Enable/disable reranking (`"true"` or `"false"`)
- `RERANK_MAX_FETCH` - Max documents to over-fetch for reranking (default: 90)
- `RERANK_MIN_SCORE` - Minimum rerank relevance score (0-1) to include in results (default: 0.15)

## IAM Permissions Required

- `ssm:GetParameter` - Load configuration from SSM
- `kms:Decrypt` - Decrypt SSM parameters
- `s3vectors:QueryVectors` - Search the vector index
- `bedrock:InvokeModel` - Generate query embeddings (Titan v2)
- `bedrock:Rerank` - Rerank search results (Cohere Rerank 3.5)
- `aws-marketplace:ViewSubscriptions`, `aws-marketplace:Subscribe` - Required for Cohere marketplace model access
- `lambda:InvokeFunction` - Invoke ask-claude Lambda

**Prerequisite:** The Cohere Rerank 3.5 marketplace agreement must be accepted in the AWS account before reranking will work. See [Deployment Guide - Bedrock Model Access](deployment-guide.md#bedrock-model-access-and-marketplace-agreements).

## Pipeline Stages

### Stage 1: Vector Search (S3 Vectors)

**Direct API call**: `s3vectors:QueryVectors`

**Processing**:
1. Generates query embedding using Bedrock Titan Embeddings v2 (1024 dimensions)
2. Queries S3 Vectors index with cosine similarity
3. Optionally filters by repo metadata
4. Returns top K results ranked by similarity

**S3 Vectors Query**:
```python
response = s3vectors_client.query_vectors(
    vectorBucketName=VECTOR_BUCKET,
    indexName=VECTOR_INDEX,
    queryVector={"float32": query_embedding},
    topK=5,
    returnMetadata=True,
    returnDistance=True,
    filter={"repo": {"equals": "spring-petclinic"}}  # optional
)
```

**Output**:
```json
[
  {
    "score": 0.93,
    "text": "...",
    "source": "ADR: api-gateway-standards",
    "type": "adr",
    "repo": "outcome-ops-ai-assist",
    "file_path": "docs/adr/ADR-001.md"
  }
]
```

### Stage 2: Reranking (Cohere Rerank 3.5)

**API**: `bedrock-agent-runtime:Rerank`

**Why**: Cosine similarity is good for recall (finding candidates) but not precision (ranking them). When documents from multiple sources (ADRs, Confluence, Jira, code maps) score similarly, cosine distance alone can't distinguish which document best answers the query. Reranking uses cross-attention scoring to determine true relevance.

**Processing**:
1. Takes over-fetched candidates from Stage 1 (up to `RERANK_MAX_FETCH` documents)
2. Sends query + all document texts to Cohere Rerank 3.5 via Bedrock
3. Receives cross-attention relevance scores for each document
4. Returns the top K documents ordered by rerank score

**Graceful degradation**: If the rerank API fails (throttling, model unavailable), falls back to the original cosine-ranked results. No user impact.

**Feature flag**: Set `RERANK_ENABLED=false` in the Lambda environment to disable without a code deploy. When disabled, the handler fetches the original `topK` (no over-fetch) and skips reranking entirely.

### Stage 3: Answer Generation

**Invokes**: ask-claude Lambda

**Input**:
```json
{
  "query": "How should API Gateway routes be defined?",
  "context": [
    { "score": 0.93, "rerank_score": 0.98, "text": "...", "source": "ADR: api-gateway-standards" }
  ]
}
```

**Processing**:
- Builds RAG prompt with reranked context (documents ordered by relevance)
- Claude is instructed to prioritize earlier documents as strongest matches
- Calls Claude 3.5 Haiku (cost-effective for RAG synthesis)
- Extracts answer and sources

**Output**:
```json
{
  "answer": "API routes should be defined...",
  "sources": ["ADR: api-gateway-standards"]
}
```

## Error Handling

**Missing query**:
```json
{
  "statusCode": 400,
  "body": {"error": "Missing required field: query"}
}
```

**Vector search failed**:
```json
{
  "statusCode": 500,
  "body": {"error": "Failed to search knowledge base"}
}
```

**Answer generation failed**:
```json
{
  "statusCode": 500,
  "body": {"error": "Failed to generate answer"}
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
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-query-kb --follow
```

**Key Log Messages**:
- `[query-kb] Processing query: '...' (top X results)` - Started
- `[query-kb] Querying S3 Vectors...` - Stage 1 start
- `[query-kb] Found X relevant documents` - Stage 1 complete
- `[query-kb] Reranked X documents in Yms` - Stage 2 complete
- `[query-kb] Reranked and filtered to top X` - Post-rerank filtering
- `[query-kb] No relevant documents found` - Empty results
- `[query-kb] Invoking ask-claude Lambda...` - Stage 3 start
- `[query-kb] Successfully generated answer with X sources` - Complete

**End-to-end metrics**:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value=prd-outcome-ops-ai-assist-query-kb \
  --start-time 2025-01-01T00:00:00Z \
  --end-time 2025-01-15T00:00:00Z \
  --period 3600 \
  --statistics Average,Maximum
```

## Performance

**Typical latency breakdown**:
- Embedding generation: 100-200ms
- S3 Vectors search: <100ms
- Reranking: 100-300ms
- Answer generation: 5-15 seconds
- **Total**: 5-16 seconds

**Timeout**: 10 minutes (600 seconds) - conservative for reliability

**Optimization opportunities**:
- Cache frequent queries (future)
- Parallel Lambda invocations if independent (not applicable here)
- Reduce topK for faster vector search
- Use Claude Haiku for faster responses (lower quality)

## Testing

**Manual test**:
```bash
aws lambda invoke \
  --function-name prd-outcome-ops-ai-assist-query-kb \
  --payload '{"query": "How should Lambda error handling work?", "topK": 3}' \
  response.json

cat response.json | jq '.body | fromjson'
```

**Expected response**:
- Status code: 200 or 404
- Body contains: answer (string) and sources (array)
- Execution time: 7-20 seconds

**Using the CLI**:
```bash
scripts/outcome-ops-assist "How should Lambda handlers be structured?"
```

## Usage Examples

### From CLI

```bash
scripts/outcome-ops-assist "How should I handle errors in Lambda functions?"
```

### From Python

```python
import boto3
import json

lambda_client = boto3.client("lambda")

response = lambda_client.invoke(
    FunctionName="prd-outcome-ops-ai-assist-query-kb",
    InvocationType="RequestResponse",
    Payload=json.dumps({
        "query": "How should Lambda error handling work?",
        "topK": 5
    })
)

result = json.loads(response["Payload"].read())
body = json.loads(result["body"])

print(f"Answer: {body['answer']}")
print(f"Sources: {', '.join(body['sources'])}")
```

## Common Issues

**1. "No relevant documents found"**
- Cause: Knowledge base empty or query too specific
- Fix: Run ingest-docs Lambda, rephrase query

**2. Timeout after 10 minutes**
- Cause: Vector search or Claude API taking too long
- Fix: Check CloudWatch logs, reduce topK, verify Bedrock availability

**3. "Failed to search knowledge base"**
- Cause: S3 Vectors query error
- Fix: Check CloudWatch logs, verify S3 Vectors bucket/index exist, check IAM permissions

**4. "Failed to generate answer"**
- Cause: ask-claude Lambda error
- Fix: Check ask-claude logs, verify Bedrock permissions

**5. Reranking fails silently (results returned in cosine order, not reranked)**
- Cause: Cohere Rerank 3.5 marketplace agreement not accepted
- Symptom: CloudWatch logs show `Reranking failed, falling back to cosine order: ValidationException: Model access is denied due to IAM user or service role is not authorized to perform the required AWS Marketplace actions`
- Fix: Accept the marketplace agreement via CLI or Bedrock Console. See [Deployment Guide - Bedrock Model Access](deployment-guide.md#bedrock-model-access-and-marketplace-agreements)

## Related

- **Handler code**: `lambda/query-kb/handler.py`
- **Dependencies**:
  - `docs/lambda-ask-claude.md` (answer generation)
  - S3 Vectors (`{env}-outcome-ops-ai-assist-vectors` bucket)
- **Terraform**: `terraform/lambda-query.tf` (query_kb_lambda module)
- **Consumers**: CLI (`scripts/outcome-ops-assist`), code generation Lambda

<!-- Confluence sync -->
