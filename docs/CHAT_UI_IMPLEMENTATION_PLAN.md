# Chat UI Implementation Plan

Port the chat interface and streaming RAG system from app.retrieveit.ai to outcome-ops-ai-assist.

## Overview

Add a web-based chat interface that allows users to query the knowledge base with:
- Real-time streaming responses (Lambda Web Adapter pattern)
- Conversation history with persistence
- Long-term conversation memory (vector-based)
- Source citations from KB results

## Scope

**In Scope:**
- Chat streaming Lambda (LWA + Flask/FastAPI)
- Conversation storage (DynamoDB)
- Conversation memory (S3 Vectors chunks)
- React chat UI with streaming display
- Terraform infrastructure

**Out of Scope (TBD later):**
- Authentication (deployed in customer AWS account)
- User management
- Billing/usage tracking

## Architecture

```
Browser (React)
    |
    | POST /chat (streaming)
    v
Lambda Function URL (RESPONSE_STREAM mode)
    |
    | LWA Layer + FastAPI
    v
Chat Handler
    |
    +---> S3 Vectors: Search KB documents
    +---> S3 Vectors: Search conversation memory
    +---> DynamoDB: Load recent messages
    |
    v
Bedrock converse_stream (Claude)
    |
    | NDJSON tokens
    v
Browser renders streaming response
    |
    v
DynamoDB: Save message + update conversation
```

## Implementation Steps

### Step 1: Terraform Infrastructure

**Files to create/modify:**
- `terraform/dynamodb-chat.tf` - Conversations table
- `terraform/lambda-chat.tf` - Chat streaming Lambda + Function URL
- `terraform/api-gateway.tf` - Add chat REST routes (list/delete conversations)
- `terraform/ssm.tf` - Add chat config parameters

**DynamoDB Schema (single table, simple):**
```
PK: CONV#{conversation_id}
SK: METADATA | MSG#{timestamp}#{message_id}

Conversation record:
  PK: CONV#{id}, SK: METADATA
  title, message_count, memory_chunk_count, created_at, updated_at

Message record:
  PK: CONV#{id}, SK: MSG#{timestamp}#{id}
  role (user|assistant), content, sources[], created_at
```

**Lambda Configuration:**
- Runtime: Python 3.12
- Memory: 1024 MB
- Timeout: 900 seconds (15 min)
- Layers: Lambda Web Adapter (LWA)
- Function URL: RESPONSE_STREAM invoke mode

### Step 2: Chat Streaming Lambda

**Files to create:**
- `lambda/chat/handler.py` - Main chat logic
- `lambda/chat/main.py` - FastAPI app wrapper
- `lambda/chat/run.sh` - LWA bootstrap script
- `lambda/chat/requirements.txt` - Dependencies

**Core Flow:**
1. Parse request (message, conversation_id, top_k)
2. Get/create conversation in DynamoDB
3. Load last 10 messages for context
4. Search conversation memory (if chunks exist)
5. Search KB documents (existing S3 Vectors)
6. Build prompt with: system instructions + memory + KB results + recent messages + user query
7. Stream response from Bedrock converse_stream
8. Save assistant message to DynamoDB
9. Create memory chunk if message_count % 20 == 0

**NDJSON Response Format:**
```json
{"type": "metadata", "conversation_id": "...", "sources": [...]}
{"type": "token", "text": "Based"}
{"type": "token", "text": " on"}
{"type": "done", "message_id": "..."}
```

### Step 3: Conversation Memory

**Integration with existing S3 Vectors:**
- Use same vector bucket/index as KB documents
- Memory chunks stored with `doc_type: "conversation_memory"`
- Key format: `conversations/{conv_id}/memory_chunk_{n}`

**Memory Creation (every 20 messages):**
1. Collect messages since last chunk
2. Summarize conversation segment
3. Generate embedding
4. Store in S3 Vectors with conversation metadata

**Memory Search (on each query):**
1. Generate embedding for user query
2. Search S3 Vectors filtered by conversation_id + doc_type
3. Return top 3 most relevant memory chunks
4. Include in prompt context

### Step 4: React Chat UI

**Files to create:**
- `ui/` - New React app (Vite + TypeScript + Tailwind)
- `ui/src/pages/Chat.tsx` - Main chat interface
- `ui/src/components/MessageList.tsx` - Streaming message display
- `ui/src/components/ConversationSidebar.tsx` - Conversation list
- `ui/src/components/SourceCitations.tsx` - KB source display
- `ui/src/lib/api.ts` - API client with streaming support

**Key Features:**
- Real-time token streaming (ReadableStream API)
- Auto-scroll to latest message
- Markdown rendering for responses
- Expandable source citations
- Conversation list with delete
- Loading states (thinking/searching/typing)

**Build & Deploy:**
- Vite build to S3
- CloudFront distribution
- Add to Terraform

## File Reference (from app.retrieveit.ai)

| Source File | Purpose |
|-------------|---------|
| `lambda/chat/handler.py` | Streaming chat logic (port + simplify) |
| `lambda/chat/main.py` | FastAPI wrapper |
| `lambda/chat/run.sh` | LWA bootstrap |
| `ui/src/pages/Chat.tsx` | Chat UI (port + simplify) |
| `ui/src/lib/api.ts` | Streaming API client |
| `terraform/lambda.tf` | LWA Lambda config |
| `docs/CONVERSATION_MEMORY.md` | Memory architecture |

## Testing

- Unit tests for chat handler
- Integration test for streaming endpoint
- Manual test of full chat flow

## Success Criteria

1. User can send message and see streaming response
2. Conversation persists across page reloads
3. Long conversations maintain context via memory chunks
4. Source citations show KB documents used
5. Multiple conversations supported
