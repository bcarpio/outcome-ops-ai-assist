# Process Batch Summary

**Enterprise Component**

## Overview

The `process-batch-summary` Lambda is an SQS consumer that processes code map batches created by the generate-code-maps Lambda. For each batch, it fetches file contents from GitHub, generates detailed summaries using internal LLMs, creates vector embeddings, and stores results in the knowledge base.

This Lambda processes different batch types (infrastructure, handlers, tests, shared utilities, schemas, docs) with specialized prompts optimized for each code organization pattern.

## Architecture

- **Input:** SQS FIFO queue messages containing file batches
- **Process:**
  - Fetch file contents from GitHub repositories
  - Generate specialized summaries based on batch type
  - Create vector embeddings for semantic search
  - Store summaries in knowledge base
- **Output:** Batch summaries stored in DynamoDB with embeddings

**Workflow:**
```
SQS Message → process-batch-summary → [GitHub Fetch] → [LLM Summary] → [Embedding Generation] → DynamoDB
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- FIFO-ordered processing for consistency
- Retry logic with exponential backoff
- Partial batch failure handling
- Compliance audit logging

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- GitHub credentials and API access
- LLM endpoints and model selection
- Storage backends (DynamoDB)
- Batch processing configuration

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
- SQS event source mapping tests
- Performance and cost benchmarking

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
