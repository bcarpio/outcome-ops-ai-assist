# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# RAG Query Lambda Functions
# - ask-claude: Generate RAG answers using Claude
# - query-kb: Orchestrate RAG pipeline (vector search via S3 Vectors + Claude)
# ============================================================================

# ============================================================================
# Ask Claude Lambda (RAG Answer Generation)
# ============================================================================

module "ask_claude_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-ask-claude"
  description   = "Generate RAG answers using Claude 3.5 Sonnet via Bedrock"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes for Claude API calls
  memory_size   = 512

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module)
  source_path = [
    "${path.module}/../lambda/ask-claude",
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = merge(
    {
      ENV                                = var.environment
      APP_NAME                           = var.app_name
      AUDIT_TABLE                        = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS               = tostring(var.audit_retention_days)
      BEDROCK_ADVANCED_MODEL_ID          = var.bedrock_advanced_model_id
      BEDROCK_DEFAULT_BACKEND            = var.bedrock_default_backend
      BEDROCK_RESPONSES_REASONING_EFFORT = var.bedrock_responses_reasoning_effort
      BEDROCK_MANTLE_API_KEY_PARAM       = "/${var.environment}/${var.app_name}/bedrock/mantle-api-key"
      BEDROCK_BASIC_MODEL_ID             = var.bedrock_basic_model_id
      BEDROCK_ADVANCED_INPUT_PRICE       = tostring(var.bedrock_advanced_input_price)
      BEDROCK_ADVANCED_OUTPUT_PRICE      = tostring(var.bedrock_advanced_output_price)
      BEDROCK_BASIC_INPUT_PRICE          = tostring(var.bedrock_basic_input_price)
      BEDROCK_BASIC_OUTPUT_PRICE         = tostring(var.bedrock_basic_output_price)
    },
    # ADR-016 v1.2 -- ask-claude serves the same "user Q&A" surface as
    # chat, so it belongs to the chat workload for cost attribution.
    local.bedrock_chat_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB audit logging
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn
      ]
    }

    # Bedrock models (all foundation models and inference profiles for model portability)
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
      ]
    }

    # AWS Marketplace permissions for Bedrock models
    aws_marketplace = {
      effect = "Allow"
      actions = [
        "aws-marketplace:ViewSubscriptions",
        "aws-marketplace:Subscribe"
      ]
      resources = ["*"]
    }
  }

  tags = {
    Purpose = "ask-claude"
  }
}

# Store Lambda ARN in SSM for query-kb orchestrator
resource "aws_ssm_parameter" "ask_claude_lambda_arn" {
  name  = "/${var.environment}/${var.app_name}/lambda/ask-claude-arn"
  type  = "String"
  value = module.ask_claude_lambda.lambda_function_arn

  tags = {
    Purpose = "ask-claude-config"
  }
}

output "ask_claude_lambda_arn" {
  description = "ARN of the ask-claude Lambda function"
  value       = module.ask_claude_lambda.lambda_function_arn
}

output "ask_claude_lambda_name" {
  description = "Name of the ask-claude Lambda function"
  value       = module.ask_claude_lambda.lambda_function_name
}

# ============================================================================
# Query KB Lambda (RAG Orchestrator with S3 Vectors)
# Now performs vector search directly via S3 Vectors instead of invoking
# a separate vector-query Lambda
# ============================================================================

module "query_kb_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-query-kb"
  description   = "Orchestrate RAG pipeline (S3 Vectors search + Claude generation)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 600 # 10 minutes for full RAG pipeline
  memory_size   = 512

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module)
  source_path = [
    "${path.module}/../lambda/query-kb",
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = merge(
    {
      ENV                            = var.environment
      APP_NAME                       = var.app_name
      AUDIT_TABLE                    = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS           = tostring(var.audit_retention_days)
      BEDROCK_EMBEDDING_MODEL_ID     = var.bedrock_embedding_model_id
      BEDROCK_EMBEDDING_INPUT_PRICE  = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE = tostring(var.bedrock_embedding_output_price)
      BEDROCK_RERANK_MODEL_ID        = var.bedrock_rerank_model_id
      BEDROCK_RERANK_PRICE_PER_1K    = tostring(var.bedrock_rerank_price_per_1k)
      RERANK_ENABLED                 = tostring(var.bedrock_rerank_enabled)
      RERANK_MAX_FETCH               = tostring(var.rerank_max_fetch)
      RERANK_MIN_SCORE               = tostring(var.rerank_min_score)
      # Workspace metadata table for chunk-05 source-workspace name
      # resolution. Empty when the deploy has enable_workspaces=false;
      # the resolver treats missing as "no lookup available".
      WORKSPACES_TABLE = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_id : ""

      # Opt-in hybrid retrieval (ADR-023 seam): when a caller passes
      # hybrid=true, query-kb fuses deterministic symbol-graph facts into
      # the context. IAM read access is attached in iam_symbol_graph.tf;
      # CODE_MAPS_TABLE feeds symbol_graph_reader's per-repo staleness
      # pointer, same as chat. Unset vars -> hybrid degrades to RAG-only.
      SYMBOL_GRAPH_TABLE = module.symbol_graph_table.dynamodb_table_id
      CODE_MAPS_TABLE    = module.code_maps_table.dynamodb_table_id
      # The hybrid augmentation's router classify_intent calls the BASIC
      # chat model. query-kb did no server-side LLM call before hybrid
      # (synthesis lives in ask-claude), so it was never given a model id;
      # the Claude app inference profiles are disabled in prd (Control Tower
      # region-deny SCP -- see bedrock-inference-profiles.tf), so
      # local.bedrock_chat_env carries no BEDROCK_CHAT_BASIC_PROFILE_ARN.
      # Wire the us.* system-profile fallback -- same as chat (lambda-chat.tf)
      # and ask-claude above -- or hybrid fails augment_error -> RAG-only.
      BEDROCK_BASIC_MODEL_ID = var.bedrock_basic_model_id
    },
    # ADR-016 v1.2 -- query-kb is chat's retrieval endpoint (embed + rerank).
    local.bedrock_chat_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB audit logging
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn
      ]
    }

    # Workspace metadata read for the chunk-05 citation polish:
    # BatchGetItem resolves workspace_id -> workspace name so source
    # citations carry "(Workspace Name)" for compliance auditor
    # traceability. Fail-soft in the handler if this grant is
    # missing; sources render name-less. Only fires when the
    # deployment has workspaces enabled.
    dynamodb_workspaces_read = {
      effect = "Allow"
      actions = [
        "dynamodb:BatchGetItem",
        "dynamodb:GetItem",
      ]
      resources = var.enable_workspaces ? [
        module.workspaces_table[0].dynamodb_table_arn,
      ] : []
    }

    # Opt-in hybrid retrieval: symbol_graph_reader reads the code-maps
    # table's per-repo pointer row to compute staleness_seconds / graph_sha
    # so a graph-backed answer can disclose freshness. Read-only; identical
    # to chat's code_maps_pointer_read. (Symbol-graph table + GSI read is
    # attached via the shared symbol_graph_reader policy in
    # iam_symbol_graph.tf.)
    code_maps_pointer_read = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:Query",
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn,
        "${module.code_maps_table.dynamodb_table_arn}/index/*",
      ]
    }

    # Read SSM parameters (to get Lambda ARNs and S3 Vectors config)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # Decrypt SSM parameters encrypted with AWS managed KMS key
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # Invoke ask-claude Lambda for answer generation
    lambda_invoke = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.ask_claude_lambda.lambda_function_arn
      ]
    }

    # S3 Vectors query permissions for semantic search
    s3_vectors_query = {
      effect = "Allow"
      actions = [
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }

    # Bedrock models for embedding generation.
    # ADR-016 v1.2: application inference profiles have a distinct
    # resource type from system-defined profiles -- both flavors need
    # explicit grants (per project_bedrock_application_profile_arn_gotcha).
    bedrock_titan = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
      ]
    }

    # Bedrock Rerank API for post-retrieval reranking
    bedrock_rerank = {
      effect = "Allow"
      actions = [
        "bedrock:Rerank"
      ]
      resources = ["*"]
    }

    # AWS Marketplace permissions for Bedrock models (required for Cohere Rerank)
    aws_marketplace = {
      effect = "Allow"
      actions = [
        "aws-marketplace:ViewSubscriptions",
        "aws-marketplace:Subscribe"
      ]
      resources = ["*"]
    }
  }

  tags = {
    Purpose = "query-kb"
  }
}

output "query_kb_lambda_arn" {
  description = "ARN of the query-kb Lambda function"
  value       = module.query_kb_lambda.lambda_function_arn
}

output "query_kb_lambda_name" {
  description = "Name of the query-kb Lambda function"
  value       = module.query_kb_lambda.lambda_function_name
}
