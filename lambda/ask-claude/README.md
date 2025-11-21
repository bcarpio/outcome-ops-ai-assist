# Lambda: Ask Claude – Enterprise Component

**Purpose**
Generates RAG (Retrieval-Augmented Generation) answers from organizational knowledge in regulated environments:
- Receives natural language queries and context chunks from vector search
- Constructs optimized prompts with ADRs and code-maps
- Returns grounded answers with source citations
- Uses temperature-tuned settings for factual, deterministic responses

**Enterprise Features**
- Air-gapped deployment with internal LLMs (Bedrock, Azure OpenAI, on-prem)
- Production-refined prompt engineering for RAG accuracy
- Source attribution and citation tracking for compliance
- Token usage optimization to minimize costs
- Audit trails for every LLM invocation
- Policy-based response filtering and validation

This component is part of the proprietary enterprise platform and is only available via licensed deployments or transformation engagements.

For enterprise briefings and licensing:
https://www.outcomeops.ai

Related open resources:
- [Architecture Overview](../../docs/architecture.md)
- [ADR-007: Documentation-Driven Decision Making](../../docs/adr/ADR-007-documentation-driven-decisions.md)
- [Open Framework on GitHub](https://github.com/bcarpio/outcome-ops-ai-assist)
