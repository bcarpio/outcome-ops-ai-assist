# GitHub App Integration Lambdas
#
# Resources:
# - module "github_integration_lambda" (OAuth flow and connection management)
# - aws_lambda_function_url "github_integration" (Function URL with IAM auth)
# - aws_ssm_parameter "github_integration_url" (SSM parameter for URL)
# - aws_sqs_queue "github_sync" (Sync job queue with DLQ)
# - aws_sqs_queue "github_sync_dlq" (Dead letter queue)
# - module "github_sync_lambda" (Sync GitHub repos for code-map generation)
# - aws_lambda_event_source_mapping "github_sync" (SQS trigger, max concurrency 5)
# - module "github_scheduler_lambda" (Hourly scheduler for sync fan-out)
# - aws_cloudwatch_event_rule "github_hourly_sync" (EventBridge hourly trigger)
# - aws_ssm_parameter "github_app_id" (GitHub App ID)
# - aws_ssm_parameter "github_private_key" (GitHub App private key, SecureString)
# - aws_ssm_parameter "github_client_id" (OAuth client ID)
# - aws_ssm_parameter "github_client_secret" (OAuth client secret, SecureString)
# - aws_ssm_parameter "github_webhook_secret" (Webhook secret, SecureString)
# - aws_ssm_parameter "github_callback_url" (OAuth callback URL)
# - aws_ssm_parameter "github_app_slug" (GitHub App URL slug)
#
# Conditional on enable_workspaces.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
