# ============================================================================
# IAM policies for the symbol-graph DynamoDB table (plan-gkg chunk 02)
#
# Per-Lambda least-privilege. The table is touched by four Lambdas:
#
#   process-batch-summary  - WRITE path. Reads the current pointer from
#                            the code-maps table (chunk 03 wires the
#                            pointer-swap), batch-writes new Facts, batch-
#                            deletes the old partition.
#   delete-worker          - SWEEP path. Workspace/repo deletion enumerates
#                            and batch-deletes partitions.
#   chat / analyze-pr /    - READ-only path (chunks 05/06/07 wire the
#   generate-code            consumers; policies attach now so the
#                            infrastructure is ready when those chunks
#                            land).
#
# Resource ARNs are exact (table + each GSI). No wildcards. The bundled
# IAM in each Lambda's existing terraform-aws-modules/lambda/aws block
# stays untouched; this file overlays a tightly-scoped policy.
# ============================================================================

# ----------------------------------------------------------------------------
# Locals: shared resource ARN list (table + both GSIs).
# ----------------------------------------------------------------------------

locals {
  symbol_graph_arns = [
    module.symbol_graph_table.dynamodb_table_arn,
    "${module.symbol_graph_table.dynamodb_table_arn}/index/target-kind-index",
    "${module.symbol_graph_table.dynamodb_table_arn}/index/target-prefix-index",
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
