# Handle Command

**Enterprise Component**

## Overview

The `handle-command` Lambda processes commands issued via PR comments (e.g., `outcomeops: fix readme`). It enables teams to interact with OutcomeOps directly from pull request discussions.

This Lambda validates licenses, parses commands from comment text, dispatches to appropriate handlers, tracks usage for analytics, and posts results as PR comments.

## Architecture

- **Input:** GitHub Actions workflow invocation with command details (command, pr_number, repo, commenter, branch)
- **Process:**
  - Validate OutcomeOps license
  - Parse command from PR comment text
  - Dispatch to appropriate command handler:
    - `outcomeops: help` - Display available commands
    - `outcomeops: fix readme` - Update README based on PR changes
    - `outcomeops: fix tests` - Generate or update tests for changed code
    - `outcomeops: fix adr` - Update ADR documentation based on architectural changes
    - `outcomeops: fix license` - Fix license headers and compliance issues
    - `outcomeops: regenerate` - Regenerate the entire PR (code, tests, docs)
  - Track command execution for analytics
  - Post result as PR comment
- **Output:** GitHub PR comment with command result

**Workflow:**
```
GitHub issue_comment → GitHub Actions → handle-command → [Validate License] → [Execute Command] → GitHub Comment
```

## Enterprise Features

- Air-gapped deployment (no external dependencies)
- Custom LLM integration (Azure OpenAI, AWS Bedrock, on-prem models)
- License validation and usage tracking
- Compliance audit logging
- Extensible command framework

## Configuration

The enterprise deployment uses SSM Parameter Store for configuration:
- LLM endpoints and credentials
- GitHub integration tokens
- License server connection details

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

## Support

For enterprise briefings: https://www.outcomeops.ai

For technical questions: https://www.outcomeops.ai/contact
