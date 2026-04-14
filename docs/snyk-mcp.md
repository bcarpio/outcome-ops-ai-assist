# Snyk MCP Server

OutcomeOps integrates with Snyk via the Model Context Protocol (MCP), giving the AI assistant direct access to security vulnerability data during chat conversations. The integration uses a custom MCP server that queries the Snyk REST API to retrieve scan results from projects already monitored in Snyk, enabling users to ask natural language questions about their security posture.

## Key Features

- Real-time Snyk vulnerability queries during AI chat conversations
- Tools for listing organizations, projects, issues, dependencies, and SBOMs
- Severity and scan-type filtering for targeted vulnerability analysis
- Deployed as a lightweight Fargate Spot container (~$6-9/month) in private subnets
- API token stored securely in SSM Parameter Store with KMS encryption
- Workspace-scoped or global access control managed through the OutcomeOps UI
- Dynamic tool manifest discovery with automatic refresh

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
