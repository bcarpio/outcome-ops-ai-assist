# OutcomeOps MCP Server
#
# Resources:
# - aws_ecr_repository "mcp_outcomeops" (ECR repo for custom MCP container image)
# - module "mcp_outcomeops" (MCP server via shared mcp-server module)
# - aws_iam_role_policy "mcp_outcomeops" (Task role policy for DynamoDB, S3 Vectors, Bedrock, S3, SSM)
#
# Conditional on mcp_server_enabled. Exposes the knowledge base to
# external MCP clients (Claude Code, VS Code, Cursor).
# API key authentication, routed through ALB.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
