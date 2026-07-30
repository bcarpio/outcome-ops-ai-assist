# ============================================================================
# DynamoDB Table for the Symbol Graph.
#
# Persists the structural Fact stream emitted by the symbol-graph
# extractor. Each item is one edge (defines / imports / calls /
# implements / extends / references) scoped to
# ${workspace_id}#${repo}#${commit_sha}, so a regen
# (delete-old + write-new) is a single-partition operation.
#
# Three GSIs power the dominant read paths in the query API:
#   target-kind-index   - callers(X) / importers(X) / implementers(X) by
#                         (workspace_id, target_symbol_id) + kind prefix.
#   target-prefix-index - subpath wildcards like
#                         "@acme-corp/shared-utils/*" by (workspace_id,
#                         target_prefix) with kind/repo in the sort key.
#   target-name-index   - fuzzy resolution by bare name (the tail of
#                         a canonical symbol id) for the chat-side
#                         resolver. Lets chat Query for the canonicals
#                         that match a user-typed bare name instead of
#                         Scanning the workspace partition.
#
# GSI naming convention: see ADR-021. Rename via a versioned table
# replacement if the ADR is later amended.
# ============================================================================

module "symbol_graph_table" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  name      = "${var.environment}-${var.app_name}-symbol-graph"
  hash_key  = "PK"
  range_key = "SK"

  # Single-tenant customer deployment; matches code-maps / chat tables.
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
    # target-kind-index keys (callers/importers/implementers by symbol).
    {
      name = "GSI1PK"
      type = "S"
    },
    {
      name = "GSI1SK"
      type = "S"
    },
    # target-prefix-index keys (subpath wildcard queries).
    {
      name = "GSI2PK"
      type = "S"
    },
    {
      name = "GSI2SK"
      type = "S"
    },
    # target-name-index keys (fuzzy resolution by bare name).
    {
      name = "GSI3PK"
      type = "S"
    },
    {
      name = "GSI3SK"
      type = "S"
    },
  ]

  # GSI1 - target-kind-index:
  #   PK = "${workspace_id}#${target_symbol_id}"
  #   SK = "${kind}#${repo}#${file_path}#${line}"
  # GSI2 - target-prefix-index:
  #   PK = "${workspace_id}#${target_prefix}"
  #   SK = "${target_suffix}#${kind}#${repo}#${file_path}#${line}"
  # GSI3 - target-name-index:
  #   PK = "${workspace_id}#${bare_name}"
  #   SK = "${target_symbol_id}#${kind}#${repo}#${file_path}#${line}"
  global_secondary_indexes = [
    {
      name            = "target-kind-index"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    },
    {
      name            = "target-prefix-index"
      hash_key        = "GSI2PK"
      range_key       = "GSI2SK"
      projection_type = "ALL"
    },
    {
      name            = "target-name-index"
      hash_key        = "GSI3PK"
      range_key       = "GSI3SK"
      projection_type = "ALL"
    },
  ]

  # On-demand: writes are bursty (regen on every code-map refresh), reads
  # are unpredictable but low-volume. Reserved capacity only makes sense
  # after >6 months of observed traffic.
  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_ssm_parameter" "symbol_graph_table_name" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/symbol-graph-table"
  type  = "String"
  value = module.symbol_graph_table.dynamodb_table_id

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_ssm_parameter" "symbol_graph_table_arn" {
  name  = "/${var.environment}/${var.app_name}/dynamodb/symbol-graph-table-arn"
  type  = "String"
  value = module.symbol_graph_table.dynamodb_table_arn

  tags = {
    Purpose = "symbol-graph"
  }
}

output "symbol_graph_table_name" {
  description = "Name of the symbol-graph DynamoDB table"
  value       = module.symbol_graph_table.dynamodb_table_id
}

output "symbol_graph_table_arn" {
  description = "ARN of the symbol-graph DynamoDB table"
  value       = module.symbol_graph_table.dynamodb_table_arn
}

# ============================================================================
# Secondary-region copy (HA mode only).
# ============================================================================

module "symbol_graph_table_secondary" {
  source  = "terraform-aws-modules/dynamodb-table/aws"
  version = "4.0.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name      = "${var.environment}-${var.app_name}-symbol-graph"
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
    },
  ]

  global_secondary_indexes = [
    {
      name            = "target-kind-index"
      hash_key        = "GSI1PK"
      range_key       = "GSI1SK"
      projection_type = "ALL"
    },
    {
      name            = "target-prefix-index"
      hash_key        = "GSI2PK"
      range_key       = "GSI2SK"
      projection_type = "ALL"
    },
    {
      name            = "target-name-index"
      hash_key        = "GSI3PK"
      range_key       = "GSI3SK"
      projection_type = "ALL"
    },
  ]

  billing_mode = "PAY_PER_REQUEST"

  point_in_time_recovery_enabled = true

  server_side_encryption_enabled     = true
  server_side_encryption_kms_key_arn = local.data_cmk_arn_secondary

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_ssm_parameter" "symbol_graph_table_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/symbol-graph-table"
  type  = "String"
  value = module.symbol_graph_table_secondary[0].dynamodb_table_id

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_ssm_parameter" "symbol_graph_table_arn_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/dynamodb/symbol-graph-table-arn"
  type  = "String"
  value = module.symbol_graph_table_secondary[0].dynamodb_table_arn

  tags = {
    Purpose = "symbol-graph"
  }
}

output "symbol_graph_table_arn_secondary" {
  description = "ARN of the secondary-region symbol-graph DDB table (null when HA disabled)"
  value       = var.secondary_region != "" ? module.symbol_graph_table_secondary[0].dynamodb_table_arn : null
}
