# Lambda: Ask Claude

Generates natural language answers using Claude 3.5 Haiku via Bedrock Converse API.

## Purpose

The ask-claude Lambda function implements Retrieval Augmented Generation (RAG) by taking context from vector search and generating grounded, factual responses using Claude 3.5 Haiku. It ensures answers cite sources and stay within the provided context. Haiku is used for cost-effective RAG synthesis since the heavy lifting is done by the vector search.

## Trigger

- **Invoked by**:
  - query-kb orchestrator Lambda (when `raw=false`, the default)
  - generate-code Lambda (for standards summarization after vector search)
- **Invocation type**: Synchronous (RequestResponse)
- **Typical latency**: 5-15 seconds

## How It Works

1. **Receive query + context**: Accepts natural language question and relevant document chunks
2. **Build RAG prompt**: Constructs prompt with context documents and instructions
3. **Invoke Claude**: Calls Claude 3.5 Haiku via Bedrock Converse API
4. **Extract answer**: Parses response and extracts generated text
5. **Return with sources**: Returns answer with cited sources from context

## Input

```json
{
  "query": "How should API Gateway routes be defined?",
  "context": [
    {
      "score": 0.93,
      "text": "API Gateway routes are defined inline in infra/apigw.tf...",
      "source": "ADR: api-gateway-standards"
    },
    {
      "score": 0.89,
      "text": "All routes must use inline integrations per ADR-APIGW-V5...",
      "source": "README.md - hpe-journey"
    }
  ]
}
```

**Parameters**:
- `query` (required): Natural language question
- `context` (required): Array of relevant document chunks from vector search

## Output

```json
{
  "answer": "API routes in hpe-journey are defined using Terraform with inline integrations in infra/apigw.tf, in accordance with ADR-APIGW-V5 v2.1. This approach centralizes route definitions and ensures consistency across deployments.",
  "sources": ["ADR: api-gateway-standards", "README.md - hpe-journey"]
}
```

**Fields**:
- `answer`: Natural language response grounded in provided context
- `sources`: List of unique sources cited (extracted from context)

## Configuration

**Environment Variables** (set in Terraform):
- `ENV` - Environment name (dev, prd)
- `APP_NAME` - Application name (outcome-ops-ai-assist)

**Claude Model**:
- Model ID: `us.anthropic.claude-3-5-haiku-20241022-v1:0`
- Using cross-region inference profile for better availability

## IAM Permissions Required

- `bedrock:InvokeModel` - Call Claude 3.5 Haiku via Bedrock
  - Foundation model ARN
  - Cross-region inference profile ARN

## Prompt Engineering

### RAG Prompt Structure

```
You are a helpful assistant that answers questions about software development
patterns and architectural decisions based ONLY on the provided context.

CONTEXT:
[Document 1] (Relevance: 0.93) - Source: ADR: api-gateway-standards
API Gateway routes are defined inline in infra/apigw.tf...

[Document 2] (Relevance: 0.89) - Source: README.md - hpe-journey
All routes must use inline integrations per ADR-APIGW-V5...

INSTRUCTIONS:
1. Answer the question using ONLY information from the provided context
2. Cite the specific sources you use (e.g., "According to ADR-001...")
3. If the context doesn't contain enough information, say so clearly
4. Be concise but thorough
5. Do not make assumptions or add information not in the context

QUESTION: How should API Gateway routes be defined?

ANSWER:
```

### Inference Configuration

- **Temperature**: 0.3 (low for factual, deterministic responses)
- **Max tokens**: 2000 (prevents overly long responses)
- **Model**: Claude 3.5 Haiku (cost-effective for RAG synthesis)

## Retry Logic

The Lambda implements exponential backoff retry for transient errors:

**Retry on**:
- `ThrottlingException` - Bedrock rate limiting
- `ServiceUnavailableException` - Temporary service issues
- `InternalServerException` - Internal AWS errors

**No retry on**:
- `ValidationException` - Invalid request
- `AccessDeniedException` - Permission issues

**Backoff schedule**:
- Attempt 1: Immediate
- Attempt 2: 1 second wait
- Attempt 3: 2 seconds wait
- Attempt 4: 4 seconds wait (if max_retries=4)

## Error Handling

**Missing query**:
```json
{
  "statusCode": 400,
  "body": {"error": "Missing required field: query"}
}
```

**Empty context**:
```json
{
  "statusCode": 200,
  "body": {
    "answer": "I don't have enough information in the knowledge base to answer this question...",
    "sources": []
  }
}
```

**Bedrock API failure**:
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
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-ask-claude --follow
```

**Key Log Messages**:
- `[ask-claude] Generating answer for query: '...' with X context documents` - Started
- `[ask-claude] Successfully invoked Claude on attempt X` - API call succeeded
- `[ask-claude] Token usage - Input: X, Output: Y` - Token metrics
- `[ask-claude] Successfully generated answer (X chars) with Y sources` - Complete
- `[ask-claude] ThrottlingException on attempt X, retrying in Ys...` - Retry triggered

**Token Usage Tracking**:

Claude responses include token usage metrics:
```json
{
  "usage": {
    "inputTokens": 1523,
    "outputTokens": 287
  }
}
```

Monitor these for cost tracking and optimization.

## Cost Considerations

**Claude 3.5 Haiku pricing** (approximate):
- Input: $0.80 per million tokens
- Output: $4 per million tokens

**Typical query cost**:
- Context (5 docs × 1000 chars): ~1500 input tokens
- Answer (500 chars): ~150 output tokens
- **Cost per query**: ~$0.0018

Using Haiku instead of Sonnet reduces RAG synthesis costs by ~75% while maintaining good quality for summarization tasks.

**Optimization tips**:
- Limit context to top 3-5 most relevant docs
- Truncate document text to 1000 chars (already done)
- Use lower temperature for shorter responses

## Testing

**Manual test**:
```bash
aws lambda invoke \
  --function-name prd-outcome-ops-ai-assist-ask-claude \
  --payload '{
    "query": "How should Lambda error handling work?",
    "context": [
      {
        "score": 0.95,
        "text": "All handlers must catch exceptions...",
        "source": "ADR: lambda-standards"
      }
    ]
  }' \
  response.json

cat response.json | jq '.body | fromjson'
```

**Expected response**:
- Status code: 200
- Body contains: answer (string) and sources (array)
- Execution time: 5-15 seconds
- Answer cites provided sources

## Common Issues

**1. "Failed to generate answer"**
- Cause: Bedrock API error or timeout
- Fix: Check CloudWatch logs, verify IAM permissions

**2. Answer doesn't cite sources**
- Cause: Prompt not clear enough or Claude hallucinating
- Fix: Already handled by strict prompt instructions

**3. ThrottlingException**
- Cause: Too many concurrent requests to Bedrock
- Fix: Retry logic handles this automatically

**4. Answer says "not in context" but info is there**
- Cause: Context not clear or query mismatch
- Fix: Improve document ingestion, add more context

## Related

- **Handler code**: `lambda/ask-claude/handler.py`
- **Consumers**:
  - `docs/lambda-query-kb.md` (orchestrator, when `raw=false`)
  - `docs/lambda-generate-code.md` (for standards summarization)
- **Terraform**: `terraform/lambda.tf` (ask_claude_lambda module)
- **Claude Documentation**: https://docs.anthropic.com/claude/reference/bedrock

<!-- Confluence sync -->
