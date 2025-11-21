# Vector Query

**Enterprise Component**

## Overview

The `vector-query` Lambda performs semantic search over the knowledge base. It generates embeddings for natural language queries and returns the most relevant organizational documents (ADRs, code-maps).

This Lambda is invoked by the `query-kb` orchestrator. It receives a natural language query and optional topK parameter, generates an embedding for the query using Bedrock Titan Embeddings v2, scans DynamoDB for all document embeddings, calculates cosine similarity between query and documents, and returns top K most similar documents with scores.

## Architecture

- **Input:** Internal Lambda invocation from query-kb orchestrator (includes query and topK parameter)
- **Process:**
  - Receive natural language query and topK (default: 5)
  - Generate embedding for query using Bedrock Titan Embeddings v2
  - Scan DynamoDB code-maps table for document embeddings
  - Calculate cosine similarity between query embedding and each document
  - Rank documents by similarity score
  - Return top K most similar documents with scores and metadata
- **Output:** Top K documents with similarity scores, content, and source attribution

**Workflow:**
```
query-kb → vector-query → [Generate Embedding] → [Scan DynamoDB] → [Calculate Similarity] → [Rank + Return Top K]
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- Policy-based cost controls
- Compliance audit logging
- Multi-tenant knowledge base architecture

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- LLM endpoints and credentials
- Knowledge base connection details
- GitHub/GitLab/Bitbucket integration tokens
- Cost and policy guardrails

Specific parameter structures are documented in enterprise deployments.

## Deployment

This component is deployed as part of the full OutcomeOps platform via:
- Terraform (infrastructure as code)
- Air-gapped installer (for regulated environments)

Deployment scripts and configurations are included in enterprise licensing.

## Testing

The enterprise platform includes comprehensive test coverage:
- Unit tests with >90% coverage
- Integration tests with mocked AWS services
- End-to-end workflow tests
- Performance and cost benchmarking

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
