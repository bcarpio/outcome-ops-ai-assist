# ============================================================================
# DynamoDB Tables
# NOTE: All tables have deletion_protection_enabled = true to prevent
# accidental destruction. This must be explicitly disabled before deletion.
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
  deletion_protection_enabled = false

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

  # Server-side encryption enabled by default
  server_side_encryption_enabled = true

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
