# ============================================================================
# Jira Integration Lambda
# Handles OAuth flow and connection management for Jira Cloud
# ============================================================================

module "jira_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-jira-integration"
  description   = "Jira OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/jira-integration"
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

  # ADR-019: attach OutcomeOps license validator layer so the
  # ``report_repos_snapshot`` helper the handler imports is available
  # at cold start (no ImportError fallback that would silently no-op
  # every connect/disconnect snapshot).
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_key.oauth_tokens[0].arn
      JIRA_SYNC_QUEUE      = aws_sqs_queue.jira_sync[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
    local.ha_scheduler_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge(
    {
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
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/atlassian/*"
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
        resources = local.oauth_kms_token_key_arns
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
          aws_sqs_queue.jira_sync[0].arn,
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
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "jira-integration"
  }
}

# Lambda Function URL with IAM auth
resource "aws_lambda_function_url" "jira_integration" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  function_name      = module.jira_integration_lambda[0].lambda_function_name
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
resource "aws_ssm_parameter" "jira_integration_url" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/jira-integration-url"
  description = "Lambda Function URL for Jira integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.jira_integration[0].function_url

  tags = {
    Purpose = "jira-integration-url"
  }
}

# ============================================================================
# SQS Queue for Jira Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "jira_sync" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-jira-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.jira_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "jira-sync-queue"
  }
}

resource "aws_sqs_queue" "jira_sync_dlq" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-jira-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "jira-sync-dlq"
  }
}

# ============================================================================
# Jira Sync Lambda
# Processes SQS messages to sync Jira issues to vector store
# ============================================================================

module "jira_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-jira-sync"
  description   = "Syncs Jira issues to vector store for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large syncs
  memory_size   = 1024

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/jira-sync"
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
      OAUTH_KMS_KEY_ARN    = aws_kms_key.oauth_tokens[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge(
    {
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
        resources = local.oauth_kms_token_key_arns
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
          aws_sqs_queue.jira_sync[0].arn
        ]
      }

      # SQS for sending to file ingestion and delete queues
      sqs_send = {
        effect = "Allow"
        actions = [
          "sqs:SendMessage"
        ]
        resources = [
          aws_sqs_queue.file_ingestion[0].arn,
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
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "jira-sync"
  }
}

# SQS trigger for sync Lambda
resource "aws_lambda_event_source_mapping" "jira_sync" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  event_source_arn = aws_sqs_queue.jira_sync[0].arn
  function_name    = module.jira_sync_lambda[0].lambda_function_name

  batch_size                         = 1 # Process one sync at a time
  maximum_batching_window_in_seconds = 0 # Process immediately

  # Chunk 02: partial-batch-failure. Handler returns batchItemFailures for
  # records that failed; without this, ReportBatchItemFailures is disabled
  # and SQS deletes every record on 200 (silent KB data loss).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5 # Limit concurrent syncs
  }
}

# ============================================================================
# Jira Sync Scheduler Lambda
# Runs hourly to fan out sync jobs for all active connections
# ============================================================================

module "jira_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-jira-scheduler"
  description   = "Hourly scheduler for Jira sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  # Source code
  source_path = [
    {
      path             = "${path.module}/../lambda/jira-scheduler"
      pip_requirements = false
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    },
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  environment_variables = merge({
    ENV              = var.environment
    APP_NAME         = var.app_name
    WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id
    JIRA_SYNC_QUEUE  = aws_sqs_queue.jira_sync[0].url
    },
    local.ha_scheduler_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
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
        aws_sqs_queue.jira_sync[0].arn
      ]
    }
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "jira-scheduler"
  }
}

# EventBridge rule for hourly sync
resource "aws_cloudwatch_event_rule" "jira_hourly_sync" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  name                = "${var.environment}-${var.app_name}-jira-hourly-sync"
  description         = "Trigger Jira sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "jira-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "jira_scheduler" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  rule      = aws_cloudwatch_event_rule.jira_hourly_sync[0].name
  target_id = "jira-scheduler"
  arn       = module.jira_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "jira_scheduler_eventbridge" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.jira_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.jira_hourly_sync[0].arn
}

# ============================================================================
# SSM Parameter for Jira OAuth Callback URL
# (Client ID and Secret are shared with Confluence via Atlassian app)
# ============================================================================

resource "aws_ssm_parameter" "atlassian_jira_callback_url" {
  count = var.enable_workspaces && var.enable_jira_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/oauth/atlassian/jira-callback-url"
  description = "OAuth callback URL for Jira. Register this in your Atlassian OAuth app config after local.fqdn resolves to the UI ALB (whether via terraform-managed Route53 or external DNS - see docs/byo-cert-and-dns.md)."
  type        = "String"
  value       = var.domain != "" ? "https://${local.fqdn}/api/jira/callback" : "PLACEHOLDER_CONFIGURE_ME"

  tags = {
    Purpose = "atlassian-oauth"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "jira_integration_lambda_name" {
  description = "Name of the Jira integration Lambda function"
  value       = try(module.jira_integration_lambda[0].lambda_function_name, null)
}

output "jira_integration_lambda_arn" {
  description = "ARN of the Jira integration Lambda function"
  value       = try(module.jira_integration_lambda[0].lambda_function_arn, null)
}

output "jira_integration_url" {
  description = "Lambda Function URL for Jira integration"
  value       = try(aws_lambda_function_url.jira_integration[0].function_url, null)
}

output "jira_sync_queue_url" {
  description = "SQS queue URL for Jira sync jobs"
  value       = try(aws_sqs_queue.jira_sync[0].url, null)
}

output "jira_sync_lambda_name" {
  description = "Name of the Jira sync Lambda function"
  value       = try(module.jira_sync_lambda[0].lambda_function_name, null)
}

output "jira_sync_lambda_arn" {
  description = "ARN of the Jira sync Lambda function"
  value       = try(module.jira_sync_lambda[0].lambda_function_arn, null)
}

output "jira_scheduler_lambda_name" {
  description = "Name of the Jira scheduler Lambda function"
  value       = try(module.jira_scheduler_lambda[0].lambda_function_name, null)
}

output "jira_scheduler_lambda_arn" {
  description = "ARN of the Jira scheduler Lambda function"
  value       = try(module.jira_scheduler_lambda[0].lambda_function_arn, null)
}
