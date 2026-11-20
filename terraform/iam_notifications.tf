# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Shared IAM locals for workspace notifications.
#
# Merged into the policy_statements of the Lambdas that touch the
# notifications table (dynamodb-notifications.tf), in both regions:
#   - github-sync           writes a row when a repo enters `stalled`, and
#                           Queries the failed-files table to list the
#                           files that failed for the stalled commit.
#   - workspace-management  lists (Query), reads (GetItem) and dismisses
#                           (DeleteItem) notification rows.
#
# Resource ARNs are exact (ADR-001, no wildcards). Defined once here so the
# primary and secondary modules merge matching locals (the HA parity check
# pairs `local.<name>` with `local.<name>_secondary`) and so the two
# oversized secondary files only grow by one merge line each.
#
# Both modules are gated on var.enable_workspaces, as is the table, so the
# empty-map fallbacks below only exist to keep the locals evaluable when a
# deploy has workspaces (or HA) turned off.
# ============================================================================

locals {
  github_sync_notifications_iam = var.enable_workspaces ? {
    notifications_write = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [module.notifications_table[0].dynamodb_table_arn]
    }
    failed_files_query = {
      effect    = "Allow"
      actions   = ["dynamodb:Query"]
      resources = [module.code_map_failed_files_table.dynamodb_table_arn]
    }
  } : {}

  github_sync_notifications_iam_secondary = var.enable_workspaces && var.secondary_region != "" ? {
    notifications_write = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [module.notifications_table_secondary[0].dynamodb_table_arn]
    }
    failed_files_query = {
      effect    = "Allow"
      actions   = ["dynamodb:Query"]
      resources = [module.code_map_failed_files_table_secondary[0].dynamodb_table_arn]
    }
  } : {}

  workspace_mgmt_notifications_iam = var.enable_workspaces ? {
    notifications_read_dismiss = {
      effect    = "Allow"
      actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:DeleteItem"]
      resources = [module.notifications_table[0].dynamodb_table_arn]
    }
  } : {}

  workspace_mgmt_notifications_iam_secondary = var.enable_workspaces && var.secondary_region != "" ? {
    notifications_read_dismiss = {
      effect    = "Allow"
      actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:DeleteItem"]
      resources = [module.notifications_table_secondary[0].dynamodb_table_arn]
    }
  } : {}
}
