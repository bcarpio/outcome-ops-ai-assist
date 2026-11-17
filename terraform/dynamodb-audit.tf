# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Audit Logs Table
# Stores full plaintext audit records for ALL Bedrock invocations and
# admin-action audit rows.
#
# Access patterns:
# - PK: WS#{workspace_id} / SK: {timestamp}#{request_id}     -> by workspace + time
# - GSI1PK: USER#{email}  / GSI1SK: {timestamp}#{request_id} -> by user + time
# - GSI2PK: CAT#{category} / GSI2SK: {timestamp}#{request_id} -> by category + time
#   (the audit dashboard's "all admin actions across the org" path; without
#   it, an org-wide query has to Scan + FilterExpression and the bedrock
#   rows clustered in each PK swamp the limit before any admin row matches)
# ============================================================================

module "audit_logs_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-audit-logs"
  hash_key  = "PK"
  range_key = "SK"

  # Prevent accidental deletion of audit data
  deletion_protection_enabled = var.enable_deletion_protection

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
    },
    {
      name = "GSI2PK"
      type = "S"
    },
    {
      name = "GSI2SK"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "GSI1"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    },
    {
      # Org-wide "all admin actions in date range" query path. PK is the
      # category discriminator so a single Query returns admin-only rows
      # newest-first without scanning bedrock clusters.
      name            = "GSI2"
      hash_key        = "GSI2PK"
      range_key       = "GSI2SK"
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

# ============================================================================
# Secondary-region copy (HA mode only).
# ============================================================================

module "audit_logs_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-audit-logs"
  hash_key  = "PK"
  range_key = "SK"

  deletion_protection_enabled = var.enable_deletion_protection

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
    },
    {
      name = "GSI2PK"
      type = "S"
    },
    {
      name = "GSI2SK"
      type = "S"
    }
  ]

  global_secondary_indexes = [
    {
      name            = "GSI1"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    },
    {
      name            = "GSI2"
      hash_key        = "GSI2PK"
      range_key       = "GSI2SK"
      projection_type = "ALL"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  stream_enabled   = true
  stream_view_type = "NEW_IMAGE"

  tags = {
    Purpose = "audit-logs"
  }
}

resource "aws_ssm_parameter" "audit_logs_table_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/audit-logs-table"
  type  = "String"
  value = module.audit_logs_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "audit-logs-table-name"
  }
}

resource "aws_ssm_parameter" "audit_logs_table_arn_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/audit-logs-table-arn"
  type  = "String"
  value = module.audit_logs_table_secondary[0].dynamodb_table_arn

  tags = {
    Purpose = "audit-logs-table-arn"
  }
}

output "audit_logs_table_arn_secondary" {
  description = "ARN of the secondary-region audit-logs DDB table (null when HA disabled)"
  value       = var.secondary_region != "" ? module.audit_logs_table_secondary[0].dynamodb_table_arn : null
}

output "audit_logs_table_stream_arn_secondary" {
  description = "ARN of the secondary-region audit-logs stream (null when HA disabled)"
  value       = var.secondary_region != "" ? module.audit_logs_table_secondary[0].dynamodb_table_stream_arn : null
}
