# Lambda: Handle Command

The handle-command Lambda processes bot commands issued via PR comments (e.g., `outcomeops: fix readme`). It validates the customer's license, parses the command, dispatches to the appropriate handler, tracks usage for analytics, and posts results back as PR comments.

## Key Features

- Processes PR comment commands triggered via GitHub Actions workflows
- Supports commands for fixing READMEs, tests, ADRs, license headers, and full PR regeneration
- Validates customer license before executing any command
- Tracks command usage for analytics (commands are unlimited, not subject to limits)
- Case-insensitive command parsing with flexible formatting
- Integrates with GitHub API to post results as PR comments
- Uses OIDC-based GitHub Actions authentication for secure Lambda invocation

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
