# ============================================================================
# GitHub App Integration Lambda
# Handles OAuth flow and connection management for GitHub App
# ============================================================================

module "github_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-github-integration"
  description   = "GitHub App OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/github-integration"
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

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
      GITHUB_SYNC_QUEUE = aws_sqs_queue.github_sync[0].url
      DELETE_QUEUE      = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE       = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge(
    {
      # DynamoDB for connections and repos
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

      # SSM for GitHub App configuration
      ssm_read = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/*"
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
          aws_sqs_queue.github_sync[0].arn,
          aws_sqs_queue.delete_queue[0].arn
        ]
      }

      # DynamoDB audit log writes (plan/audit-completeness)
      dynamodb_audit = {
        effect = "Allow"
        actions = [
          "dynamodb:PutItem"
        ]
        resources = [
          module.audit_logs_table.dynamodb_table_arn
        ]
      }
    },
    local.outcomeops_license_iam,
  )

  tags = {
    Purpose = "github-integration"
  }
}

# Lambda Function URL with IAM auth
resource "aws_lambda_function_url" "github_integration" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.github_integration_lambda[0].lambda_function_name
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
resource "aws_ssm_parameter" "github_integration_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/github-integration-url"
  description = "Lambda Function URL for GitHub integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.github_integration[0].function_url

  tags = {
    Purpose = "github-integration-url"
  }
}

# ============================================================================
# SQS Queue for GitHub Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "github_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-github-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.github_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "github-sync-queue"
  }
}

resource "aws_sqs_queue" "github_sync_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-github-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "github-sync-dlq"
  }
}

# ============================================================================
# GitHub Sync Lambda
# Processes SQS messages to sync GitHub repos for code-map generation
# ============================================================================

module "github_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-github-sync"
  description   = "Syncs GitHub repos for code-map generation for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large repos
  memory_size   = 1024

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/github-sync"
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

  # ADR-019: license validator layer required for every customer-facing Lambda.
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table[0].dynamodb_table_id
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion[0].url
      REPO_SUMMARIES_QUEUE = module.repo_summaries_queue.queue_url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
      KB_BUCKET            = module.knowledge_base_bucket.s3_bucket_id
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge(
    {
      # DynamoDB for connections and state
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

      # SSM for GitHub App configuration
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

      # SQS for receiving sync requests
      sqs_receive = {
        effect = "Allow"
        actions = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        resources = [
          aws_sqs_queue.github_sync[0].arn
        ]
      }

      # SQS for sending to file ingestion, repo summaries, and delete queues
      sqs_send = {
        effect = "Allow"
        actions = [
          "sqs:SendMessage"
        ]
        resources = [
          aws_sqs_queue.file_ingestion[0].arn,
          module.repo_summaries_queue.queue_arn,
          aws_sqs_queue.delete_queue[0].arn
        ]
      }

      # S3 for uploading standards repo files to knowledge base
      s3_kb = {
        effect = "Allow"
        actions = [
          "s3:PutObject"
        ]
        resources = [
          "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
        ]
      }

      # DynamoDB audit log writes (plan/audit-completeness)
      dynamodb_audit = {
        effect = "Allow"
        actions = [
          "dynamodb:PutItem"
        ]
        resources = [
          module.audit_logs_table.dynamodb_table_arn
        ]
      }
    },
    local.outcomeops_license_iam,
  )

  tags = {
    Purpose = "github-sync"
  }
}

# SQS trigger for sync Lambda
resource "aws_lambda_event_source_mapping" "github_sync" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.github_sync[0].arn
  function_name    = module.github_sync_lambda[0].lambda_function_name

  batch_size                         = 1 # Process one sync at a time
  maximum_batching_window_in_seconds = 0 # Process immediately

  scaling_config {
    maximum_concurrency = 5 # Limit concurrent syncs
  }
}

# ============================================================================
# GitHub Sync Scheduler Lambda
# Runs hourly to fan out sync jobs for all active repos
# ============================================================================

module "github_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-github-scheduler"
  description   = "Hourly scheduler for GitHub sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  # Source code
  source_path = [
    {
      path             = "${path.module}/../lambda/github-scheduler"
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
    ENV               = var.environment
    APP_NAME          = var.app_name
    WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
    GITHUB_SYNC_QUEUE = aws_sqs_queue.github_sync[0].url
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for scanning repos
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
        aws_sqs_queue.github_sync[0].arn
      ]
    }
  }

  tags = {
    Purpose = "github-scheduler"
  }
}

# EventBridge rule for hourly sync
resource "aws_cloudwatch_event_rule" "github_hourly_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                = "${var.environment}-${var.app_name}-github-hourly-sync"
  description         = "Trigger GitHub sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "github-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "github_scheduler" {
  count = var.enable_workspaces ? 1 : 0

  rule      = aws_cloudwatch_event_rule.github_hourly_sync[0].name
  target_id = "github-scheduler"
  arn       = module.github_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "github_scheduler_eventbridge" {
  count = var.enable_workspaces ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.github_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.github_hourly_sync[0].arn
}

# ============================================================================
# SSM Parameters for GitHub App Configuration
# These need to be manually configured after GitHub App creation
# ============================================================================

resource "aws_ssm_parameter" "github_app_id" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/app-id"
  description = "GitHub App ID"
  type        = "String"
  value       = var.github_app_id != "" ? var.github_app_id : "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_private_key" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/private-key"
  description = "GitHub App private key (PEM format)"
  type        = "SecureString"
  value       = "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_client_id" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/client-id"
  description = "GitHub App OAuth client ID"
  type        = "String"
  value       = var.github_client_id != "" ? var.github_client_id : "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_client_secret" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/client-secret"
  description = "GitHub App OAuth client secret"
  type        = "SecureString"
  value       = "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_webhook_secret" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/webhook-secret"
  description = "GitHub App webhook secret"
  type        = "SecureString"
  value       = "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_callback_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/callback-url"
  description = "GitHub App OAuth callback URL"
  type        = "String"
  value       = var.domain != "" ? "https://${local.fqdn}/api/github/callback" : "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  tags = {
    Purpose = "github-app"
  }
}

resource "aws_ssm_parameter" "github_app_slug" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/github/app-slug"
  description = "GitHub App URL slug (from GitHub App settings page URL)"
  type        = "String"
  value       = var.github_app_slug != "" ? var.github_app_slug : "PLACEHOLDER_CONFIGURE_ME"
  overwrite   = true

  tags = {
    Purpose = "github-app"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "github_integration_lambda_name" {
  description = "Name of the GitHub integration Lambda function"
  value       = var.enable_workspaces ? module.github_integration_lambda[0].lambda_function_name : null
}

output "github_integration_lambda_arn" {
  description = "ARN of the GitHub integration Lambda function"
  value       = var.enable_workspaces ? module.github_integration_lambda[0].lambda_function_arn : null
}

output "github_integration_url" {
  description = "Lambda Function URL for GitHub integration"
  value       = var.enable_workspaces ? aws_lambda_function_url.github_integration[0].function_url : null
}

output "github_sync_queue_url" {
  description = "SQS queue URL for GitHub sync jobs"
  value       = var.enable_workspaces ? aws_sqs_queue.github_sync[0].url : null
}

output "github_sync_lambda_name" {
  description = "Name of the GitHub sync Lambda function"
  value       = var.enable_workspaces ? module.github_sync_lambda[0].lambda_function_name : null
}

output "github_sync_lambda_arn" {
  description = "ARN of the GitHub sync Lambda function"
  value       = var.enable_workspaces ? module.github_sync_lambda[0].lambda_function_arn : null
}

output "github_scheduler_lambda_name" {
  description = "Name of the GitHub scheduler Lambda function"
  value       = var.enable_workspaces ? module.github_scheduler_lambda[0].lambda_function_name : null
}

output "github_scheduler_lambda_arn" {
  description = "ARN of the GitHub scheduler Lambda function"
  value       = var.enable_workspaces ? module.github_scheduler_lambda[0].lambda_function_arn : null
}
