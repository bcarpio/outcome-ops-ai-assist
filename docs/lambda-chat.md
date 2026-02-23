# Chat Streaming Lambda

The `chat-streaming` Lambda provides a conversational RAG interface with real-time token streaming. It uses Lambda Web Adapter (LWA) to enable HTTP response streaming, bypassing API Gateway's 30-second timeout limitation.

## Overview

| Attribute | Value |
|-----------|-------|
| **Function Name** | `{env}-outcome-ops-ai-assist-chat-streaming` |
| **Runtime** | Python 3.12 |
| **Timeout** | 15 minutes |
| **Memory** | 1024 MB |
| **Trigger** | Lambda Function URL (IAM auth) |
| **Purpose Tag** | `chat-streaming` |

## Architecture

```
+-------------+     +------------------+     +-------------------+
|   Fargate   |---->| Lambda Function  |---->| Bedrock Claude    |
|   (React UI)|     | URL (Streaming)  |     | (Haiku/Sonnet)    |
+-------------+     +--------+---------+     +-------------------+
      |                      |
      | SigV4 Signed         |
      | Requests             v
      |              +-------+--------+
      |              | Lambda Web     |
      |              | Adapter (LWA)  |
      |              +-------+--------+
      |                      |
      |                      v
      |              +-------+--------+     +-------------+
      |              | FastAPI App    |---->| S3 Vectors  |
      |              | (Streaming)    |     | (KB Search) |
      |              +----------------+     +-------------+
      |                      |
      |                      v
      |              +----------------+
      +------------->| DynamoDB       |
                     | (Conversations)|
                     +----------------+
```

## Why Lambda Web Adapter?

Standard Lambda integrations have limitations for streaming:
- **API Gateway REST/HTTP**: 30-second timeout, no streaming
- **Lambda Function URLs**: Support streaming but require HTTP server

Lambda Web Adapter (LWA) solves this by:
1. Running a FastAPI server inside Lambda
2. Translating Lambda events to HTTP requests
3. Enabling `RESPONSE_STREAM` mode for real-time token delivery

## Request Flow

### 1. User Sends Message

```json
POST /chat
{
  "conversation_id": "conv-123",
  "message": "How do we structure Lambda handlers?"
}
```

### 2. RAG Pipeline

1. **Query Embedding**: Generate embedding for user message via Titan v2
2. **KB Search**: Query S3 Vectors for relevant documents (ADRs, code maps)
3. **Context Assembly**: Combine retrieved docs + conversation history
4. **Streaming Response**: Stream Claude response tokens in real-time

### 3. Streaming Response

```
data: {"type": "token", "content": "Based"}
data: {"type": "token", "content": " on"}
data: {"type": "token", "content": " ADR"}
data: {"type": "token", "content": "-005"}
...
data: {"type": "done", "conversation_id": "conv-123"}
```

## Models

| Model | Use Case | Speed | Cost |
|-------|----------|-------|------|
| **Claude 3.5 Haiku** | Standard queries | Fast (~2s first token) | Low |
| **Claude Sonnet 4.5** | Complex reasoning | Slower (~4s first token) | Higher |

Model selection is automatic based on query complexity, or can be explicitly requested.

## Conversation Memory

Conversations are persisted in DynamoDB:

```
Table: {env}-outcome-ops-ai-assist-chat

PK: CONV#{conversation_id}
SK: MSG#{timestamp}

Attributes:
- role: "user" | "assistant"
- content: message text
- metadata: model used, tokens, latency
```

Memory features:
- **Context Window**: Last N messages included in prompt
- **Summarization**: Older messages summarized to save tokens
- **Semantic Memory**: Key facts stored in S3 Vectors for long-term recall

## Security

### IAM Authentication

The Function URL uses `AWS_IAM` authorization:

```hcl
resource "aws_lambda_function_url" "chat_streaming" {
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"
}
```

Only the Fargate task role can invoke the function:

```hcl
tasks_iam_role_statements = [
  {
    effect    = "Allow"
    actions   = ["lambda:InvokeFunctionUrl"]
    resources = [module.chat_streaming_lambda.lambda_function_arn]
  }
]
```

### No Anonymous Access

- Function URL requires SigV4-signed requests
- Fargate proxy signs requests using task IAM role
- End users authenticate via ALB OIDC (Azure AD)

## Environment Variables

| Variable | Description |
|----------|-------------|
| `ENV` | Environment name (dev/prd) |
| `APP_NAME` | Application identifier |
| `AWS_LAMBDA_EXEC_WRAPPER` | `/opt/bootstrap` - LWA entrypoint |
| `AWS_LWA_INVOKE_MODE` | `RESPONSE_STREAM` - Enable streaming |
| `AWS_LWA_READINESS_CHECK_PATH` | `/health` - Health check endpoint |
| `PORT` | `8000` - FastAPI server port |

## IAM Permissions

```hcl
policy_statements = {
  # Conversation storage
  dynamodb = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:Query", ...]

  # Configuration
  ssm_read = ["ssm:GetParameter", "ssm:GetParameters"]

  # LLM and embeddings
  bedrock_invoke = [
    "bedrock:InvokeModel",
    "bedrock:InvokeModelWithResponseStream",
    "bedrock:Converse",
    "bedrock:ConverseStream"
  ]

  # Knowledge base search
  s3_vectors = ["s3vectors:QueryVectors", "s3vectors:GetVectors", ...]
}
```

## Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check for LWA readiness |
| `/chat` | POST | Send message, receive streaming response |
| `/conversations` | GET | List user's conversations |
| `/conversations/{id}` | GET | Get conversation history |
| `/conversations/{id}` | DELETE | Delete conversation |

## Source Code

```
lambda/chat/
├── handler.py          # FastAPI app with streaming endpoints
├── rag.py              # RAG pipeline (search + generation)
├── memory.py           # Conversation memory management
├── models.py           # Pydantic request/response models
├── requirements.txt    # Dependencies (fastapi, boto3, etc.)
└── run.sh              # LWA entrypoint script
```

## Local Development

```bash
# Install dependencies
cd lambda/chat
pip install -r requirements.txt -r requirements-dev.txt

# Run locally (without LWA)
uvicorn handler:app --reload --port 8000

# Test streaming
curl -X POST http://localhost:8000/chat \
  -H "Content-Type: application/json" \
  -d '{"message": "What are our testing standards?"}'
```

## Deployment

The Lambda is deployed via Terraform in `terraform/lambda-chat.tf`:

```bash
cd terraform
terraform apply -var-file=prd.tfvars
```

The Function URL is stored in SSM for the Fargate proxy:
- Parameter: `/{env}/{app_name}/api/chat-streaming-url`

## Monitoring

### CloudWatch Logs

Log group: `/aws/lambda/{env}-outcome-ops-ai-assist-chat-streaming`

Key log patterns:
- `[RAG]` - Knowledge base search results
- `[STREAM]` - Token streaming events
- `[MEMORY]` - Conversation persistence

### Metrics

- **Duration**: Track streaming response time
- **Invocations**: Request volume
- **Errors**: Failed requests (usually Bedrock throttling)

## Troubleshooting

### "Task timed out"

The 15-minute timeout should handle most queries. If timing out:
- Check Bedrock throttling (cross-region inference helps)
- Reduce context window size
- Check S3 Vectors query performance

### "Access Denied" on Function URL

- Verify Fargate task role has `lambda:InvokeFunctionUrl` permission
- Check SigV4 signing is working (credentials available)
- Verify Function URL authorization type is `AWS_IAM`

### Slow First Token

Cold starts add ~3-5 seconds. Mitigations:
- Provisioned concurrency (adds cost)
- Keep Lambda warm with scheduled pings
- Accept cold starts for cost optimization (current approach)
