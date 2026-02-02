# Chat Streaming

**Enterprise Component**

## Overview

The `chat` Lambda provides a streaming chat interface for the OutcomeOps Chat UI. It handles real-time conversations with Claude via Bedrock, persists conversation history, and performs RAG queries for knowledge-grounded responses.

This Lambda uses Lambda Web Adapter (LWA) to enable HTTP response streaming, bypassing API Gateway's 30-second timeout limitation.

## Architecture

- **Runtime:** Python 3.12 with Lambda Web Adapter layer
- **Framework:** FastAPI/ASGI for async streaming
- **Invocation:** Lambda Function URL with IAM authentication (SigV4)
- **Storage:** DynamoDB for conversations, S3 Vectors for memory embeddings
- **LLM:** Bedrock Claude 4.5 Sonnet (streaming) + Titan v2 (embeddings)

**Workflow:**
```
Chat UI (Fargate) → SigV4 Signed Request → Lambda Function URL
         ↓
    [chat Lambda]
         ↓
    ├→ Load conversation from DynamoDB
    ├→ Generate query embedding (Titan v2)
    ├→ Search S3 Vectors for relevant KB context
    ├→ Stream response from Claude (ConverseStream API)
    ├→ Save message to DynamoDB
    └→ Update conversation memory in S3 Vectors
```

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| POST | /chat | Stream a new message in a conversation |
| GET | /conversations | List user conversations |
| GET | /conversations/{id} | Get conversation with messages |
| DELETE | /conversations/{id} | Delete a conversation |
| GET | /health | Health check for ALB |

## Enterprise Features

- Lambda Web Adapter for true HTTP streaming (15-minute timeout)
- IAM-authenticated Function URLs (no anonymous access)
- OIDC authentication at ALB level (Azure AD integration)
- Conversation memory via S3 Vectors embeddings
- RAG integration for knowledge-grounded responses
- Audit trail for all conversations
- Policy-based execution controls

## Deployment

This component is deployed alongside the Chat UI infrastructure:
- Fargate Spot cluster for the React UI
- Internal ALB or public ALB with OIDC
- Lambda Function URL for streaming API
- DynamoDB table for conversation persistence

## Configuration

SSM Parameters:
- `/{env}/{app}/s3vectors/bucket-name` - S3 Vectors bucket
- `/{env}/{app}/s3vectors/index-name` - Vector index name
- `/{env}/{app}/dynamodb/chat-table` - Chat DynamoDB table

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
