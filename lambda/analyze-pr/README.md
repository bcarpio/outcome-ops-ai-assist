# Lambda: Analyze PR – Enterprise Component

**Purpose**
Orchestrates Pull Request analysis in regulated environments:
- Detects changed files and determines relevant compliance checks
- Queues async jobs for ADR compliance, test coverage, architectural duplication, etc.
- Posts status updates to PRs
- Routes check execution to specialized worker Lambdas

**Enterprise Features**
- Full GitHub App integration with fine-grained permissions
- FIFO-ordered processing per PR to prevent race conditions
- Automatic check-result aggregation and executive reporting
- Audit trails for every architectural decision
- Air-gapped deployment with internal LLMs
- Policy-based check selection and cost controls

This component is part of the proprietary enterprise platform and is only available via licensed deployments or transformation engagements.

For enterprise briefings and licensing:
https://www.outcomeops.ai

Related open resources:
- [Architecture Overview](../../docs/architecture.md)
- [ADR-001: Creating ADRs](../../docs/adr/ADR-001-create-adrs.md)
- [Open Framework on GitHub](https://github.com/bcarpio/outcome-ops-ai-assist)
