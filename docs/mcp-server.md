# OutcomeOps MCP Server

The OutcomeOps MCP server connects Claude Code, VS Code, Cursor, and other MCP-compatible tools to your organization's knowledge base. It acts as a gateway that automatically discovers all MCP servers available to your workspaces and exposes their tools alongside native knowledge base tools through a single connection and API key.

## Key Features

- Works with Claude Code, VS Code, Cursor, and any MCP client supporting Streamable HTTP transport
- Native tools for querying the knowledge base, listing workspaces, and retrieving coding standards
- Automatic proxying of workspace-scoped MCP server tools (e.g., SonarQube, Snyk)
- API key authentication with SHA-256 hashed storage and per-user key scoping
- Deployed as a Fargate container with ALB routing and Cloud Map service discovery
- Cross-workspace tool sharing for collaborative environments
- Self-service API key management with expiration and revocation via the web UI

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
