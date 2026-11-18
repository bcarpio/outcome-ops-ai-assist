# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0

# ============================================================================
# List Recent Docs Lambda (KB ingestion verification)
# Internal worker invoked directly by the outcome-ops-assist CLI. Read-only:
# lists S3 Vectors metadata and returns the newest documents. Primary-region
# only, like query-kb: it writes nothing, so there is no secondary mirror.
# ============================================================================

module "list_recent_docs_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-list-recent-docs"
  description   = "List recently ingested knowledge base documents (S3 Vectors metadata)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  # Walks the full vector index (S3 Vectors has no server-side ordering).
  timeout     = 300
  memory_size = 512

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module)
  source_path = [
    "${path.module}/../lambda/list-recent-docs",
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  attach_policy_statements = true
  policy_statements = {
    # S3 Vectors bucket/index names
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        aws_ssm_parameter.vector_bucket_name.arn,
        aws_ssm_parameter.vector_index_name.arn
      ]
    }

    # Read-only listing of vector metadata
    s3_vectors_list = {
      effect = "Allow"
      actions = [
        "s3vectors:ListVectors",
        "s3vectors:GetVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }
  }

  tags = {
    Purpose = "list-recent-docs"
  }
}

output "list_recent_docs_lambda_name" {
  description = "Name of the list-recent-docs Lambda function"
  value       = module.list_recent_docs_lambda.lambda_function_name
}
