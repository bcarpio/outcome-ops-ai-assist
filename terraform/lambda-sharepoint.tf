# SharePoint Integration Lambdas
#
# Resources:
# - module "sharepoint_integration_lambda" (Microsoft OAuth flow and connection management)
# - aws_lambda_function_url "sharepoint_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "sharepoint_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "sharepoint_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "sharepoint_sync_dlq" (Dead letter queue)
# - module "sharepoint_sync_lambda" (Sync SharePoint files to knowledge base)
# - aws_lambda_event_source_mapping "sharepoint_sync" (SQS trigger, max concurrency 5)
# - module "sharepoint_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "sharepoint_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "microsoft_sharepoint_callback_url" (OAuth callback URL)
#
# Conditional on enable_workspaces. Shares Microsoft OAuth app with Outlook/Teams.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
