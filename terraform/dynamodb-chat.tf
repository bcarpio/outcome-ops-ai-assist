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
    }
  ]

  # No GSIs needed - direct key lookups only
  # PK: CONV#{conversation_id}
  # SK: METADATA | MSG#{timestamp}#{message_id}
  global_secondary_indexes = []

  # On-demand billing for variable traffic patterns
  billing_mode = "PAY_PER_REQUEST"

  # Point-in-time recovery for data protection
  point_in_time_recovery_enabled = true

  # Server-side encryption enabled by default
  server_side_encryption_enabled = true

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
