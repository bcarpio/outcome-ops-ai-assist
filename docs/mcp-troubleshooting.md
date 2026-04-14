# MCP Troubleshooting

This document tracks the investigation and resolution of connecting the OutcomeOps MCP server to Claude Code. The server works correctly via curl but initially failed to load tools in Claude Code due to configuration location issues.

## Key Features

- Documents the root cause: `~/.claude/mcp.json` is not read by Claude Code for HTTP MCP servers
- Provides the working solution: use `claude mcp add --transport http` CLI command
- Covers transport type differences between CLI (`http`), mcp.json (`streamable-http`), and VS Code (`streamableHttp`)
- Includes server verification commands for health checking
- Lists all MCP config file locations and their scopes
- Tracks what was tried and what did not work to prevent repeating failed approaches

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
