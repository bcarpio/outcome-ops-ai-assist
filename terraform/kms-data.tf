# KMS Data-at-Rest Key
#
# Resources:
# - aws_kms_key "data_at_rest" (CMK for S3, DynamoDB, SQS, SNS, ECR encryption)
# - aws_kms_alias "data_at_rest" (Key alias)
# - local "data_cmk_arn" (Resolved ARN, null when CMK disabled)
#
# Conditional on enable_cmk_encryption. Shared key for all
# data-at-rest encryption. Includes S3 Vectors indexing service grant.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
