# Ingest Docs

**Enterprise Component**

## Overview

The `ingest-docs` Lambda scans repositories via GitHub API and ingests documentation into the knowledge base for semantic search and code generation. It processes ADRs (Architecture Decision Records), READMEs, and function-specific documentation with smart chunking for large files.

This Lambda runs on a schedule (hourly by default) and supports both full ingestion of all configured repositories and filtered ingestion of specific repositories.

## Architecture

- **Input:** EventBridge schedule (hourly) or direct invocation with optional repository filter
- **Process:**
  - Fetch documentation from GitHub repositories
  - Apply smart text chunking for large documents
  - Generate vector embeddings for semantic search
  - Store in knowledge base with metadata
- **Output:** Documentation stored in DynamoDB with embeddings + S3 archival

**Workflow:**
```
EventBridge/CLI → ingest-docs → [GitHub API] → [Text Chunking] → [Embedding Generation] → DynamoDB/S3
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom embedding model integration (Bedrock Titan, Azure OpenAI, on-prem)
- Smart text chunking for large documents
- Incremental updates with change detection
- Multi-tenant knowledge base architecture
- Compliance audit logging

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- Repository allowlists and GitHub credentials
- Embedding model endpoints and selection
- Storage backends (DynamoDB, S3)
- Document filtering rules

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
