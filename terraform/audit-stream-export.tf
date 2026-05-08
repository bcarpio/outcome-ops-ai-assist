# ============================================================================
# Audit Stream Export (SIEM hook)
#
# Optional Kinesis Data Stream + publisher Lambda that re-emits audit-log
# INSERT events as OCSF v1.3.0 envelopes. Customers' security teams attach
# their own consumer (Splunk, Datadog, Sumo, AWS Security Lake, or a
# Firehose to S3) at the stream. They choose which events they want via
# FilterCriteria on their consumer event source mapping; we publish all of
# them.
#
# Stream config: on-demand mode, 24h retention, CMK flows from
# enable_cmk_encryption when on. See docs/audit-stream-export.md.
# ============================================================================

locals {
  audit_export_enabled     = var.audit_stream_export_enabled ? 1 : 0
  audit_export_stream_name = "${var.environment}-${var.app_name}-audit-export"
}

module "audit_export_stream" {
  source  = "cloudposse/kinesis-stream/aws"
  version = "0.4.0"

  count = local.audit_export_enabled

  name = local.audit_export_stream_name

  shard_count               = 0 # on-demand mode ignores shard_count
  retention_period          = 24
  enforce_consumer_deletion = false
  shard_level_metrics       = []
  stream_mode               = "ON_DEMAND"

  # Always KMS-encrypted; choose CMK when the customer has enabled the
  # platform CMK toggle, fall back to AWS-managed alias/aws/kinesis otherwise.
  encryption_type = "KMS"
  kms_key_id      = var.enable_cmk_encryption ? local.data_cmk_arn : "alias/aws/kinesis"

  tags = {
    Purpose = "audit-stream-export"
  }
}

resource "aws_ssm_parameter" "audit_export_stream_arn" {
  count = local.audit_export_enabled

  name  = "/${var.environment}/${var.app_name}/audit-export/stream-arn"
  type  = "String"
  value = module.audit_export_stream[0].stream_arn
}

# ----------------------------------------------------------------------------
# Publisher Lambda (DynamoDB Streams -> OCSF transform -> Kinesis)
# ----------------------------------------------------------------------------

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

  # Same INSERT + workspace_id filter the analytics-aggregator uses, so
  # SYSTEM-level rows that don't apply to a workspace stay out of the SIEM
  # feed.
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
      })
    }
  }
}

output "audit_export_stream_arn" {
  description = "ARN of the audit-export Kinesis stream (null when disabled)"
  value       = local.audit_export_enabled == 1 ? module.audit_export_stream[0].stream_arn : null
}

output "audit_export_stream_name" {
  description = "Name of the audit-export Kinesis stream (null when disabled)"
  value       = local.audit_export_enabled == 1 ? local.audit_export_stream_name : null
}
