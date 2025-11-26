# Generate Code Maps

**Enterprise Component**

## Overview

The `generate-code-maps` Lambda discovers code units from repository structure and generates architectural summaries using internal LLMs. These code maps augment the knowledge base with organizational patterns, conventions, and architectural decisions embedded in your codebase structure.

This Lambda supports both full regeneration (for initial setup or major refactorings) and incremental updates (hourly automated runs that only process changed components), optimizing for cost and performance.

## Architecture

- **Input:** EventBridge schedule (hourly) or direct invocation with repository list
- **Process:**
  - Discover code units (Lambda handlers, K8s services, modules)
  - Detect changes via git-based comparison (incremental mode)
  - Queue repositories to SQS for async architectural summary generation
  - Async processing naturally rate-limits LLM API calls
- **Output:** Repository messages sent to SQS for downstream processing

**Workflow:**
```
EventBridge/CLI → generate-code-maps → [Git Change Detection] → [Code Discovery]
                                                                       ↓
                                                            [Queue to repo-summaries-queue]
                                                                       ↓
                                                            process-repo-summary → [LLM Summary] → DynamoDB/S3
                                                                                 ↓
                                                                      [Queue code units] → code-maps-queue
                                                                                 ↓
                                                                      process-batch-summary → DynamoDB
```

This async architecture enables:
- **Scalability**: Process 100s of repos without hitting LLM rate limits
- **Resilience**: Failed repos retry independently via SQS/DLQ
- **Enterprise**: Works in new AWS accounts, Azure shops, isolated environments

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- Pluggable backend architecture (Lambda, K8s, monolith)
- Incremental updates with git-based change detection
- Multi-tenant knowledge base architecture
- FIFO queue processing for ordered batch execution

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- Repository allowlists and access credentials
- LLM endpoints and model selection
- Storage backends (DynamoDB, S3, SQS)
- Backend type selection (Lambda, K8s, monolith)

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
