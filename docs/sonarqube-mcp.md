# SonarQube MCP Server

OutcomeOps integrates with SonarQube via the Model Context Protocol (MCP), giving the AI assistant direct access to code quality and security analysis data during chat conversations. The integration uses the official SonarQube MCP server from SonarSource, deployed as a Fargate Spot container in your VPC, supporting both SonarCloud and self-hosted SonarQube instances.

## Key Features

- Real-time SonarQube code quality queries during AI chat conversations
- Tools for searching issues, viewing metrics, checking quality gates, and reviewing security hotspots
- Support for both SonarCloud and self-hosted SonarQube Server instances
- Deployed as a Fargate Spot container (~$6-9/month) in private subnets
- API token stored securely in SSM Parameter Store with KMS encryption
- Workspace-scoped or global access control managed through the OutcomeOps UI
- Extensible pattern for adding custom MCP servers using the same Terraform module

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
