# ============================================================================
# IAM policies for the symbol-graph DynamoDB table.
#
# Per-Lambda least-privilege. The table is touched by four Lambdas:
#
#   process-batch-summary  - WRITE path. Reads the current pointer from
#                            the code-maps table for the pointer-swap,
#                            batch-writes new Facts, batch-deletes the
#                            old partition.
#   delete-worker          - SWEEP path. Workspace/repo deletion enumerates
#                            and batch-deletes partitions.
#   chat / analyze-pr /    - READ-only path. Consumers are wired now so
#   generate-code /          the infrastructure is ready. process-pr-check reads
#   process-pr-check         callers/importers for BREAKING_CHANGES (ADR-054).
#
# Resource ARNs are exact (table + each GSI; GSI names governed by
# ADR-021). No wildcards. The bundled IAM in each Lambda's existing
# terraform-aws-modules/lambda/aws block stays untouched; this file
# overlays a tightly-scoped policy.
# ============================================================================

# ----------------------------------------------------------------------------
# Locals: shared resource ARN list (table + both GSIs).
# ----------------------------------------------------------------------------

locals {
  symbol_graph_arns = [
    module.symbol_graph_table.dynamodb_table_arn,
    "${module.symbol_graph_table.dynamodb_table_arn}/index/target-kind-index",
    "${module.symbol_graph_table.dynamodb_table_arn}/index/target-prefix-index",
    "${module.symbol_graph_table.dynamodb_table_arn}/index/target-name-index",
  ]
}

# ============================================================================
# Writer policy - process-batch-summary
# Required for the delete-then-write regen path (chunk 03):
#   Query    - enumerate the previous-SHA partition for deletion
#   BatchWriteItem / DeleteItem - delete-old + write-new in 25-item batches
#   PutItem  - pointer-swap fallback
# ============================================================================

resource "aws_iam_policy" "symbol_graph_writer" {
  name        = "${var.environment}-${var.app_name}-symbol-graph-writer"
  description = "Read-modify-write access to the symbol-graph table for process-batch-summary"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:BatchWriteItem",
          "dynamodb:DeleteItem",
        ]
        Resource = local.symbol_graph_arns
      },
    ]
  })

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_iam_role_policy_attachment" "process_batch_summary_symbol_graph_writer" {
  role       = module.process_batch_summary_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_writer.arn
}

# ============================================================================
# Sweep policy - delete-worker
# Workspace/repo lifecycle deletes enumerate all partitions matching the
# (workspace_id, repo) prefix and batch-delete the items.
# ============================================================================

resource "aws_iam_policy" "symbol_graph_sweep" {
  name        = "${var.environment}-${var.app_name}-symbol-graph-sweep"
  description = "Sweep (Query + BatchWriteItem) access to the symbol-graph table for delete-worker"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:BatchWriteItem",
          "dynamodb:DeleteItem",
        ]
        Resource = local.symbol_graph_arns
      },
    ]
  })

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_iam_role_policy_attachment" "delete_worker_symbol_graph_sweep" {
  count = var.enable_workspaces ? 1 : 0

  role       = module.delete_worker_lambda[0].lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_sweep.arn
}

# ============================================================================
# Reader policy - chat / analyze-pr / generate-code
# Query-only against the table and both GSIs. Writes deliberately omitted
# so a buggy retrieval path cannot corrupt the graph.
# ============================================================================

resource "aws_iam_policy" "symbol_graph_reader" {
  name        = "${var.environment}-${var.app_name}-symbol-graph-reader"
  description = "Query-only access to the symbol-graph table for chat, analyze-pr, and generate-code"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
        ]
        Resource = local.symbol_graph_arns
      },
    ]
  })

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_iam_role_policy_attachment" "chat_symbol_graph_reader" {
  role       = module.chat_streaming_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

resource "aws_iam_role_policy_attachment" "analyze_pr_symbol_graph_reader" {
  role       = module.analyze_pr_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

resource "aws_iam_role_policy_attachment" "generate_code_symbol_graph_reader" {
  role       = module.generate_code_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

# query-kb runs the opt-in hybrid augmentation (ADR-023 seam): on a
# hybrid=true request it resolves a changed/queried symbol's
# callers/importers from the symbol graph and fuses them into the retrieval
# context. Query-only, identical to the other readers. (query-kb is
# primary-only -- there is no query-kb-secondary module -- so no secondary
# mirror applies.)
resource "aws_iam_role_policy_attachment" "query_kb_symbol_graph_reader" {
  role       = module.query_kb_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

# process-pr-check runs the BREAKING_CHANGES plane-1 consumer lookup (ADR-054):
# it resolves a changed symbol's callers/importers from the symbol graph.
# Query-only, identical to the other readers.
resource "aws_iam_role_policy_attachment" "process_pr_check_symbol_graph_reader" {
  role       = module.process_pr_check_lambda.lambda_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

# The mcp-outcomeops Fargate task exposes find_callers / find_importers
# / find_implementers / find_definitions to external MCP clients (see
# docs/mcp-server.md). It needs the same query-only access as the
# consumer Lambdas. Attached only when the MCP server is enabled (the
# module count gates the task role's existence).
resource "aws_iam_role_policy_attachment" "mcp_outcomeops_symbol_graph_reader" {
  count = var.mcp_server_enabled ? 1 : 0

  role       = module.mcp_outcomeops[0].task_role_name
  policy_arn = aws_iam_policy.symbol_graph_reader.arn
}

# The reader also reads the code-maps table for per-(workspace, repo)
# pointer state -- that's how QueryResult.staleness_seconds /
# QueryResult.graph_sha get populated. The mcp-outcomeops baseline IAM
# (terraform/mcp-outcomeops.tf) already grants Query/GetItem on the
# workspaces table, but the code-maps table is a separate table; grant
# read-only access here so the staleness reporting works.
resource "aws_iam_policy" "mcp_outcomeops_code_maps_read" {
  count = var.mcp_server_enabled ? 1 : 0

  name        = "${var.environment}-${var.app_name}-mcp-outcomeops-code-maps-read"
  description = "Read pointer state from the code-maps table for symbol-graph staleness reporting"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        Resource = [
          module.code_maps_table.dynamodb_table_arn,
          "${module.code_maps_table.dynamodb_table_arn}/index/*",
        ]
      },
    ]
  })

  tags = {
    Purpose = "symbol-graph"
  }
}

resource "aws_iam_role_policy_attachment" "mcp_outcomeops_code_maps_read" {
  count = var.mcp_server_enabled ? 1 : 0

  role       = module.mcp_outcomeops[0].task_role_name
  policy_arn = aws_iam_policy.mcp_outcomeops_code_maps_read[0].arn
}
