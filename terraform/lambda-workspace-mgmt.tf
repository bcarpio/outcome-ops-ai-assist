# Workspace Management Lambda
#
# Resources:
# - module "workspace_management_lambda" (Workspace CRUD, membership, repo management)
# - aws_lambda_function_url "workspace_management" (Function URL with IAM auth)
# - aws_ssm_parameter "workspace_management_url" (SSM parameter for URL)
#
# Conditional on enable_workspaces. Uses Lambda Web Adapter (LWA)
# for HTTP endpoints. Manages workspaces, members, repos, documents,
# MCP credentials, and system prompts. Integrates with S3, DynamoDB,
# SQS delete queue, and KMS for credential encryption.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
