# Lambda: Query KB

Orchestrates the full RAG pipeline for knowledge base queries.

## Purpose

The query-kb Lambda function is the single entry point for querying the OutcomeOps AI Assist knowledge base. It orchestrates the complete RAG (Retrieval Augmented Generation) pipeline by coordinating vector search and Claude answer generation.

## Trigger

- **Invoked by**:
  - MS Teams bot
  - CLI tools (outcome-ops-assist command)
  - Slack integrations
  - Direct Lambda invocation
  - Any user-facing interface
- **Invocation type**: Synchronous (RequestResponse)
- **Typical latency**: 7-20 seconds

## How It Works

```
User Query
    ↓
[query-kb Lambda]
    ↓
1. Invoke vector-query Lambda
    → Find top K relevant documents
    ↓
2. Check if results found
    → If empty: Return "not found" message
    → If found: Continue to step 3
    ↓
3. Invoke ask-claude Lambda
    → Generate answer from context
    ↓
4. Return answer + sources
```

## Input

```json
{
  "query": "How should API Gateway routes be defined?",
  "topK": 5
}
```

**Parameters**:
- `query` (required): Natural language question
- `topK` (optional): Number of documents to retrieve (default: 5)

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

**No results found**:
```json
{
  "statusCode": 404,
  "body": {
    "answer": "I couldn't find any relevant information in the knowledge base to answer this question. This could mean:\n\n1. The topic hasn't been documented yet\n2. The relevant documentation hasn't been ingested\n3. The query uses different terminology than the documentation\n\nTry rephrasing your question or check if the relevant ADRs and documentation have been added to the knowledge base.",
    "sources": []
  }
}
```

## Configuration

**SSM Parameters** (loaded at container startup):
- `/{env}/{app_name}/lambda/vector-query-arn` - Vector query Lambda ARN
- `/{env}/{app_name}/lambda/ask-claude-arn` - Ask Claude Lambda ARN

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)

## IAM Permissions Required

- `ssm:GetParameter` - Load Lambda ARNs from SSM
- `kms:Decrypt` - Decrypt SSM parameters
- `lambda:InvokeFunction` - Invoke vector-query and ask-claude Lambdas

## Pipeline Stages

### Stage 1: Vector Search

**Invokes**: vector-query Lambda

**Input**:
```json
{
  "query": "How should API Gateway routes be defined?",
  "topK": 5
}
```

**Processing**:
- Generates query embedding
- Searches DynamoDB for similar documents
- Returns top K results ranked by cosine similarity

**Output**:
```json
[
  {
    "score": 0.93,
    "text": "...",
    "source": "ADR: api-gateway-standards"
  }
]
```

### Stage 2: Answer Generation

**Invokes**: ask-claude Lambda

**Input**:
```json
{
  "query": "How should API Gateway routes be defined?",
  "context": [
    { "score": 0.93, "text": "...", "source": "ADR: api-gateway-standards" }
  ]
}
```

**Processing**:
- Builds RAG prompt with context
- Calls Claude 3.5 Sonnet
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
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-query-kb --follow
```

**Key Log Messages**:
- `[query-kb] Processing query: '...' (top X results)` - Started
- `[query-kb] Invoking vector-query Lambda...` - Stage 1 start
- `[query-kb] Found X relevant documents` - Stage 1 complete
- `[query-kb] No relevant documents found` - Empty results
- `[query-kb] Invoking ask-claude Lambda...` - Stage 2 start
- `[query-kb] Successfully generated answer with X sources` - Complete

**End-to-end metrics**:
```bash
aws cloudwatch get-metric-statistics \
  --namespace AWS/Lambda \
  --metric-name Duration \
  --dimensions Name=FunctionName,Value=dev-outcome-ops-ai-assist-query-kb \
  --start-time 2025-01-01T00:00:00Z \
  --end-time 2025-01-15T00:00:00Z \
  --period 3600 \
  --statistics Average,Maximum
```

## Performance

**Typical latency breakdown**:
- Vector search: 2-5 seconds
- Answer generation: 5-15 seconds
- **Total**: 7-20 seconds

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
  --function-name dev-outcome-ops-ai-assist-query-kb \
  --payload '{"query": "How should Lambda error handling work?", "topK": 3}' \
  response.json

cat response.json | jq '.body | fromjson'
```

**Expected response**:
- Status code: 200 or 404
- Body contains: answer (string) and sources (array)
- Execution time: 7-20 seconds

**Integration test**:
```bash
# Test full pipeline
./test-query-kb.sh "How should Lambda handlers be structured?"

# Expected output:
# ✓ Vector search: 3 documents found
# ✓ Answer generated: 450 chars
# ✓ Sources cited: 2
# ✓ Total time: 12.3s
```

## Usage Examples

### From CLI

```bash
# outcome-ops-assist command (future implementation)
outcome-ops-assist "How should I handle errors in Lambda functions?"

# Output:
# 🤖 Querying knowledge base...
#
# 📚 Answer:
# Lambda handlers should follow the error handling pattern defined in ADR-004...
#
# 📖 Sources:
#   - ADR: lambda-handler-standards
#   - Code map - outcome-ops-ai-assist
```

### From MS Teams Bot

```
User: @OutcomeOpsBot How should API Gateway routes be defined?

Bot: API routes should be defined using Terraform with inline integrations
in infra/apigw.tf, in accordance with ADR-APIGW-V5 v2.1. This approach
centralizes route definitions and ensures consistency across deployments.

Sources:
• ADR: api-gateway-standards
• README.md - hpe-journey
```

### From Python

```python
import boto3
import json

lambda_client = boto3.client("lambda")

response = lambda_client.invoke(
    FunctionName="dev-outcome-ops-ai-assist-query-kb",
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
- Cause: vector-query Lambda error
- Fix: Check vector-query logs, verify DynamoDB table exists

**4. "Failed to generate answer"**
- Cause: ask-claude Lambda error
- Fix: Check ask-claude logs, verify Bedrock permissions

## Related

- **Handler code**: `lambda/query-kb/handler.py`
- **Dependencies**:
  - `docs/lambda-vector-query.md` (vector search)
  - `docs/lambda-ask-claude.md` (answer generation)
- **Terraform**: `terraform/lambda.tf` (query_kb_lambda module)
- **Consumers**: MS Teams bot, CLI tools, Slack integrations
