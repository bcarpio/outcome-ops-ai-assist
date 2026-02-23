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

  # GSI1 for workspace membership queries
  # Allows: "List all members of workspace X"
  # PK: WS#{workspace_id}, SK: USER#{user_email}
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

  # Point-in-time recovery for data protection
  point_in_time_recovery_enabled = true

  # Server-side encryption enabled by default
  server_side_encryption_enabled = true

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
