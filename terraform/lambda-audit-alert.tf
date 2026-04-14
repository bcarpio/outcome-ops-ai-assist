# Audit Alert Lambda
#
# Resources:
# - module "audit_alert_lambda" (Send SNS alerts on AI refusal detection)
# - aws_lambda_event_source_mapping "audit_logs_stream" (DynamoDB Streams trigger)
#
# Triggered by DynamoDB Streams on the audit-logs table.
# Filters for INSERT events where status is "refusal".
# Sends notifications to the audit alerts SNS topic.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
