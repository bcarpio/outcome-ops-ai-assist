# DynamoDB Chat Table
#
# Resources:
# - module "chat_table" (DynamoDB table for chat conversations and messages)
# - aws_ssm_parameter "chat_table_name" (SSM parameter for table name)
#
# Single-table design with GSIs for user conversations (GSI2) and
# workspace-scoped shared conversations (GSI3).
# PAY_PER_REQUEST billing, PITR enabled, KMS encryption.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
