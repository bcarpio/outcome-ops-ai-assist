module "code_maps_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-code-maps"
  hash_key  = "PK"
  range_key = "SK"

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
      name = "type"
      type = "S"
    },
    {
      name = "repo"
      type = "S"
    }
  ]

  # Global Secondary Index for querying by type
  global_secondary_indexes = [
    {
      name            = "type-index"
      hash_key        = "type"
      range_key       = "SK"
      projection_type = "ALL"
    },
    {
      name            = "repo-index"
      hash_key        = "repo"
      range_key       = "SK"
      projection_type = "ALL"
    }
  ]

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
    Purpose = "code-maps"
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
