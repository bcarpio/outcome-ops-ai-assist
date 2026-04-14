# Teams Integration Lambdas
#
# Resources:
# - module "teams_integration_lambda" (Microsoft OAuth flow and connection management)
# - aws_lambda_function_url "teams_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "teams_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "teams_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "teams_sync_dlq" (Dead letter queue)
# - module "teams_sync_lambda" (Sync Teams messages to knowledge base)
# - aws_lambda_event_source_mapping "teams_sync" (SQS trigger, max concurrency 5)
# - module "teams_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "teams_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "microsoft_teams_callback_url" (OAuth callback URL)
#
# Conditional on enable_workspaces. Shares Microsoft OAuth app with Outlook/SharePoint.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
