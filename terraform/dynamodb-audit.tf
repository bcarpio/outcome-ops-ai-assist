# ============================================================================
# DynamoDB Audit Logs Table
# Stores full plaintext audit records for ALL Bedrock invocations.
# Enterprise customers get full visibility into AI interactions.
#
# Access patterns:
# - PK: WS#{workspace_id} / SK: {timestamp}#{request_id}  -> by workspace + time
# - GSI1PK: USER#{email} / GSI1SK: {timestamp}#{request_id} -> by user + time
# ============================================================================

module "audit_logs_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-audit-logs"
  hash_key  = "PK"
  range_key = "SK"

  # Prevent accidental deletion of audit data
  deletion_protection_enabled = false

  attributes = [
    {
      name = "PK"
      type = "S"
    },
    {
      name = "SK"
      type = "S"
    },
    {
      name = "GSI1PK"
      type = "S"
    },
    {
      name = "GSI1SK"
      type = "S"
    }
  ]

  # GSI for querying by user email
  global_secondary_indexes = [
    {
      name            = "GSI1"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    }
  ]

  # On-demand billing for variable traffic patterns
  billing_mode = "PAY_PER_REQUEST"

  # TTL for automatic cleanup (365 days)
  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  # Point-in-time recovery for data protection
  point_in_time_recovery_enabled = true

  # Server-side encryption (CMK when enabled, AWS-managed otherwise)
  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  # Stream for audit-alert Lambda (refusal detection)
  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  tags = {
    Purpose = "audit-logs"
  }
}

resource "aws_ssm_parameter" "audit_logs_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/audit-logs-table"
  type  = "String"
  value = module.audit_logs_table.dynamodb_table_id

  tags = {
    Purpose = "audit-logs-table-name"
  }
}

resource "aws_ssm_parameter" "audit_logs_table_arn" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/audit-logs-table-arn"
  type  = "String"
  value = module.audit_logs_table.dynamodb_table_arn

  tags = {
    Purpose = "audit-logs-table-arn"
  }
}

output "audit_logs_table_name" {
  description = "Name of the audit logs DynamoDB table"
  value       = module.audit_logs_table.dynamodb_table_id
}

output "audit_logs_table_arn" {
  description = "ARN of the audit logs DynamoDB table"
  value       = module.audit_logs_table.dynamodb_table_arn
}

output "audit_logs_table_stream_arn" {
  description = "ARN of the audit logs DynamoDB table stream"
  value       = module.audit_logs_table.dynamodb_table_stream_arn
}
