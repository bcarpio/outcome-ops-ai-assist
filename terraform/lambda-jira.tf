# Jira Integration Lambdas
#
# Resources:
# - module "jira_integration_lambda" (OAuth flow and connection management)
# - aws_lambda_function_url "jira_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "jira_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "jira_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "jira_sync_dlq" (Dead letter queue)
# - module "jira_sync_lambda" (Sync Jira issues to vector store)
# - aws_lambda_event_source_mapping "jira_sync" (SQS trigger, max concurrency 5)
# - module "jira_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "jira_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "atlassian_jira_callback_url" (Jira OAuth callback URL)
#
# Conditional on enable_workspaces. Shares Atlassian OAuth app with Confluence.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
