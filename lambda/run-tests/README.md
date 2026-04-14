# Lambda: Run Tests – Enterprise Component

**Purpose**
Executes automated testing with AI-powered error correction in regulated environments:
- Runs test suites (pytest, jest, etc.) on generated code in isolated environments
- Classifies errors (syntax vs logic) to determine fixability
- Queries knowledge base for organizational testing patterns (ADR-006, etc.)
- Applies KB-aware auto-fix with bounded retry logic

**Enterprise Features**
- Air-gapped deployment with internal LLMs (Bedrock, Azure OpenAI, on-prem)
- Knowledge base-aware error correction (10x higher success rate than generic retry)
- Self-healing workflows with intelligent escalation to human review
- S3 artifact storage with CloudTrail audit logging
- EventBridge integration for workflow progression
- Cost guardrails and policy-based execution controls

This component is part of the proprietary enterprise platform and is only available via licensed deployments or transformation engagements.

For enterprise briefings and licensing:
https://www.outcomeops.ai

Related open resources:
- [Architecture Overview](../../docs/architecture.md)
- [ADR-006: Python Testing Import Patterns](../../docs/adr/ADR-006-python-testing-imports.md)
- [Open Framework on GitHub](https://github.com/bcarpio/outcome-ops-ai-assist)
