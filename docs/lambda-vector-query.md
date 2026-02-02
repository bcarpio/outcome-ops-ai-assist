# Vector Query

**DEPRECATED - Superseded by S3 Vectors**

## Overview

> **Note:** This Lambda has been deprecated. Vector search is now performed directly
> within the `query-kb` Lambda using AWS S3 Vectors native similarity search.
> This documentation is retained for historical reference.

The `vector-query` Lambda previously performed semantic search over the knowledge base by scanning DynamoDB and calculating cosine similarity in Python.

## Migration to S3 Vectors

With the migration to S3 Vectors, this functionality is now handled natively:
- **100-1000x faster** query performance
- **Native cosine similarity** at the database level
- **No client-side** similarity calculations needed
- **Better scalability** for large knowledge bases

## Current Architecture

Vector search is now integrated into `query-kb`:
```
query-kb → [Generate Embedding (Titan v2)] → [S3 Vectors QueryVectors] → [Return Top K]
```

See [lambda-query-kb.md](lambda-query-kb.md) for the current implementation.

## Legacy Architecture (Deprecated)

- **Input:** Internal Lambda invocation from query-kb orchestrator
- **Process:**
  - Generate embedding for query using Bedrock Titan v2
  - Scan DynamoDB code-maps table for document embeddings
  - Calculate cosine similarity in Python
  - Rank and return top K documents
- **Output:** Top K documents with similarity scores

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
