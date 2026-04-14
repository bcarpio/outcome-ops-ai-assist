# Technical Reference

OutcomeOps AI Assist is built on a serverless AWS architecture using Lambda, Bedrock, S3 Vectors, DynamoDB, and SQS. This reference covers the complete tech stack, project structure, core features, environment configuration, resource tagging, deployment, monitoring, testing, and troubleshooting.

## Key Features

- Serverless compute with AWS Lambda (Python 3.12)
- AI-powered RAG pipeline using Bedrock (Titan Embeddings v2 + Claude)
- S3 Vectors for native vector storage and semantic search
- DynamoDB for state tracking and conversation persistence
- SQS FIFO queues for async batch processing
- Terraform IaC for all infrastructure resources
- GitHub API integration for source control operations
- GitHub Actions CI/CD for automated deployments

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
