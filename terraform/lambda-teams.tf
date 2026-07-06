# ============================================================================
# Teams Integration Lambda
# Handles OAuth flow and connection management for Microsoft Teams
# ============================================================================

module "teams_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-teams-integration"
  description   = "Microsoft Teams OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/teams-integration"
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
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn
      TEAMS_SYNC_QUEUE  = aws_sqs_queue.teams_sync[0].url
      DELETE_QUEUE      = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE       = module.audit_logs_table.dynamodb_table_id
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
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"
        ]
      }

      # KMS for token encryption/decryption. Includes both the
      # primary MRK ARN and (when HA is enabled) the secondary
      # replica ARN. The multi-region key material is shared, but
      # IAM requires the ARN of the specific key the ciphertext
      # was minted against -- so a token encrypted in the passive
      # region needs the replica ARN on the decrypt side too.
      kms_oauth = {
        effect = "Allow"
        actions = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        resources = concat(
          [aws_kms_key.oauth_tokens[0].arn],
          var.secondary_region != "" ? [aws_kms_replica_key.oauth_tokens_secondary[0].arn] : []
        )
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
          aws_sqs_queue.teams_sync[0].arn,
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
    Purpose = "teams-integration"
  }
}

# Lambda Function URL with IAM auth
resource "aws_lambda_function_url" "teams_integration" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  function_name      = module.teams_integration_lambda[0].lambda_function_name
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
resource "aws_ssm_parameter" "teams_integration_url" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/teams-integration-url"
  description = "Lambda Function URL for Teams integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.teams_integration[0].function_url

  tags = {
    Purpose = "teams-integration-url"
  }
}

# ============================================================================
# SQS Queue for Teams Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "teams_sync" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-teams-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.teams_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "teams-sync-queue"
  }
}

resource "aws_sqs_queue" "teams_sync_dlq" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-teams-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "teams-sync-dlq"
  }
}

# ============================================================================
# Teams Sync Lambda
# Processes SQS messages to sync Teams messages to knowledge base
# ============================================================================

module "teams_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-teams-sync"
  description   = "Syncs Teams messages to knowledge base for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large syncs
  memory_size   = 1024

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/teams-sync"
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

  # ADR-019: attach OutcomeOps license validator layer.
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
      TEAMS_SYNC_QUEUE     = aws_sqs_queue.teams_sync[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
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

      # KMS for token encryption/decryption. Includes both the
      # primary MRK ARN and (when HA is enabled) the secondary
      # replica ARN. The multi-region key material is shared, but
      # IAM requires the ARN of the specific key the ciphertext
      # was minted against -- so a token encrypted in the passive
      # region needs the replica ARN on the decrypt side too.
      kms_oauth = {
        effect = "Allow"
        actions = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        resources = concat(
          [aws_kms_key.oauth_tokens[0].arn],
          var.secondary_region != "" ? [aws_kms_replica_key.oauth_tokens_secondary[0].arn] : []
        )
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
          aws_sqs_queue.teams_sync[0].arn
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
          aws_sqs_queue.teams_sync[0].arn,
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
    Purpose = "teams-sync"
  }
}

# SQS trigger for sync Lambda
resource "aws_lambda_event_source_mapping" "teams_sync" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  event_source_arn = aws_sqs_queue.teams_sync[0].arn
  function_name    = module.teams_sync_lambda[0].lambda_function_name

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
# Teams Sync Scheduler Lambda
# Runs hourly to fan out sync jobs for all active connections
# ============================================================================

module "teams_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-teams-scheduler"
  description   = "Hourly scheduler for Teams sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  # Source code
  source_path = [
    {
      path             = "${path.module}/../lambda/teams-scheduler"
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
    TEAMS_SYNC_QUEUE = aws_sqs_queue.teams_sync[0].url
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
        aws_sqs_queue.teams_sync[0].arn
      ]
    }
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "teams-scheduler"
  }
}

# EventBridge rule for hourly sync
resource "aws_cloudwatch_event_rule" "teams_hourly_sync" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  name                = "${var.environment}-${var.app_name}-teams-hourly-sync"
  description         = "Trigger Teams sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "teams-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "teams_scheduler" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  rule      = aws_cloudwatch_event_rule.teams_hourly_sync[0].name
  target_id = "teams-scheduler"
  arn       = module.teams_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "teams_scheduler_eventbridge" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.teams_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.teams_hourly_sync[0].arn
}

# ============================================================================
# SSM Parameter for Teams Callback URL
# ============================================================================

resource "aws_ssm_parameter" "microsoft_teams_callback_url" {
  count = var.enable_workspaces && var.enable_teams_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/oauth/microsoft/teams-callback-url"
  description = "OAuth callback URL for Teams. Register this in your Azure AD app registration after local.fqdn resolves to the UI ALB (whether via terraform-managed Route53 or external DNS - see docs/byo-cert-and-dns.md)."
  type        = "String"
  value       = var.domain != "" ? "https://${local.fqdn}/api/teams/callback" : "PLACEHOLDER_CONFIGURE_ME"

  tags = {
    Purpose = "microsoft-oauth"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "teams_integration_lambda_name" {
  description = "Name of the Teams integration Lambda function"
  value       = try(module.teams_integration_lambda[0].lambda_function_name, null)
}

output "teams_integration_lambda_arn" {
  description = "ARN of the Teams integration Lambda function"
  value       = try(module.teams_integration_lambda[0].lambda_function_arn, null)
}

output "teams_integration_url" {
  description = "Lambda Function URL for Teams integration"
  value       = try(aws_lambda_function_url.teams_integration[0].function_url, null)
}

output "teams_sync_queue_url" {
  description = "SQS queue URL for Teams sync jobs"
  value       = try(aws_sqs_queue.teams_sync[0].url, null)
}

output "teams_sync_lambda_name" {
  description = "Name of the Teams sync Lambda function"
  value       = try(module.teams_sync_lambda[0].lambda_function_name, null)
}

output "teams_sync_lambda_arn" {
  description = "ARN of the Teams sync Lambda function"
  value       = try(module.teams_sync_lambda[0].lambda_function_arn, null)
}

output "teams_scheduler_lambda_name" {
  description = "Name of the Teams scheduler Lambda function"
  value       = try(module.teams_scheduler_lambda[0].lambda_function_name, null)
}

output "teams_scheduler_lambda_arn" {
  description = "ARN of the Teams scheduler Lambda function"
  value       = try(module.teams_scheduler_lambda[0].lambda_function_arn, null)
}
