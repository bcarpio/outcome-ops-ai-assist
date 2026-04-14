# Jira Integration Setup

The Jira integration enables code generation directly from Jira issues. By adding labels like `approved-for-plan` or `approved-for-generation` to a Jira issue, a Jira Automation rule triggers an AWS SSM Document that invokes the code generation pipeline, ultimately producing a GitHub PR with the implementation plan or full code, tests, and documentation.

## Key Features

- Label-driven workflow: `approved-for-plan` for implementation plans, `approved-for-generation` for full code generation
- Jira Automation rules trigger AWS SSM Documents via a cross-account IAM role
- Component-based repository targeting using exact GitHub repository paths
- Two-phase Terraform deployment with Jira External ID trust policy
- Supports plan-only review before committing to full code generation
- End-to-end flow from Jira issue to GitHub pull request
- Works with any Jira project that supports Automation rules

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
