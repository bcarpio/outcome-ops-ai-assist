# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# DynamoDB Table for MCP Session Store ()
# ----------------------------------------------------------------------------
# Externalizes the MCP server's per-user session state (previously a module-
# global dict inside the container). Two consequences:
#   - Autoscaling the MCP Fargate service () becomes safe
#     — a second task doesn't lose or duplicate sessions.
#   - Container restarts (spot reclamation, deploys) no longer drop every
#     in-flight MCP session.
#
# Item shape:
#   PK (S)         session_id (raw hex for our own sessions;
#                  "upstream#<mcp_id>" for cached proxied-server sessions)
#   email (S)      authenticated user
#   last_active (N) unix seconds — application checks this against
#                   SESSION_TIMEOUT as authoritative expiry; TTL is
#                   defense-in-depth (DDB TTL is best-effort, up to 48h delay)
#   tool_routing (M) optional map for tools/list-derived routing table
#   expires_at (N) unix seconds; drives TTL eviction
#
# HA parity: same table shape in secondary region. Sessions are region-scoped
# by design (each region's MCP task pool reads its own table). Post-failover
# users re-establish sessions cleanly — no cross-region session drift.
# ============================================================================

module "mcp_sessions_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.mcp_server_enabled ? 1 : 0

  name     = "${var.environment}-${var.app_name}-mcp-sessions"
  hash_key = "PK"

  deletion_protection_enabled = var.enable_deletion_protection

  attributes = [
    {
      name = "PK"
      type = "S"
    },
  ]

  billing_mode = "PAY_PER_REQUEST"

  # Sessions are short-lived; PITR isn't useful for them, but keep server-side
  # encryption on with the platform CMK for consistency with other tables.
  point_in_time_recovery_enabled     = false
  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  ttl_enabled        = true
  ttl_attribute_name = "expires_at"

  tags = {
    Purpose = "mcp-session-store"
  }
}

resource "aws_ssm_parameter" "mcp_sessions_table_name" {
  count = var.mcp_server_enabled ? 1 : 0

  name  = "/${var.environment}/${var.app_name}/dynamodb/mcp-sessions-table"
  type  = "String"
  value = module.mcp_sessions_table[0].dynamodb_table_id

  tags = {
    Purpose = "mcp-sessions-table-name"
  }
}

output "mcp_sessions_table_name" {
  description = "Name of the MCP sessions DynamoDB table (null when MCP disabled)"
  value       = var.mcp_server_enabled ? module.mcp_sessions_table[0].dynamodb_table_id : null
}

output "mcp_sessions_table_arn" {
  description = "ARN of the MCP sessions DynamoDB table (null when MCP disabled)"
  value       = var.mcp_server_enabled ? module.mcp_sessions_table[0].dynamodb_table_arn : null
}

# ============================================================================
# Secondary-region copy (HA mode only) — identical schema.
# ============================================================================

module "mcp_sessions_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.mcp_server_enabled && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name     = "${var.environment}-${var.app_name}-mcp-sessions"
  hash_key = "PK"

  deletion_protection_enabled = var.enable_deletion_protection

  attributes = [
    {
      name = "PK"
      type = "S"
    },
  ]

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled     = false
  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  ttl_enabled        = true
  ttl_attribute_name = "expires_at"

  tags = {
    Purpose = "mcp-session-store"
  }
}

resource "aws_ssm_parameter" "mcp_sessions_table_name_secondary" {
  count = var.mcp_server_enabled && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/mcp-sessions-table"
  type  = "String"
  value = module.mcp_sessions_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "mcp-sessions-table-name"
  }
}

output "mcp_sessions_table_arn_secondary" {
  description = "ARN of the secondary-region MCP sessions DDB table (null when HA / MCP disabled)"
  value       = (var.mcp_server_enabled && var.secondary_region != "") ? module.mcp_sessions_table_secondary[0].dynamodb_table_arn : null
}
