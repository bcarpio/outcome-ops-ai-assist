# Process Repository Summary

**Enterprise Component**

## Overview

The `process-repo-summary` Lambda is an SQS consumer that processes repositories asynchronously to generate architectural summaries. For each repository, it analyzes the file structure, generates a comprehensive architectural overview using internal LLMs, creates vector embeddings, and stores results in the knowledge base.

This Lambda enables scalable processing of hundreds of repositories without hitting API rate limits, making it ideal for enterprise deployments across diverse environments (new AWS accounts, Azure shops, isolated/air-gapped systems).

## Architecture

- **Input:** SQS FIFO queue messages containing repository metadata
- **Process:**
  - Analyze repository file structure
  - Generate architectural summary using LLM
  - Create vector embeddings for semantic search
  - Store summary in knowledge base (DynamoDB + S3)
  - Queue code units for detailed batch processing
- **Output:** Architectural summaries stored in DynamoDB with embeddings, code units queued to downstream processor

**Workflow:**
```
SQS Message → process-repo-summary → [LLM Summary] → [Embedding Generation] → DynamoDB + S3
                                   ↓
                              [Queue Code Units] → code-maps-queue → process-batch-summary
```

## Enterprise Features

- Async processing with natural rate limiting (SQS batch_size=1)
- Aggressive retry logic with exponential backoff (10s → 120s)
- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- FIFO-ordered processing for consistency
- Partial batch failure handling
- Dead letter queue for failed repositories
- Compliance audit logging
- Multi-cloud and hybrid-cloud support

## Scalability

This architecture enables processing at scale without throttling:
- **Small deployments**: 4-20 repos process smoothly
- **Medium deployments**: 90+ Lambda handlers (e.g., myfantasy.ai)
- **Enterprise deployments**: 100s of repos across multiple teams (e.g., Hyatt)
- **New AWS accounts**: Works with minimal Bedrock quotas
- **Azure/on-prem clients**: Processes at whatever rate LLM API allows

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- GitHub credentials and API access
- LLM endpoints and model selection
- Storage backends (DynamoDB, S3)
- SQS queue URLs
- Retry and timeout settings

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
- Throttling and retry scenario tests
- Performance and cost benchmarking

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
