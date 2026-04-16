# ============================================================================
# Workspace Management Lambda
# Handles workspace CRUD, membership, and repo management
# Uses Lambda Web Adapter (LWA) for HTTP endpoints
# ============================================================================

module "workspace_management_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-workspace-management"
  description   = "Workspace management for OutcomeOps AI Assist"
  handler       = "run.sh"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 512

  # Lambda Web Adapter layer for HTTP endpoints
  layers = [
    "arn:aws:lambda:${var.aws_region}:753240598075:layer:LambdaAdapterLayerX86:25"
  ]

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/workspace-management"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!requirements-dev\\.txt",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
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
    AWS_LWA_READINESS_CHECK_PATH = "/health"
    PORT                         = "8000"
    KB_BUCKET                    = module.knowledge_base_bucket.s3_bucket_id
    DELETE_QUEUE_URL             = aws_sqs_queue.delete_queue[0].url
    OAUTH_KMS_KEY_ARN            = aws_kms_key.oauth_tokens[0].arn
    AUDIT_TABLE                  = module.audit_logs_table.dynamodb_table_id
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for workspaces table
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchWriteItem"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    # DynamoDB for audit logs (system prompt changes + analytics reads)
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn,
        "${module.audit_logs_table.dynamodb_table_arn}/index/*"
      ]
    }

    # SSM for configuration (table names, GitHub PAT)
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

    # KMS for decrypting SSM parameters (GitHub PAT)
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # S3 for document upload/list/delete (presigned URLs and listing)
    s3_documents = {
      effect = "Allow"
      actions = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ]
      resources = [
        module.knowledge_base_bucket.s3_bucket_arn,
        "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
      ]
    }

    # KMS for MCP credential encryption
    oauth_kms = {
      effect = "Allow"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey"
      ]
      resources = [
        aws_kms_key.oauth_tokens[0].arn
      ]
    }

    # SQS for sending delete messages
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.delete_queue[0].arn
      ]
    }
  }

  tags = {
    Purpose = "workspace-management"
  }
}

# Lambda Function URL with IAM auth - only Fargate task role can invoke
resource "aws_lambda_function_url" "workspace_management" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.workspace_management_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST", "GET", "PATCH", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

# Store workspace management URL in SSM for Fargate proxy
resource "aws_ssm_parameter" "workspace_management_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/workspace-management-url"
  description = "Lambda Function URL for workspace management (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.workspace_management[0].function_url

  tags = {
    Purpose = "workspace-management-url"
  }
}

output "workspace_management_lambda_name" {
  description = "Name of the workspace management Lambda function"
  value       = var.enable_workspaces ? module.workspace_management_lambda[0].lambda_function_name : null
}

output "workspace_management_lambda_arn" {
  description = "ARN of the workspace management Lambda function"
  value       = var.enable_workspaces ? module.workspace_management_lambda[0].lambda_function_arn : null
}

output "workspace_management_url" {
  description = "Lambda Function URL for workspace management"
  value       = var.enable_workspaces ? aws_lambda_function_url.workspace_management[0].function_url : null
}
