# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Analytics Aggregator Lambda
# DynamoDB Streams consumer for audit-logs table. Produces daily roll-ups
# in the analytics table so the dashboard can answer queries with bounded
# read volume regardless of how much raw audit data is generated.
# ============================================================================

module "analytics_aggregator_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-analytics-aggregator"
  description   = "Aggregate audit-log events into daily analytics roll-ups"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  source_path = "${path.module}/../lambda/analytics-aggregator"

  quiet_archive_local_exec = true

  environment_variables = {
    ENV             = var.environment
    APP_NAME        = var.app_name
    ANALYTICS_TABLE = module.analytics_table.dynamodb_table_id
  }

  attach_policy_statements = true
  policy_statements = {
    # Write aggregates
    dynamodb_analytics_write = {
      effect = "Allow"
      actions = [
        "dynamodb:UpdateItem",
        "dynamodb:PutItem",
        "dynamodb:GetItem"
      ]
      resources = [
        module.analytics_table.dynamodb_table_arn
      ]
    }

    # Read the audit stream
    dynamodb_stream = {
      effect = "Allow"
      actions = [
        "dynamodb:GetRecords",
        "dynamodb:GetShardIterator",
        "dynamodb:DescribeStream",
        "dynamodb:ListStreams"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_stream_arn
      ]
    }
  }

  tags = {
    Purpose = "analytics-aggregator"
  }
}

resource "aws_lambda_event_source_mapping" "analytics_aggregator_stream" {
  event_source_arn  = module.audit_logs_table.dynamodb_table_stream_arn
  function_name     = module.analytics_aggregator_lambda.lambda_function_name
  starting_position = "LATEST"
  batch_size        = 25
  enabled           = true

  # Keep retries bounded so a poisonous record cannot block aggregation
  # indefinitely.
  maximum_retry_attempts        = 3
  maximum_record_age_in_seconds = 3600

  # chunk 10 (HA parity): the handler returns batchItemFailures with the
  # failed record's eventID; ReportBatchItemFailures tells the stream to
  # redeliver only that record instead of dropping it on the 200. Without
  # this the retry bounds above are meaningless (whole batch succeeds).
  function_response_types = ["ReportBatchItemFailures"]

  # Only INSERTs of items that have a workspace_id. System-level calls
  # (workspace_id="SYSTEM" or missing) are filtered out at the source.
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            workspace_id = {
              S = [{ "anything-but" = ["", "SYSTEM"] }]
            }
          }
        }
      })
    }
  }
}

output "analytics_aggregator_lambda_arn" {
  description = "ARN of the analytics-aggregator Lambda function"
  value       = module.analytics_aggregator_lambda.lambda_function_arn
}

output "analytics_aggregator_lambda_name" {
  description = "Name of the analytics-aggregator Lambda function"
  value       = module.analytics_aggregator_lambda.lambda_function_name
}
