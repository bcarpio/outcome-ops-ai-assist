# Outlook Integration Lambdas
#
# Resources:
# - module "outlook_integration_lambda" (Microsoft OAuth flow and connection management)
# - aws_lambda_function_url "outlook_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "outlook_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "outlook_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "outlook_sync_dlq" (Dead letter queue)
# - module "outlook_sync_lambda" (Sync Outlook emails to knowledge base)
# - aws_lambda_event_source_mapping "outlook_sync" (SQS trigger, max concurrency 5)
# - module "outlook_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "outlook_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "microsoft_client_id" (Microsoft OAuth client ID placeholder)
# - aws_ssm_parameter "microsoft_client_secret" (Microsoft OAuth client secret placeholder)
# - aws_ssm_parameter "microsoft_callback_url" (OAuth callback URL)
#
# Conditional on enable_workspaces. KMS-encrypted OAuth tokens.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
