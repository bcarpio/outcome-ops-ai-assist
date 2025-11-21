# Query KB

**Enterprise Component**

## Overview

The `query-kb` Lambda is the single entry point for querying the OutcomeOps knowledge base. It orchestrates the complete RAG (Retrieval Augmented Generation) pipeline by coordinating vector search and Claude answer generation, returning natural language answers backed by organizational knowledge with source attribution.

This Lambda receives natural language queries from user-facing interfaces (MS Teams, CLI, Slack) and returns answers grounded in ADRs, code maps, and documentation.

## Architecture

- **Input:** Natural language query from user interfaces (API Gateway, direct invocation)
- **Process:**
  - Invoke vector search to find relevant documents
  - Check if relevant context was found
  - Invoke LLM to generate grounded answers with citations
  - Return natural language response with source attribution
- **Output:** JSON response with answer and sources, or "not found" message

**Workflow:**
```
User Query → query-kb → [Vector Search] → [Context Check] → [LLM Answer Generation] → Response + Sources
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- Lambda-based orchestration for modularity
- Source attribution and citation tracking
- Fallback handling for no-results scenarios
- Compliance audit logging

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- Lambda function ARNs for vector search and answer generation
- LLM endpoints and credentials
- Knowledge base connection details
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
- End-to-end RAG pipeline tests
- Performance and latency benchmarking

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
