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
  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Bedrock Claude models
    # Haiku: fast, cheap RAG synthesis (default)
    # Sonnet: advanced reasoning for complex queries (--advanced flag)
    # Cross-region inference profiles route to available regions
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        # Haiku 3.5
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-3-5-haiku-20241022-v1:0",
        # Sonnet 4.5 (for --advanced mode)
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }

    # AWS Marketplace permissions for Anthropic Claude models
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

  # Source code from local directory
  source_path = "${path.module}/../lambda/query-kb"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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

    # Bedrock embeddings (Titan Embeddings v2) for query embedding generation
    bedrock_titan = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
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
