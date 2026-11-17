# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Analytics Aggregates Table
# Stores pre-aggregated daily roll-ups of audit records so the analytics
# dashboard can answer queries with a bounded number of reads, regardless
# of how much raw audit volume a customer generates.
#
# Items per workspace per day:
#   SK=DT#{date}                -> day totals
#   SK=DM#{date}#{model_id}     -> day x model breakdown
#   SK=DU#{date}#{user_email}   -> day x user breakdown
#
# Aggregates are produced by the analytics-aggregator Lambda, which consumes
# the audit-logs DynamoDB stream and uses atomic ADD updates so that late or
# retried events converge to the correct counts.
# ============================================================================

module "analytics_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-analytics"
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
    }
  ]

  billing_mode = "PAY_PER_REQUEST"

  # Retain aggregates a bit longer than raw audit logs (400 days) so
  # full-year trend comparisons stay intact even if audit raw data is
  # rotating out under its own 365-day TTL.
  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "analytics-aggregates"
  }
}

resource "aws_ssm_parameter" "analytics_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/analytics-table"
  type  = "String"
  value = module.analytics_table.dynamodb_table_id

  tags = {
    Purpose = "analytics-aggregates-table-name"
  }
}

output "analytics_table_name" {
  description = "Name of the analytics aggregates DynamoDB table"
  value       = module.analytics_table.dynamodb_table_id
}

output "analytics_table_arn" {
  description = "ARN of the analytics aggregates DynamoDB table"
  value       = module.analytics_table.dynamodb_table_arn
}

# ============================================================================
# Secondary-region copy (HA mode only).
# ============================================================================

module "analytics_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-analytics"
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
    }
  ]

  billing_mode = "PAY_PER_REQUEST"

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  tags = {
    Purpose = "analytics-aggregates"
  }
}

resource "aws_ssm_parameter" "analytics_table_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/analytics-table"
  type  = "String"
  value = module.analytics_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "analytics-aggregates-table-name"
  }
}

output "analytics_table_arn_secondary" {
  description = "ARN of the secondary-region analytics DDB table (null when HA disabled)"
  value       = var.secondary_region != "" ? module.analytics_table_secondary[0].dynamodb_table_arn : null
}
