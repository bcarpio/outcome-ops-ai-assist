# ============================================================================
# Database Integration Lambda
# Handles connection management for the database structured-data source
# (Postgres / MySQL / MS SQL Server) via the Databao Context Engine.
# ============================================================================

module "database_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-database-integration"
  description   = "Database connection management + introspection picker for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/database-integration"
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

  # Integration Lambda needs Databao for the schema picker
  # (list_schemas calls INFORMATION_SCHEMA on the customer DB).
  layers = [
    module.databao_layer[0].lambda_layer_arn,
  ]

  environment_variables = {
    ENV                       = var.environment
    APP_NAME                  = var.app_name
    WORKSPACES_TABLE          = module.workspaces_table[0].dynamodb_table_id
    DB_CREDENTIAL_KMS_KEY_ARN = aws_kms_key.db_credentials[0].arn
    DATABASE_SYNC_QUEUE       = aws_sqs_queue.database_sync[0].url
    DELETE_QUEUE              = aws_sqs_queue.delete_queue[0].url
    AUDIT_TABLE               = module.audit_logs_table.dynamodb_table_id
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB for connections and credentials
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

    # KMS for credential encryption/decryption
    kms_db_credentials = {
      effect = "Allow"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey*"
      ]
      resources = [
        aws_kms_key.db_credentials[0].arn
      ]
    }

    # SQS for sync and delete queues
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.database_sync[0].arn,
        aws_sqs_queue.delete_queue[0].arn
      ]
    }

    # DynamoDB audit log writes
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn
      ]
    }
  }

  tags = {
    Purpose = "database-integration"
  }
}

# Lambda Function URL with IAM auth
resource "aws_lambda_function_url" "database_integration" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.database_integration_lambda[0].lambda_function_name
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
resource "aws_ssm_parameter" "database_integration_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/database-integration-url"
  description = "Lambda Function URL for database integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.database_integration[0].function_url

  tags = {
    Purpose = "database-integration-url"
  }
}

# ============================================================================
# SQS Queue for Database Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "database_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-database-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.database_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "database-sync-queue"
  }
}

resource "aws_sqs_queue" "database_sync_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-database-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "database-sync-dlq"
  }
}

# ============================================================================
# Database Sync Lambda
# Processes SQS messages to introspect databases via Databao + queue
# per-table YAML chunks to file-ingestion.
# ============================================================================

module "database_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-database-sync"
  description   = "Database introspection sync via Databao for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large warehouses
  memory_size   = 2048

  source_path = [
    {
      path             = "${path.module}/../lambda/database-sync"
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

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  quiet_archive_local_exec = true

  # ADR-019: attach OutcomeOps license validator + Databao layer.
  layers = [
    var.outcomeops_license_layer_arn,
    module.databao_layer[0].lambda_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV                       = var.environment
      APP_NAME                  = var.app_name
      WORKSPACES_TABLE          = module.workspaces_table[0].dynamodb_table_id
      DB_CREDENTIAL_KMS_KEY_ARN = aws_kms_key.db_credentials[0].arn
      FILE_INGESTION_QUEUE      = aws_sqs_queue.file_ingestion[0].url
      DATABASE_SYNC_QUEUE       = aws_sqs_queue.database_sync[0].url
      DELETE_QUEUE              = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE               = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
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

      kms_db_credentials = {
        effect = "Allow"
        actions = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        resources = [
          aws_kms_key.db_credentials[0].arn
        ]
      }

      sqs_receive = {
        effect = "Allow"
        actions = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        resources = [
          aws_sqs_queue.database_sync[0].arn
        ]
      }

      sqs_send = {
        effect = "Allow"
        actions = [
          "sqs:SendMessage"
        ]
        resources = [
          aws_sqs_queue.file_ingestion[0].arn,
          aws_sqs_queue.database_sync[0].arn,
          aws_sqs_queue.delete_queue[0].arn
        ]
      }

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
    Purpose = "database-sync"
  }
}

# SQS trigger for sync Lambda
resource "aws_lambda_event_source_mapping" "database_sync" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.database_sync[0].arn
  function_name    = module.database_sync_lambda[0].lambda_function_name

  batch_size                         = 1 # Process one sync at a time
  maximum_batching_window_in_seconds = 0 # Process immediately

  scaling_config {
    maximum_concurrency = 5
  }
}

# ============================================================================
# Database Sync Scheduler Lambda
# Runs hourly to fan out sync jobs for all active connections
# ============================================================================

module "database_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-database-scheduler"
  description   = "Hourly scheduler for database sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    {
      path             = "${path.module}/../lambda/database-scheduler"
      pip_requirements = false
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
      ]
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  quiet_archive_local_exec = true

  environment_variables = {
    ENV                 = var.environment
    APP_NAME            = var.app_name
    WORKSPACES_TABLE    = module.workspaces_table[0].dynamodb_table_id
    DATABASE_SYNC_QUEUE = aws_sqs_queue.database_sync[0].url
  }

  attach_policy_statements = true
  policy_statements = {
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn
      ]
    }

    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = [
        aws_sqs_queue.database_sync[0].arn
      ]
    }
  }

  tags = {
    Purpose = "database-scheduler"
  }
}

# EventBridge rule for hourly sync
resource "aws_cloudwatch_event_rule" "database_hourly_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                = "${var.environment}-${var.app_name}-database-hourly-sync"
  description         = "Trigger database sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "database-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "database_scheduler" {
  count = var.enable_workspaces ? 1 : 0

  rule      = aws_cloudwatch_event_rule.database_hourly_sync[0].name
  target_id = "database-scheduler"
  arn       = module.database_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "database_scheduler_eventbridge" {
  count = var.enable_workspaces ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.database_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.database_hourly_sync[0].arn
}

# ============================================================================
# Outputs
# ============================================================================

output "database_integration_lambda_name" {
  description = "Name of the database integration Lambda function"
  value       = var.enable_workspaces ? module.database_integration_lambda[0].lambda_function_name : null
}

output "database_integration_lambda_arn" {
  description = "ARN of the database integration Lambda function"
  value       = var.enable_workspaces ? module.database_integration_lambda[0].lambda_function_arn : null
}

output "database_integration_url" {
  description = "Lambda Function URL for database integration"
  value       = var.enable_workspaces ? aws_lambda_function_url.database_integration[0].function_url : null
}

output "database_sync_queue_url" {
  description = "SQS queue URL for database sync jobs"
  value       = var.enable_workspaces ? aws_sqs_queue.database_sync[0].url : null
}

output "database_sync_lambda_name" {
  description = "Name of the database sync Lambda function"
  value       = var.enable_workspaces ? module.database_sync_lambda[0].lambda_function_name : null
}

output "database_sync_lambda_arn" {
  description = "ARN of the database sync Lambda function"
  value       = var.enable_workspaces ? module.database_sync_lambda[0].lambda_function_arn : null
}

output "database_scheduler_lambda_name" {
  description = "Name of the database scheduler Lambda function"
  value       = var.enable_workspaces ? module.database_scheduler_lambda[0].lambda_function_name : null
}

output "database_scheduler_lambda_arn" {
  description = "ARN of the database scheduler Lambda function"
  value       = var.enable_workspaces ? module.database_scheduler_lambda[0].lambda_function_arn : null
}
