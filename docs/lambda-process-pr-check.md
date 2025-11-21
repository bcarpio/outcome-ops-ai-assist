# Process PR Check

**Enterprise Component**

## Overview

The `process-pr-check` Lambda is a worker function that processes architectural check jobs queued by the `analyze-pr` Lambda. It executes specific checks and posts detailed results to pull requests.

This Lambda is triggered by SQS messages from the pr-checks-queue. It routes to the appropriate check handler based on checkType (ADR_COMPLIANCE, README_FRESHNESS, TEST_COVERAGE, BREAKING_CHANGES, ARCHITECTURAL_DUPLICATION), executes the check using internal LLMs and knowledge base context, stores results in DynamoDB for audit trails, and posts detailed check results as PR comments.

## Architecture

- **Input:** SQS message from analyze-pr Lambda (includes checkType, pr_number, repository, changed_files)
- **Process:**
  - Parse SQS message and extract check details
  - Route to appropriate check handler:
    - ADR_COMPLIANCE: Validates Lambda handlers and Terraform against ADR standards
    - README_FRESHNESS: Checks if README needs updating based on changes
    - TEST_COVERAGE: Validates new handlers have corresponding tests
    - BREAKING_CHANGES: Detects dependencies and potential breaking changes
    - ARCHITECTURAL_DUPLICATION: Identifies similar functionality across repos
  - Execute check using LLM and knowledge base
  - Store results in DynamoDB (with timestamp, check details, pass/fail)
  - Post detailed results as PR comment
- **Output:** DynamoDB record + GitHub PR comment with check results

**Workflow:**
```
SQS Message → process-pr-check → [Route to Handler] → [Execute Check] → DynamoDB + GitHub Comment
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
