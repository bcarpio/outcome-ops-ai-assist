# Ask Claude

**Enterprise Component**

## Overview

The `ask-claude` Lambda generates RAG (Retrieval-Augmented Generation) answers using Claude. It's invoked by the `query-kb` orchestrator to generate grounded responses from organizational knowledge.

This Lambda receives a natural language query and context chunks from vector search, constructs an optimized prompt with organizational context, calls Claude 3.5 Sonnet via Bedrock Converse API, and returns grounded answers citing sources from ADRs and code-maps. It uses temperature 0.3 for factual, deterministic responses.

## Architecture

- **Input:** Internal Lambda invocation from query-kb orchestrator (includes query and top-K context chunks)
- **Process:**
  - Receive natural language query and context chunks
  - Build RAG prompt with context and source citations
  - Call Claude 3.5 Sonnet via Bedrock Converse API
  - Extract answer and source citations
  - Return grounded answer with attribution
- **Output:** Grounded answer with source citations (ADRs, code-maps)

**Workflow:**
```
query-kb → ask-claude → [Build RAG Prompt] → [Call Claude] → [Extract Answer + Citations] → Return Answer
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
