# Lambda: Process PR Check – Enterprise Component

**Purpose**
Executes distributed architectural checks on Pull Requests in regulated environments:
- Processes SQS check jobs queued by analyze-pr Lambda
- Executes ADR compliance, test coverage, breaking change, and duplication checks
- Posts detailed results as PR comments with remediation guidance
- Stores audit-ready results in DynamoDB

**Enterprise Features**
- LLM-powered architectural analysis with organizational context
- Knowledge base integration for ADR-aware validation
- DynamoDB audit trails with check lineage and reasoning
- GitHub API integration with detailed PR annotations
- Air-gapped deployment with internal LLMs
- Cost guardrails and policy-based execution controls

This component is part of the proprietary enterprise platform and is only available via licensed deployments or transformation engagements.

For enterprise briefings and licensing:
https://www.outcomeops.ai

Related open resources:
- [Architecture Overview](../../docs/architecture.md)
- [ADR-007: Documentation-Driven Decision Making](../../docs/adr/ADR-007-documentation-driven-decisions.md)
- [Open Framework on GitHub](https://github.com/bcarpio/outcome-ops-ai-assist)
