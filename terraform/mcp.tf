# MCP Server Catalog
#
# Resources:
# - aws_service_discovery_private_dns_namespace "mcp" (Cloud Map namespace for MCP servers)
# - module "mcp_sonarqube" (SonarQube MCP server via shared mcp-server module)
# - aws_dynamodb_table_item "mcp_sonarqube" (Auto-register SonarQube in DynamoDB catalog)
# - local "any_mcp_enabled" (Toggle for shared MCP infrastructure)
# - local "mcp_alb_listener_arn" (ALB listener for MCP routing)
# - local "mcp_base_url" (Base URL for MCP servers through ALB)
# - local "mcp_sonarqube_token_ssm_path" (SSM path for SonarQube token)
#
# Conditional on mcp_sonarqube_enabled. Deploys MCP server containers
# as Fargate Spot tasks with ALB routing, bypassing OIDC.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
