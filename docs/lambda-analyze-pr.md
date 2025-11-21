# Analyze PR

**Enterprise Component**

## Overview

The `analyze-pr` Lambda orchestrates automated pull request analysis for architectural compliance. It analyzes PR diffs, determines which organizational checks to run based on changed files, and queues jobs for async processing.

This Lambda is triggered manually or via GitHub webhooks. It fetches the PR diff from GitHub API, parses changed files to determine relevant checks (ADR compliance, test coverage, breaking changes, architectural duplication, README freshness), queues SQS jobs for parallel check execution, and posts an initial analysis status comment to the PR.

## Architecture

- **Input:** GitHub webhook or manual invocation with PR details (pr_number, repository)
- **Process:**
  - Fetch PR diff from GitHub API
  - Parse changed files and determine relevant checks
  - For each check type:
    - ADR Compliance: Validates Lambda handlers and Terraform against ADR standards
    - README Freshness: Checks if README needs updating based on changes
    - Test Coverage: Ensures new handlers have corresponding tests
    - Breaking Changes: Detects API/interface breaking changes
    - Architectural Duplication: Identifies code that violates DRY principles
  - Queue check jobs to SQS FIFO queue for async processing
  - Post initial comment to PR with analysis status
- **Output:** SQS messages for each check + GitHub PR comment

**Workflow:**
```
GitHub Webhook/Manual → analyze-pr → [Fetch PR Diff] → [Determine Checks] → SQS Jobs → GitHub Comment
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
