# Lambda: Process PR Check

The process-pr-check Lambda is the worker in the PR analysis system. It consumes check jobs from the SQS FIFO queue, routes them to specialized check handlers, executes AI-based analysis using Claude via Bedrock, posts results as GitHub PR comments, and stores results in DynamoDB for tracking.

## Key Features

- Six specialized check handlers: ADR compliance, README freshness, test coverage, breaking changes, architectural duplication, and license compliance
- SQS FIFO queue consumption with partial batch failure reporting
- AI-powered analysis using Claude via Bedrock with knowledge base context
- Automatic GitHub PR comment posting with structured results
- DynamoDB result storage for audit trails and tracking
- Unified diff analysis for precise code-level feedback
- Configurable check routing based on changed file patterns

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
