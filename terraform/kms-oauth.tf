# KMS OAuth Token Key
#
# Resources:
# - aws_kms_key "oauth_tokens" (KMS key for OAuth token encryption)
# - aws_kms_alias "oauth_tokens" (Key alias)
# - aws_ssm_parameter "oauth_kms_key_arn" (SSM parameter for key ARN)
#
# Conditional on enable_workspaces. Encrypts OAuth access/refresh
# tokens stored in DynamoDB for Confluence, Jira, Outlook, etc.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
