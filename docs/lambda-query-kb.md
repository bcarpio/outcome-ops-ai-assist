# Lambda: Query KB

The query-kb Lambda orchestrates the full RAG (Retrieval Augmented Generation) pipeline for knowledge base queries. It generates embeddings, performs vector search via S3 Vectors, reranks results using Cohere Rerank 3.5 via Bedrock, and generates answers with Claude using the retrieved context.

## Key Features

- Complete RAG pipeline from query to cited answer in a single invocation
- Vector search using Bedrock Titan Embeddings v2 and S3 Vectors
- Reranking with Cohere Rerank 3.5 for precision result ordering
- Workspace-scoped queries for multi-repository knowledge isolation
- Raw mode for programmatic use by other Lambda functions
- Source citations included with every response
- Graceful fallback to cosine similarity ordering if reranking fails
- Typical query latency of 7-20 seconds

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
