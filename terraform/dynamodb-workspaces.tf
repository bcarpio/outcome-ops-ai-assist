# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Table for Workspaces
# Stores workspace metadata, membership, repos, and OAuth connections
# ============================================================================

module "workspaces_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.enable_workspaces ? 1 : 0

  name      = "${var.environment}-${var.app_name}-workspaces"
  hash_key  = "PK"
  range_key = "SK"

  # Single-tenant, customer-deployed - less critical than SaaS
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
    # sparse GSI index attribute. Populated only on
    # METADATA rows where is_global=true (value "1"). METADATA rows without
    # this attribute don't appear in the GSI — that's the sparse-index
    # property. Enterprise-scale wins: reading "which workspaces are global"
    # becomes a small Query, not a full-table Scan filter over 2000+ rows.
    {
      name = "is_global_flag"
      type = "S"
    }
  ]

  # GSI1 for workspace membership queries
  # Allows: "List all members of workspace X"
  # PK: WS#{workspace_id}, SK: USER#{user_email}
  global_secondary_indexes = [
    {
      name            = "GSI1"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    },
    # sparse index for is_global lookup.
    # Query with is_global_flag = "1" returns every global-workspace METADATA
    # row. Regular workspaces never write this attribute so they never appear
    # in the index — no hot-partition concern until we cross low-hundreds of
    # global workspaces. Projection = ALL because METADATA rows are small
    # and callers need the full row (workspace_id, name, etc.).
    {
      name            = "GSI_is_global"
      hash_key        = "is_global_flag"
      projection_type = "ALL"
    }
  ]

  # On-demand billing for variable traffic patterns
  billing_mode = "PAY_PER_REQUEST"

  # Point-in-time recovery for data protection
  point_in_time_recovery_enabled = true

  # Server-side encryption (CMK when enabled, AWS-managed otherwise)
  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "workspaces"
  }
}

resource "aws_ssm_parameter" "workspaces_table_name" {
  count = var.enable_workspaces ? 1 : 0

  name  = "/${var.environment}/${var.app_name}/dynamodb/workspaces-table"
  type  = "String"
  value = module.workspaces_table[0].dynamodb_table_id

  tags = {
    Purpose = "workspaces-table-name"
  }
}

# ============================================================================
# Bootstrap Initial Org Admin
# Creates the first org_admin so they can add other users
# ============================================================================

resource "aws_dynamodb_table_item" "initial_org_admin" {
  count = var.enable_workspaces && var.initial_org_admin_email != "" ? 1 : 0

  table_name = module.workspaces_table[0].dynamodb_table_id
  hash_key   = "PK"
  range_key  = "SK"

  item = jsonencode({
    "PK"         = { "S" = "USER#${var.initial_org_admin_email}" }
    "SK"         = { "S" = "ORG_MEMBER" }
    "GSI1PK"     = { "S" = "ORG#default" }
    "GSI1SK"     = { "S" = "USER#${var.initial_org_admin_email}" }
    "user_email" = { "S" = var.initial_org_admin_email }
    "org_role"   = { "S" = "org_admin" }
    "joined_at"  = { "S" = timestamp() }
  })

  lifecycle {
    # Don't update if the item already exists (user may have changed their role)
    ignore_changes = [item]
  }
}

output "workspaces_table_name" {
  description = "Name of the workspaces DynamoDB table"
  value       = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_id : null
}

output "workspaces_table_arn" {
  description = "ARN of the workspaces DynamoDB table"
  value       = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_arn : null
}

output "initial_org_admin_email" {
  description = "Email of the initial org admin (if configured)"
  value       = var.enable_workspaces && var.initial_org_admin_email != "" ? var.initial_org_admin_email : null
}

# ============================================================================
# Secondary-region copy (HA mode only).
# Identical schema, same name, secondary KMS, secondary AWS provider.
# Initial-org-admin bootstrap item is NOT replicated here — the dual-write
# helper will copy the row over via EventBridge on first
# write, so we don't risk diverging the initial bootstrap state.
# ============================================================================

module "workspaces_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-workspaces"
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
    # (HA parity): sparse is_global GSI attribute.
    {
      name = "is_global_flag"
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
    # (HA parity).
    {
      name            = "GSI_is_global"
      hash_key        = "is_global_flag"
      projection_type = "ALL"
    }
  ]

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  tags = {
    Purpose = "workspaces"
  }
}

resource "aws_ssm_parameter" "workspaces_table_name_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/workspaces-table"
  type  = "String"
  value = module.workspaces_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "workspaces-table-name"
  }
}

output "workspaces_table_arn_secondary" {
  description = "ARN of the secondary-region workspaces DDB table (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? module.workspaces_table_secondary[0].dynamodb_table_arn : null
}
