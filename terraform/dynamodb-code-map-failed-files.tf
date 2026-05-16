# ============================================================================
# DynamoDB Table for Per-File Failure Log (plan-git-truth chunk 01)
#
# When a consumer hits a permanent per-unit failure (after exhausting
# in-message retries), it writes a row here, increments failed_count on
# the pending ledger, and does NOT raise — so the rest of the batch
# processes. Replaces today's bubble-up-and-DLQ-the-whole-record pattern
# in process-repo-summary.
#
# TTL: 30 days from failed_at — long enough for an operator to
# investigate, short enough to keep the table from ballooning. The UI
# can later show "SHA abc1234: 197 of 200 code units ingested, 3 failed
# (see details)" by querying this table for a (workspace, repo,
# target_sha) prefix.
# ============================================================================

module "code_map_failed_files_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-code-map-failed-files"
  hash_key  = "PK"
  range_key = "SK"

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
  ]

  ttl_enabled        = true
  ttl_attribute_name = "ttl"

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "sha-truth-failed-files"
  }
}

resource "aws_ssm_parameter" "code_map_failed_files_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/code-map-failed-files-table"
  type  = "String"
  value = module.code_map_failed_files_table.dynamodb_table_id

  tags = {
    Purpose = "sha-truth-failed-files"
  }
}

output "code_map_failed_files_table_name" {
  description = "Name of the SHA-truth failed-files DynamoDB table"
  value       = module.code_map_failed_files_table.dynamodb_table_id
}

output "code_map_failed_files_table_arn" {
  description = "ARN of the SHA-truth failed-files DynamoDB table"
  value       = module.code_map_failed_files_table.dynamodb_table_arn
}
