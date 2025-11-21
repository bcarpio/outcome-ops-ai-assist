# Run Tests

**Enterprise Component**

## Overview

The `run-tests` Lambda performs automated test execution, AI-powered error correction, and self-healing workflows. It validates generated code before human review and automatically fixes common issues using knowledge base context.

This Lambda is triggered by EventBridge after code generation completes. It clones the target branch, bootstraps a test environment, runs the repository's test suite, classifies errors (syntax vs logic), queries the knowledge base for relevant organizational patterns, and attempts automatic fixes with bounded retry logic. Syntax/import errors are typically fixed automatically using ADR context, while logic errors are escalated to human review.

## Architecture

- **Input:** EventBridge event from `generate-code` Lambda (includes repo, branch, PR details)
- **Process:**
  - Clone target branch using GitHub PAT
  - Bootstrap virtual environment and install dependencies
  - Execute `make test` to run test suite
  - Classify test failures (syntax_error, import_error, logic_error)
  - For fixable errors: Query knowledge base for organizational patterns (ADR-006, etc.)
  - Apply KB-aware auto-fix with LLM
  - Retry tests (bounded attempts)
  - Upload test results and logs to S3
- **Output:** EventBridge event with test results + S3 artifacts + GitHub PR comment

**Workflow:**
```
EventBridge Event → run-tests → [Clone Repo] → [Run Tests] → [Classify Errors] → [KB Query] → [Auto-Fix] → [Retry] → EventBridge (success/failure) + S3 Logs
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
