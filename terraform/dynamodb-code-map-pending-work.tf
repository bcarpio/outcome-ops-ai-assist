# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Table for Per-Pipeline Pending-Work Ledger (plan-git-truth chunk 01)
#
# Tracks expected vs. completed SQS messages per
# (workspace, repo, pipeline, target_sha). The dispatcher writes one row
# before publishing SQS; consumers ADD to completed_count / failed_count
# as they finish work. When the batch closes, the consumer that closed it
# advances the per-pipeline SHA column on the workspaces row.
#
# Pipeline values: "code_map" | "docs" | "gkg".
# SK encodes the target SHA so two overlapping commits get two ledger rows.
# TTL: 7 days from dispatched_at (prevents unbounded ledger growth if a
# pipeline wedges; the SHA does not auto-advance — see the operator
# runbook entry for the wedge recovery path).
# ============================================================================

module "code_map_pending_work_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-code-map-pending-work"
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
  ]

  # No GSIs. Operators investigating a stuck batch scan by PK prefix;
  # query volume is rare enough that scan cost is fine. If chunk 02
  # surfaces a need to list "all pending batches for a workspace,"
  # add a GSI then — don't speculate now.

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "sha-truth-pending-work"
  }
}

resource "aws_ssm_parameter" "code_map_pending_work_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/code-map-pending-work-table"
  type  = "String"
  value = module.code_map_pending_work_table.dynamodb_table_id

  tags = {
    Purpose = "sha-truth-pending-work"
  }
}

output "code_map_pending_work_table_name" {
  description = "Name of the SHA-truth pending-work ledger DynamoDB table"
  value       = module.code_map_pending_work_table.dynamodb_table_id
}

output "code_map_pending_work_table_arn" {
  description = "ARN of the SHA-truth pending-work ledger DynamoDB table"
  value       = module.code_map_pending_work_table.dynamodb_table_arn
}

# ============================================================================
# Secondary-region copy (HA mode only).
# ============================================================================

module "code_map_pending_work_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-code-map-pending-work"
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
  ]

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  tags = {
    Purpose = "sha-truth-pending-work"
  }
}

resource "aws_ssm_parameter" "code_map_pending_work_table_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/code-map-pending-work-table"
  type  = "String"
  value = module.code_map_pending_work_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "sha-truth-pending-work"
  }
}

output "code_map_pending_work_table_arn_secondary" {
  description = "ARN of the secondary-region SHA-truth pending-work DDB table (null when HA disabled)"
  value       = var.secondary_region != "" ? module.code_map_pending_work_table_secondary[0].dynamodb_table_arn : null
}
