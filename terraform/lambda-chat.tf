# ============================================================================
# Chat Streaming Lambda
# Uses Lambda Web Adapter (LWA) for response streaming
# API Gateway HTTP API has 30s timeout - streaming requires Lambda Function URL
# ============================================================================

module "chat_streaming_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-chat-streaming"
  description   = "Streaming chat interface for OutcomeOps AI Assist"
  handler       = "run.sh"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes max for streaming responses
  memory_size   = 1024

  # Lambda Web Adapter layer for HTTP streaming
  layers = [
    "arn:aws:lambda:${var.aws_region}:753240598075:layer:LambdaAdapterLayerX86:25"
  ]

  # Source code with pip requirements (includes shared module)
  source_path = [
    {
      path             = "${path.module}/../lambda/chat"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!requirements-dev\\.txt",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables for LWA
  environment_variables = {
    ENV                          = var.environment
    APP_NAME                     = var.app_name
    AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"
    AWS_LWA_INVOKE_MODE          = "RESPONSE_STREAM"
    AWS_LWA_READINESS_CHECK_PATH = "/health"
    PORT                         = "8000"
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for conversations and messages
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query"
      ]
      resources = [
        module.chat_table.dynamodb_table_arn
      ]
    }

    # SSM for configuration (vector bucket, index names, etc.)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # KMS for decrypting SSM parameters
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # Bedrock for streaming chat (Claude) and embeddings (Titan)
    bedrock_invoke = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream"
      ]
      resources = [
        # Titan Embeddings v2 for query/memory embeddings
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0",
        # Claude Haiku 3.5 (standard - fast)
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-3-5-haiku-20241022-v1:0",
        # Claude Sonnet 4.5 (advanced - better quality)
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }

    # S3 Vectors for KB search and conversation memory
    s3_vectors = {
      effect = "Allow"
      actions = [
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }
  }

  tags = {
    Purpose = "chat-streaming"
  }
}

# Lambda Function URL with IAM auth - only Fargate task role can invoke
# Security: Requests must be SigV4 signed, no anonymous access
resource "aws_lambda_function_url" "chat_streaming" {
  function_name      = module.chat_streaming_lambda.lambda_function_name
  authorization_type = "AWS_IAM"

  # Enable response streaming mode
  invoke_mode = "RESPONSE_STREAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token"]
    allow_credentials = false
    max_age           = 300
  }
}

# Store streaming URL in SSM for Fargate proxy
resource "aws_ssm_parameter" "chat_streaming_url" {
  name        = "/${var.environment}/${var.app_name}/api/chat-streaming-url"
  description = "Lambda Function URL for streaming chat (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.chat_streaming.function_url

  tags = {
    Purpose = "chat-streaming-url"
  }
}

output "chat_streaming_lambda_name" {
  description = "Name of the chat streaming Lambda function"
  value       = module.chat_streaming_lambda.lambda_function_name
}

output "chat_streaming_lambda_arn" {
  description = "ARN of the chat streaming Lambda function"
  value       = module.chat_streaming_lambda.lambda_function_arn
}
