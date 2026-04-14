# Lambda: Analyze PR

The analyze-pr Lambda orchestrates Pull Request analysis by fetching PR details from GitHub, detecting changed files, determining which checks are relevant, and queueing analysis jobs to SQS FIFO for async processing. It supports both AI-generated and human-created PRs.

## Key Features

- Automatic detection of relevant checks based on changed file patterns
- ADR compliance checks for Lambda handlers and Terraform files
- README freshness analysis when documentation-adjacent files change
- Test coverage verification for new or modified code
- Breaking change detection for API and infrastructure modifications
- Architectural duplication analysis across the codebase
- License compliance checking for dependency changes
- Status comment posting to PRs with analysis progress

---

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
