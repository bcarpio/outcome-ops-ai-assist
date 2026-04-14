# Snyk MCP Server
#
# Resources:
# - aws_ecr_repository "mcp_snyk" (ECR repo for Snyk MCP container image)
# - module "mcp_snyk" (Snyk MCP server via shared mcp-server module)
# - aws_dynamodb_table_item "mcp_snyk" (Auto-register Snyk in DynamoDB catalog)
# - local "mcp_snyk_token_ssm_path" (SSM path for Snyk token)
#
# Conditional on mcp_snyk_enabled. Wraps the Snyk CLI MCP server
# (stdio) via supergateway for HTTP transport.
# Custom container built from containers/mcp-snyk/Dockerfile.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
