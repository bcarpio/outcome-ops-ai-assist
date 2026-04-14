# CLI Usage Guide: outcome-ops-assist

The `outcome-ops-assist` CLI tool provides a command-line interface for querying the knowledge base using RAG and analyzing GitHub Pull Requests for compliance, duplication, and quality. It invokes deployed Lambda functions in your AWS account and returns formatted results directly in the terminal.

## Key Features

- Query the knowledge base for architectural decisions, coding standards, and patterns
- Analyze Pull Requests for ADR compliance, test coverage, and breaking changes
- Support for multiple environments (dev, prd) via environment flag
- Workspace-scoped queries for multi-repository setups
- Raw output mode for integration with other tools and scripts
- Formatted terminal output with source citations
- Direct Lambda invocation using AWS credentials

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
