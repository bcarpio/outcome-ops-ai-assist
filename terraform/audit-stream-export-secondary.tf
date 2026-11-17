# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Audit Stream Export — secondary region (HA)
# ----------------------------------------------------------------------------
# Mirror of kinesis-audit-export.tf + lambda-audit-stream-publisher.tf under
# aws.secondary. Closes the SIEM-export-under-HA gap that ADR-022's
# compute-plane addendum flagged as out-of-scope.
#
# Design (see ADR-022 "Audit-stream export under HA"):
#   - BOTH regions run an identical Kinesis stream + audit-stream-publisher.
#   - The publisher is gated on shared.active_region.is_active_region(): only
#     the ACTIVE region emits OCSF records. Both regions' audit-logs DDB
#     streams fire on every row (the active region on the original write, the
#     passive region when the replication-applier replays it), so without the
#     gate the SIEM would see each event twice. The gate makes emission
#     exactly-once: whichever region is active publishes; the other skips.
#   - The customer's SIEM consumer attaches to BOTH regional stream ARNs
#     continuously. Only the active region's stream carries data at any
#     instant, so the passive stream is simply empty — no duplicates, no
#     consumer-side failover action. Export follows the active region.
#
# Gated on audit_stream_export_enabled AND secondary_region != "" — both must
# hold, so single-region or export-disabled deploys create nothing here.
# ============================================================================

locals {
  audit_export_enabled_secondary = (
    var.audit_stream_export_enabled && var.secondary_region != ""
  ) ? 1 : 0
}

module "audit_export_stream_secondary" {
  source  = "cloudposse/kinesis-stream/aws"
  version = "0.4.0"

  count = local.audit_export_enabled_secondary

  providers = {
    aws = aws.secondary
  }

  # Same name as primary — Kinesis stream names are per-region, so the
  # publisher's AUDIT_EXPORT_STREAM_NAME resolves to the local-region stream
  # via its regional kinesis client. No env difference needed.
  name = local.audit_export_stream_name

  shard_count               = 0 # on-demand mode ignores shard_count
  retention_period          = 24
  enforce_consumer_deletion = false
  shard_level_metrics       = []
  stream_mode               = "ON_DEMAND"

  # CMK is regional: use the secondary-region data CMK when the platform CMK
  # toggle is on, else the secondary region's AWS-managed alias/aws/kinesis.
  encryption_type = "KMS"
  kms_key_id      = var.enable_cmk_encryption ? local.data_cmk_arn_secondary : "alias/aws/kinesis"

  tags = {
    Purpose = "audit-stream-export"
  }
}

resource "aws_ssm_parameter" "audit_export_stream_arn_secondary" {
  count = local.audit_export_enabled_secondary

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/audit-export/stream-arn"
  type  = "String"
  value = module.audit_export_stream_secondary[0].stream_arn
}

output "audit_export_stream_arn_secondary" {
  description = "ARN of the secondary-region audit-export Kinesis stream (null when HA / export disabled). Customer SIEM consumers attach to BOTH this and audit_export_stream_arn."
  value       = local.audit_export_enabled_secondary == 1 ? module.audit_export_stream_secondary[0].stream_arn : null
}

# ----------------------------------------------------------------------------
# Publisher Lambda (secondary). Identical handler to primary — it self-gates
# on is_active_region(), so it stays silent while this region is passive and
# starts emitting to the secondary stream after failover.
# ----------------------------------------------------------------------------

module "audit_stream_publisher_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = local.audit_export_enabled_secondary

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-audit-stream-publisher"
  # IAM roles are global per account; the primary publisher already owns the
  # un-suffixed role name, so the secondary needs its own.
  role_name   = "${var.environment}-${var.app_name}-audit-stream-publisher-secondary"
  description = "Re-emit audit-log INSERTs to Kinesis as OCSF envelopes (secondary region)"
  handler     = "handler.handler"
  runtime     = "python3.12"
  timeout     = 60
  memory_size = 256

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn

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

  environment_variables = merge(
    {
      ENV                      = var.environment
      APP_NAME                 = var.app_name
      AUDIT_EXPORT_STREAM_NAME = local.audit_export_stream_name
    },
    # AppConfig role-read env so is_active_region() evaluates THIS region's
    # role. Without it the publisher would treat itself as active (single-
    # region fast-path) and double-publish from the passive region.
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_stream = {
        effect = "Allow"
        actions = [
          "dynamodb:GetRecords",
          "dynamodb:GetShardIterator",
          "dynamodb:DescribeStream",
          "dynamodb:ListStreams",
        ]
        resources = [
          module.audit_logs_table_secondary[0].dynamodb_table_stream_arn,
        ]
      }
      kinesis_put = {
        effect = "Allow"
        actions = [
          "kinesis:PutRecord",
          "kinesis:PutRecords",
          "kinesis:DescribeStream",
        ]
        resources = [
          module.audit_export_stream_secondary[0].stream_arn,
        ]
      }
    },
    var.enable_cmk_encryption ? {
      kms_use = {
        effect = "Allow"
        actions = [
          "kms:Decrypt",
          "kms:GenerateDataKey",
        ]
        resources = [local.data_cmk_arn_secondary]
      }
    } : {},
    # appconfig:GetLatestConfiguration for the is_active_region() read.
    local.ha_scheduler_iam_secondary,
  )

  tags = {
    Purpose = "audit-stream-publisher"
  }
}

resource "aws_lambda_event_source_mapping" "audit_stream_publisher_secondary" {
  count = local.audit_export_enabled_secondary

  provider = aws.secondary

  event_source_arn  = module.audit_logs_table_secondary[0].dynamodb_table_stream_arn
  function_name     = module.audit_stream_publisher_lambda_secondary[0].lambda_function_name
  starting_position = "LATEST"
  batch_size        = 25
  enabled           = true

  maximum_retry_attempts        = 3
  maximum_record_age_in_seconds = 3600

  function_response_types = ["ReportBatchItemFailures"]

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
      })
    }
  }
}
