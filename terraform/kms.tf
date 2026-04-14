# KMS CloudWatch Logs Key
#
# Resources:
# - aws_kms_key "cloudwatch_logs" (KMS key for CloudWatch Logs encryption)
# - aws_kms_alias "cloudwatch_logs" (Key alias)
#
# Shared KMS key used by all Lambda log groups.
# Key rotation enabled, 7-day deletion window.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
