# DynamoDB Audit Logs Table
#
# Resources:
# - module "audit_logs_table" (DynamoDB table for Bedrock invocation audit logs)
# - aws_ssm_parameter "audit_logs_table_name" (SSM parameter for table name)
# - aws_ssm_parameter "audit_logs_table_arn" (SSM parameter for table ARN)
#
# Stores plaintext audit records for all AI interactions.
# GSI1 for querying by user email. TTL for 365-day auto-cleanup.
# DynamoDB Streams (NEW_IMAGE) for audit-alert Lambda integration.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
