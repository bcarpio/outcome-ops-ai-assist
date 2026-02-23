# ============================================================================
# Audit Alert Lambda
# Triggered by DynamoDB Streams on audit-logs table when refusals are detected.
# Sends SNS notification with audit metadata.
# ============================================================================

module "audit_alert_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-audit-alert"
  description   = "Send SNS alerts when AI refusals are detected in audit logs"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 128

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/audit-alert"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV           = var.environment
    APP_NAME      = var.app_name
    SNS_TOPIC_ARN = module.audit_alerts_sns.topic_arn
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Publish to SNS audit alerts topic
    sns_publish = {
      effect = "Allow"
      actions = [
        "sns:Publish"
      ]
      resources = [
        module.audit_alerts_sns.topic_arn
      ]
    }

    # DynamoDB Streams read (required for event source mapping)
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
    Purpose = "audit-alert"
  }
}

# DynamoDB Stream event source mapping with filter for refusals
resource "aws_lambda_event_source_mapping" "audit_logs_stream" {
  event_source_arn  = module.audit_logs_table.dynamodb_table_stream_arn
  function_name     = module.audit_alert_lambda.lambda_function_name
  starting_position = "LATEST"
  batch_size        = 10
  enabled           = true

  # Filter to only INSERT events where status is "refusal"
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            status = {
              S = ["refusal"]
            }
          }
        }
      })
    }
  }
}

output "audit_alert_lambda_arn" {
  description = "ARN of the audit-alert Lambda function"
  value       = module.audit_alert_lambda.lambda_function_arn
}

output "audit_alert_lambda_name" {
  description = "Name of the audit-alert Lambda function"
  value       = module.audit_alert_lambda.lambda_function_name
}

