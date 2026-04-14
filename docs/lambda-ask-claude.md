# Lambda: Ask Claude

The ask-claude Lambda generates natural language answers using Claude 3.5 Haiku via the Bedrock Converse API. It implements the Retrieval Augmented Generation (RAG) synthesis step by taking context from vector search results and producing grounded, source-cited responses. Haiku is used for cost-effective synthesis since the heavy lifting is done by the vector search layer.

## Key Features

- RAG synthesis that generates answers grounded exclusively in provided context documents
- Source citation in every response with references to specific ADRs and documentation
- Cost-effective design using Claude 3.5 Haiku (~$0.0018 per query)
- Exponential backoff retry logic for Bedrock throttling and transient errors
- Low temperature (0.3) inference for factual, deterministic responses
- Invoked by query-kb orchestrator and generate-code Lambda for standards summarization
- Token usage tracking for cost monitoring and optimization

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
