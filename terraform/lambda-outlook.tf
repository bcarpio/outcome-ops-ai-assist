# ============================================================================
# Outlook Integration Lambda
# Handles OAuth flow and connection management for Microsoft Outlook 365
# ============================================================================

module "outlook_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-outlook-integration"
  description   = "Microsoft Outlook OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/outlook-integration"
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

  environment_variables = {
    ENV                = var.environment
    APP_NAME           = var.app_name
    WORKSPACES_TABLE   = module.workspaces_table[0].dynamodb_table_id
    OAUTH_KMS_KEY_ARN  = aws_kms_key.oauth_tokens[0].arn
    OUTLOOK_SYNC_QUEUE = aws_sqs_queue.outlook_sync[0].url
    DELETE_QUEUE       = aws_sqs_queue.delete_queue[0].url
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for connections and tokens
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
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    # SSM for OAuth configuration
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"
      ]
    }

    # KMS for token encryption/decryption
    kms_oauth = {
      effect = "Allow"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey*"
      ]
      resources = [
        aws_kms_key.oauth_tokens[0].arn
      ]
    }

    # KMS for SSM parameter decryption
    kms_ssm = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # SQS for sync and delete queues
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.outlook_sync[0].arn,
        aws_sqs_queue.delete_queue[0].arn
      ]
    }
  }

  tags = {
    Purpose = "outlook-integration"
  }
}

# Lambda Function URL with IAM auth
resource "aws_lambda_function_url" "outlook_integration" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.outlook_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

# Store URL in SSM for Fargate proxy
resource "aws_ssm_parameter" "outlook_integration_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/outlook-integration-url"
  description = "Lambda Function URL for Outlook integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.outlook_integration[0].function_url

  tags = {
    Purpose = "outlook-integration-url"
  }
}

# ============================================================================
# SQS Queue for Outlook Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "outlook_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-outlook-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.outlook_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "outlook-sync-queue"
  }
}

resource "aws_sqs_queue" "outlook_sync_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-outlook-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "outlook-sync-dlq"
  }
}

# ============================================================================
# Outlook Sync Lambda
# Processes SQS messages to sync Outlook emails to knowledge base
# ============================================================================

module "outlook_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-outlook-sync"
  description   = "Syncs Outlook emails to knowledge base for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large syncs
  memory_size   = 1024

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/outlook-sync"
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

  environment_variables = {
    ENV                  = var.environment
    APP_NAME             = var.app_name
    WORKSPACES_TABLE     = module.workspaces_table[0].dynamodb_table_id
    OAUTH_KMS_KEY_ARN    = aws_kms_key.oauth_tokens[0].arn
    FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion[0].url
    OUTLOOK_SYNC_QUEUE   = aws_sqs_queue.outlook_sync[0].url
    DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for connections and tokens
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:Query"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    # SSM for configuration
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

    # KMS for token encryption/decryption
    kms_oauth = {
      effect = "Allow"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey*"
      ]
      resources = [
        aws_kms_key.oauth_tokens[0].arn
      ]
    }

    # KMS for SSM parameter decryption
    kms_ssm = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # SQS for receiving sync requests and sending continuations
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        aws_sqs_queue.outlook_sync[0].arn
      ]
    }

    # SQS for sending to file ingestion, sync continuation, and delete queues
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.file_ingestion[0].arn,
        aws_sqs_queue.outlook_sync[0].arn,
        aws_sqs_queue.delete_queue[0].arn
      ]
    }
  }

  tags = {
    Purpose = "outlook-sync"
  }
}

# SQS trigger for sync Lambda
resource "aws_lambda_event_source_mapping" "outlook_sync" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.outlook_sync[0].arn
  function_name    = module.outlook_sync_lambda[0].lambda_function_name

  batch_size                         = 1 # Process one sync at a time
  maximum_batching_window_in_seconds = 0 # Process immediately

  scaling_config {
    maximum_concurrency = 5 # Limit concurrent syncs
  }
}

# ============================================================================
# Outlook Sync Scheduler Lambda
# Runs hourly to fan out sync jobs for all active connections
# ============================================================================

module "outlook_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-outlook-scheduler"
  description   = "Hourly scheduler for Outlook sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  # Source code
  source_path = [
    {
      path             = "${path.module}/../lambda/outlook-scheduler"
      pip_requirements = false
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
      ]
    }
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  environment_variables = {
    ENV                = var.environment
    APP_NAME           = var.app_name
    WORKSPACES_TABLE   = module.workspaces_table[0].dynamodb_table_id
    OUTLOOK_SYNC_QUEUE = aws_sqs_queue.outlook_sync[0].url
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for scanning connections
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn
      ]
    }

    # SQS for sending sync jobs
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.outlook_sync[0].arn
      ]
    }
  }

  tags = {
    Purpose = "outlook-scheduler"
  }
}

# EventBridge rule for hourly sync
resource "aws_cloudwatch_event_rule" "outlook_hourly_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                = "${var.environment}-${var.app_name}-outlook-hourly-sync"
  description         = "Trigger Outlook sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "outlook-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "outlook_scheduler" {
  count = var.enable_workspaces ? 1 : 0

  rule      = aws_cloudwatch_event_rule.outlook_hourly_sync[0].name
  target_id = "outlook-scheduler"
  arn       = module.outlook_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "outlook_scheduler_eventbridge" {
  count = var.enable_workspaces ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.outlook_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.outlook_hourly_sync[0].arn
}

# ============================================================================
# SSM Parameter Placeholders for Microsoft OAuth Configuration
# Customer must fill in these values
# ============================================================================

resource "aws_ssm_parameter" "microsoft_client_id" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/oauth/microsoft/client-id"
  description = "Microsoft OAuth App Client ID"
  type        = "String"
  value       = var.microsoft_oauth_client_id != "" ? var.microsoft_oauth_client_id : "PLACEHOLDER_CONFIGURE_ME"

  tags = {
    Purpose = "microsoft-oauth"
  }
}

resource "aws_ssm_parameter" "microsoft_client_secret" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/oauth/microsoft/client-secret"
  description = "Microsoft OAuth App Client Secret - customer must configure"
  type        = "SecureString"
  value       = "PLACEHOLDER_CONFIGURE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "microsoft-oauth"
  }
}

resource "aws_ssm_parameter" "microsoft_callback_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/oauth/microsoft/callback-url"
  description = "Microsoft OAuth callback URL"
  type        = "String"
  value       = var.domain != "" ? "https://${local.fqdn}/api/outlook/callback" : "PLACEHOLDER_CONFIGURE_ME"

  tags = {
    Purpose = "microsoft-oauth"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "outlook_integration_lambda_name" {
  description = "Name of the Outlook integration Lambda function"
  value       = var.enable_workspaces ? module.outlook_integration_lambda[0].lambda_function_name : null
}

output "outlook_integration_lambda_arn" {
  description = "ARN of the Outlook integration Lambda function"
  value       = var.enable_workspaces ? module.outlook_integration_lambda[0].lambda_function_arn : null
}

output "outlook_integration_url" {
  description = "Lambda Function URL for Outlook integration"
  value       = var.enable_workspaces ? aws_lambda_function_url.outlook_integration[0].function_url : null
}

output "outlook_sync_queue_url" {
  description = "SQS queue URL for Outlook sync jobs"
  value       = var.enable_workspaces ? aws_sqs_queue.outlook_sync[0].url : null
}

output "outlook_sync_lambda_name" {
  description = "Name of the Outlook sync Lambda function"
  value       = var.enable_workspaces ? module.outlook_sync_lambda[0].lambda_function_name : null
}

output "outlook_sync_lambda_arn" {
  description = "ARN of the Outlook sync Lambda function"
  value       = var.enable_workspaces ? module.outlook_sync_lambda[0].lambda_function_arn : null
}

output "outlook_scheduler_lambda_name" {
  description = "Name of the Outlook scheduler Lambda function"
  value       = var.enable_workspaces ? module.outlook_scheduler_lambda[0].lambda_function_name : null
}

output "outlook_scheduler_lambda_arn" {
  description = "ARN of the Outlook scheduler Lambda function"
  value       = var.enable_workspaces ? module.outlook_scheduler_lambda[0].lambda_function_arn : null
}
