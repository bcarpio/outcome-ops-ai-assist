# ============================================================================
# DynamoDB Table for Chat Conversations
# Single-tenant design - no org/workspace isolation needed
# ============================================================================

module "chat_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-chat"
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
      name = "GSI2PK"
      type = "S"
    },
    {
      name = "GSI2SK"
      type = "S"
    },
    {
      name = "GSI3PK"
      type = "S"
    },
    {
      name = "GSI3SK"
      type = "S"
    }
  ]

  # GSI2: User conversations sorted by recency
  # GSI2PK: USER#{user_id}
  # GSI2SK: CONV#{updated_at} (ISO 8601, sorts lexicographically)
  #
  # GSI3: Workspace-scoped shared conversations sorted by recency
  # GSI3PK: WS_SHARE#{workspace_id}
  # GSI3SK: CONV#{updated_at} (ISO 8601, sorts lexicographically)
  global_secondary_indexes = [
    {
      name            = "GSI2"
      hash_key        = "GSI2PK"
      range_key       = "GSI2SK"
      projection_type = "ALL"
    },
    {
      name               = "GSI3"
      hash_key           = "GSI3PK"
      range_key          = "GSI3SK"
      projection_type    = "INCLUDE"
      non_key_attributes = ["conversation_id", "title", "message_count", "user_id", "shared_workspace_ids"]
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
    Purpose = "chat-conversations"
  }
}

resource "aws_ssm_parameter" "chat_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/chat-table"
  type  = "String"
  value = module.chat_table.dynamodb_table_id

  tags = {
    Purpose = "chat-table-name"
  }
}

output "chat_table_name" {
  description = "Name of the chat DynamoDB table"
  value       = module.chat_table.dynamodb_table_id
}

output "chat_table_arn" {
  description = "ARN of the chat DynamoDB table"
  value       = module.chat_table.dynamodb_table_arn
}
