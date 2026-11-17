# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Tables
# NOTE: Deletion protection on every table is driven by
# var.enable_deletion_protection (default true). Ephemeral sandbox/PoC
# deploys set it false so terraform destroy can complete; durable
# environments leave it on, which blocks deletion from the console and
# CLI as well as from Terraform.
#
# With S3 Vectors migration, this table is now used for STATE TRACKING ONLY:
# - Commit SHAs for incremental processing (ingest-docs, generate-code-maps)
# - Processing state and timestamps
# Vector embeddings and document content are now stored in S3 Vectors.
# ============================================================================

module "code_maps_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-code-maps"
  hash_key  = "PK"
  range_key = "SK"

  # Prevent accidental deletion of production data
  deletion_protection_enabled = var.enable_deletion_protection

  # Only PK and SK needed for state tracking
  # GSIs removed - vector queries now use S3 Vectors
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

  # No GSIs needed - state tracking uses direct key lookups
  global_secondary_indexes = []

  # On-demand billing for variable traffic patterns
  billing_mode = "PAY_PER_REQUEST"

  # Point-in-time recovery for data protection
  point_in_time_recovery_enabled = true

  # Server-side encryption (CMK when enabled, AWS-managed otherwise)
  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  # Stream specification for change data capture
  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  tags = {
    Purpose = "state-tracking"
  }
}

resource "aws_ssm_parameter" "code_maps_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/code-maps-table"
  type  = "String"
  value = module.code_maps_table.dynamodb_table_id

  tags = {
    Purpose = "code-maps-table-name"
  }
}

resource "aws_ssm_parameter" "code_maps_table_arn" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/code-maps-table-arn"
  type  = "String"
  value = module.code_maps_table.dynamodb_table_arn

  tags = {
    Purpose = "code-maps-table-arn"
  }
}

output "code_maps_table_name" {
  description = "Name of the code maps DynamoDB table"
  value       = module.code_maps_table.dynamodb_table_id
}

output "code_maps_table_arn" {
  description = "ARN of the code maps DynamoDB table"
  value       = module.code_maps_table.dynamodb_table_arn
}

output "code_maps_table_stream_arn" {
  description = "ARN of the code maps DynamoDB table stream"
  value       = module.code_maps_table.dynamodb_table_stream_arn
}

# ============================================================================
# Secondary-region copy (HA mode only).
# ============================================================================

module "code_maps_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-code-maps"
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

  global_secondary_indexes = []

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  stream_enabled   = true
  stream_view_type = "NEW_AND_OLD_IMAGES"

  tags = {
    Purpose = "state-tracking"
  }
}

resource "aws_ssm_parameter" "code_maps_table_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/code-maps-table"
  type  = "String"
  value = module.code_maps_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "code-maps-table-name"
  }
}

resource "aws_ssm_parameter" "code_maps_table_arn_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/code-maps-table-arn"
  type  = "String"
  value = module.code_maps_table_secondary[0].dynamodb_table_arn

  tags = {
    Purpose = "code-maps-table-arn"
  }
}

output "code_maps_table_arn_secondary" {
  description = "ARN of the secondary-region code-maps DDB table (null when HA disabled)"
  value       = var.secondary_region != "" ? module.code_maps_table_secondary[0].dynamodb_table_arn : null
}

output "code_maps_table_stream_arn_secondary" {
  description = "ARN of the secondary-region code-maps stream (null when HA disabled)"
  value       = var.secondary_region != "" ? module.code_maps_table_secondary[0].dynamodb_table_stream_arn : null
}
