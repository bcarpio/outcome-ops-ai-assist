# ============================================================================
# Audit Stream Export - Kinesis Data Stream (SIEM hook)
#
# Optional Kinesis Data Stream that receives every audit-log INSERT as an
# OCSF v1.3.0 envelope. Customers' security teams attach their own consumer
# (Splunk, Datadog, Sumo, AWS Security Lake, or a Firehose to S3) at the
# stream. They choose which events they want via FilterCriteria on their
# consumer event source mapping; the publisher Lambda
# (lambda-audit-stream-publisher.tf) writes everything.
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

output "audit_export_stream_arn" {
  description = "ARN of the audit-export Kinesis stream (null when disabled)"
  value       = local.audit_export_enabled == 1 ? module.audit_export_stream[0].stream_arn : null
}

output "audit_export_stream_name" {
  description = "Name of the audit-export Kinesis stream (null when disabled)"
  value       = local.audit_export_enabled == 1 ? local.audit_export_stream_name : null
}
