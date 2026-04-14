# Confluence Integration Lambdas
#
# Resources:
# - module "confluence_integration_lambda" (OAuth flow and connection management)
# - aws_lambda_function_url "confluence_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "confluence_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "confluence_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "confluence_sync_dlq" (Dead letter queue)
# - module "confluence_sync_lambda" (Sync Confluence pages to vector store)
# - aws_lambda_event_source_mapping "confluence_sync" (SQS trigger, max concurrency 5)
# - module "confluence_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "confluence_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "atlassian_client_id" (OAuth client ID placeholder)
# - aws_ssm_parameter "atlassian_client_secret" (OAuth client secret placeholder)
# - aws_ssm_parameter "atlassian_callback_url" (OAuth callback URL)
#
# Conditional on enable_workspaces. Full OAuth lifecycle with KMS-encrypted tokens.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
