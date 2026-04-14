# DynamoDB Workspaces Table
#
# Resources:
# - module "workspaces_table" (DynamoDB table for workspace metadata and membership)
# - aws_ssm_parameter "workspaces_table_name" (SSM parameter for table name)
# - aws_dynamodb_table_item "initial_org_admin" (Bootstrap first org admin)
#
# Conditional on enable_workspaces. Stores workspace metadata,
# membership, repos, and OAuth connections.
# GSI1 for workspace membership queries.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
