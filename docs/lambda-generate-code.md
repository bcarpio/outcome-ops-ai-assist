# Generate Code

**Enterprise Component**

## Overview

The `generate-code` Lambda is the core component of the Context Engineering autonomous agent platform. It orchestrates the entire code generation workflow from GitHub issue to pull request, using organizational context (ADRs, code-maps, architectural decisions) to generate code that matches your standards.

This Lambda receives GitHub webhook events for issues labeled "approved-for-generation" and autonomously creates a complete implementation including code, tests, and infrastructure (Terraform/CloudFormation). It queries the knowledge base for relevant organizational patterns, generates a multi-step execution plan, and iterates with self-correction loops until tests pass or human intervention is required.

## Architecture

- **Input:** GitHub issue labeled "approved-for-generation" (via API Gateway webhook)
- **Process:**
  - Query knowledge base for relevant ADRs and patterns
  - Generate multi-step execution plan
  - Execute steps sequentially or in parallel (with dependency management)
  - Generate code, tests, and infrastructure matching organizational standards
  - Self-correction loops based on static analysis and linting
- **Output:** GitHub PR with complete implementation + EventBridge event for test execution

**Workflow:**
```
GitHub Issue → generate-code → [KB Query] → [Plan Generation] → [Step Execution Loop] → GitHub PR → EventBridge (run-tests)
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- Policy-based cost controls
- Compliance audit logging
- Multi-tenant knowledge base architecture

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- LLM endpoints and credentials
- Knowledge base connection details
- GitHub/GitLab/Bitbucket integration tokens
- Cost and policy guardrails

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
