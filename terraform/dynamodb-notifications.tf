# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Table for Workspace Notifications
#
# Per-workspace notification rows shown to a workspace's admins (and to the
# person who added the repo a notification is about). Visibility is
# computed at read time, so there is one row per event, not one per user.
#
#   PK = WS#<workspace_id>
#   SK = NOTIF#<notification_id>   (deterministic id: repeats update one row)
#
# Writers: github-sync (repo entered `stalled`). Reader + dismissal:
# workspace-management (GET /notifications, DELETE .../notifications/{id}).
# Writes go through shared.dual_write, so the HA replication applier keeps
# the secondary copy in sync (see lambda-replication-applier.tf).
#
# TTL: 7 days via the `ttl` attribute (epoch seconds) set by the writer.
# ============================================================================

module "notifications_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.enable_workspaces ? 1 : 0

  name      = "${var.environment}-${var.app_name}-notifications"
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
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "notifications"
  }
}

resource "aws_ssm_parameter" "notifications_table_name" {
  count = var.enable_workspaces ? 1 : 0

  name  = "/${var.environment}/${var.app_name}/dynamodb/notifications-table"
  type  = "String"
  value = module.notifications_table[0].dynamodb_table_id

  tags = {
    Purpose = "notifications"
  }
}

output "notifications_table_name" {
  description = "Name of the workspace notifications DynamoDB table"
  value       = var.enable_workspaces ? module.notifications_table[0].dynamodb_table_id : null
}

output "notifications_table_arn" {
  description = "ARN of the workspace notifications DynamoDB table"
  value       = var.enable_workspaces ? module.notifications_table[0].dynamodb_table_arn : null
}

# ============================================================================
# Secondary-region copy (HA mode only).
# Identical schema, same name, secondary KMS, secondary AWS provider.
# ============================================================================

module "notifications_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-notifications"
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
    Purpose = "notifications"
  }
}

resource "aws_ssm_parameter" "notifications_table_name_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/notifications-table"
  type  = "String"
  value = module.notifications_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "notifications"
  }
}

output "notifications_table_arn_secondary" {
  description = "ARN of the secondary-region workspace notifications DDB table (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? module.notifications_table_secondary[0].dynamodb_table_arn : null
}
