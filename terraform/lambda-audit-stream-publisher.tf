# ============================================================================
# Audit Stream Publisher Lambda
#
# DynamoDB Streams consumer for the audit-logs table that re-emits every
# INSERT as an OCSF v1.3.0 envelope to the audit-export Kinesis stream
# (kinesis-audit-export.tf). Customers' SIEM consumers read from Kinesis;
# we don't prescribe a downstream pattern.
#
# Gated on the same audit_stream_export_enabled tfvar that gates the
# Kinesis stream itself - never publish without a target.
# ============================================================================

module "audit_stream_publisher_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = local.audit_export_enabled

  function_name = "${var.environment}-${var.app_name}-audit-stream-publisher"
  description   = "Re-emit audit-log INSERTs to Kinesis as OCSF envelopes"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  source_path = [
    {
      path             = "${path.module}/../lambda/audit-stream-publisher"
      pip_requirements = true
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    },
  ]

  quiet_archive_local_exec = true

  environment_variables = {
    ENV                      = var.environment
    APP_NAME                 = var.app_name
    AUDIT_EXPORT_STREAM_NAME = local.audit_export_stream_name
  }

  attach_policy_statements = true
  policy_statements = merge(
    {
      # Read audit DDB stream
      dynamodb_stream = {
        effect = "Allow"
        actions = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams",
        ]
        resources = [
          module.audit_logs_table.dynamodb_table_stream_arn,
        ]
      }
      # Write to Kinesis
      kinesis_put = {
        effect = "Allow"
        actions = [
          "kinesis:PutRecord",
          "kinesis:PutRecords",
          "kinesis:DescribeStream",
        ]
        resources = [
          module.audit_export_stream[0].stream_arn,
        ]
      }
    },
    var.enable_cmk_encryption ? {
      # Kinesis CMK encrypt path needs GenerateDataKey on the platform CMK
      kms_use = {
        effect = "Allow"
        actions = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        resources = [local.data_cmk_arn]
      }
    } : {},
  )

  tags = {
    Purpose = "audit-stream-publisher"
  }
}

resource "aws_lambda_event_source_mapping" "audit_stream_publisher" {
  count = local.audit_export_enabled

  event_source_arn  = module.audit_logs_table.dynamodb_table_stream_arn
  function_name     = module.audit_stream_publisher_lambda[0].lambda_function_name
  starting_position = "LATEST"
  batch_size        = 25
  enabled           = true

  # Bound retries so a poisonous record doesn't block the SIEM forever.
  maximum_retry_attempts        = 3
  maximum_record_age_in_seconds = 3600

  # Report partial-batch failures so successfully-published records aren't
  # redelivered when one record fails. Matches what handler.py returns.
  function_response_types = ["ReportBatchItemFailures"]

  # INSERTs only - skip MODIFY / REMOVE noise.
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
      })
    }
  }
}
