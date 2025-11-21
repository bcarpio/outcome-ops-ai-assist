# Lambda: Vector Query – Enterprise Component

**Purpose**
Performs semantic search over organizational knowledge base in regulated environments:
- Generates embeddings for natural language queries
- Calculates similarity scores against ADRs and code-maps in DynamoDB
- Returns top-K most relevant documents with scores and metadata
- Optimizes retrieval for organizational context, not just semantic similarity

**Enterprise Features**
- Air-gapped deployment with internal embedding models (Bedrock Titan, Azure OpenAI, on-prem)
- Proprietary relevance ranking algorithms for organizational context
- DynamoDB-based vector storage with audit logging
- Performance optimization for large knowledge bases
- SSM Parameter Store integration for runtime configuration
- Cost guardrails and policy-based execution controls

This component is part of the proprietary enterprise platform and is only available via licensed deployments or transformation engagements.

For enterprise briefings and licensing:
https://www.outcomeops.ai

Related open resources:
- [Architecture Overview](../../docs/architecture.md)
- [ADR-007: Documentation-Driven Decision Making](../../docs/adr/ADR-007-documentation-driven-decisions.md)
- [Open Framework on GitHub](https://github.com/bcarpio/outcome-ops-ai-assist)
