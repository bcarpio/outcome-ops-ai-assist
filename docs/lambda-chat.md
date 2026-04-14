# Chat Streaming Lambda

The chat-streaming Lambda provides a conversational RAG interface with real-time token streaming. It uses Lambda Web Adapter (LWA) to run a FastAPI server inside Lambda, enabling HTTP response streaming that bypasses API Gateway's 30-second timeout limitation and delivers Claude's responses token-by-token to the React UI.

## Key Features

- Real-time token streaming via Lambda Function URLs with RESPONSE_STREAM mode
- Full RAG pipeline: query embedding, S3 Vectors knowledge base search, context assembly, and streaming generation
- Automatic model selection between Claude 3.5 Haiku (fast) and Claude Sonnet 4.5 (complex reasoning)
- Persistent conversation memory in DynamoDB with context window management and summarization
- IAM-authenticated Function URLs with SigV4-signed requests from the Fargate proxy
- Conversation management endpoints (list, retrieve, delete)
- Azure AD end-user authentication via ALB OIDC upstream

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
