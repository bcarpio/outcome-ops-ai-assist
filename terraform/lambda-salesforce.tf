# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Salesforce CRM Integration Lambda
# Handles OAuth flow (env-level SSM credentials, one shared Connected App per
# deploy -- Salesforce Connected Apps can be "Available for use by all
# users") and connection management for workspace admins connecting any
# Salesforce org (prod or sandbox).
# ============================================================================

locals {
  salesforce_client_secret_ssm_name = "/${var.environment}/${var.app_name}/oauth/salesforce/client-secret"
  salesforce_common_env = {
    SALESFORCE_CLIENT_ID               = var.salesforce_oauth_client_id
    SALESFORCE_CLIENT_SECRET_SSM_PARAM = local.salesforce_client_secret_ssm_name
  }
}

module "salesforce_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-salesforce-integration"
  description   = "Salesforce OAuth + connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/salesforce-integration"
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


  environment_variables = merge(
    {
      ENV                          = var.environment
      APP_NAME                     = var.app_name
      WORKSPACES_TABLE             = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN            = aws_kms_key.oauth_tokens[0].arn
      SALESFORCE_SYNC_QUEUE        = aws_sqs_queue.salesforce_sync[0].url
      DELETE_QUEUE                 = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE                  = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS         = tostring(var.audit_retention_days)
      SALESFORCE_STATE_TTL_SECONDS = "600"
      # UI-facing callback URL used to build the Salesforce authorize
      # redirect. Empty when the deploy has no custom domain configured yet
      # (early bring-up) -- the handler treats "" as "misconfigured" and
      # returns a 500 with a diagnostic message.
      SALESFORCE_CALLBACK_URL = var.domain != "" ? "https://${local.fqdn}/api/salesforce/callback" : ""
    },
    local.salesforce_common_env,
    local.ha_writer_env,
    local.ha_scheduler_env,
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
          "dynamodb:DeleteItem",
          "dynamodb:Query"
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      }

      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = local.oauth_kms_token_key_arns
      }

      kms_ssm = {
        effect  = "Allow"
        actions = ["kms:Decrypt"]
        resources = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      }

      ssm_read = {
        effect  = "Allow"
        actions = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/salesforce/*"
        ]
      }

      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.salesforce_sync[0].arn,
          aws_sqs_queue.delete_queue[0].arn
        ]
      }

      dynamodb_audit = {
        effect  = "Allow"
        actions = ["dynamodb:PutItem"]
        resources = [
          module.audit_logs_table.dynamodb_table_arn
        ]
      }
    },
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "salesforce-integration"
  }
}

resource "aws_lambda_function_url" "salesforce_integration" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  function_name      = module.salesforce_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "salesforce_integration_url" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/salesforce-integration-url"
  description = "Lambda Function URL for Salesforce integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.salesforce_integration[0].function_url

  tags = {
    Purpose = "salesforce-integration-url"
  }
}

# ============================================================================
# SQS Queue for Salesforce Sync Jobs
# ============================================================================

resource "aws_sqs_queue" "salesforce_sync" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-salesforce-sync"
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.salesforce_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "salesforce-sync-queue"
  }
}

resource "aws_sqs_queue" "salesforce_sync_dlq" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-salesforce-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "salesforce-sync-dlq"
  }
}

# ============================================================================
# Salesforce Sync Lambda
# Processes SQS messages to walk Account/Contact/Opportunity + attached files
# (ContentVersion) with embedded recent activities (Task/Event), and fans
# into file-ingestion for S3 upload + downstream vector indexing.
# ============================================================================

module "salesforce_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-salesforce-sync"
  description   = "Cursor-driven Salesforce CRM sync -> file-ingestion queue"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    {
      path             = "${path.module}/../lambda/salesforce-sync"
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


  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN    = aws_kms_key.oauth_tokens[0].arn
      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion[0].url
      DELETE_QUEUE         = aws_sqs_queue.delete_queue[0].url
      # Continuation: sync worker self-requeues on cursor advance so a
      # long walk doesn't hold one SQS message past the visibility timeout.
      SALESFORCE_SYNC_QUEUE = aws_sqs_queue.salesforce_sync[0].url
      KB_BUCKET             = module.knowledge_base_bucket.s3_bucket_id
      AUDIT_TABLE           = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS  = tostring(var.audit_retention_days)
    },
    local.salesforce_common_env,
    local.ha_writer_env,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      }

      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = local.oauth_kms_token_key_arns
      }

      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }

      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/salesforce/*"]
      }

      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.salesforce_sync[0].arn]
      }

      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # Own queue for continuation self-requeue; file-ingestion + delete
        # queue for downstream fan-out.
        resources = [
          aws_sqs_queue.file_ingestion[0].arn,
          aws_sqs_queue.delete_queue[0].arn,
          aws_sqs_queue.salesforce_sync[0].arn,
        ]
      }

      # Orphan cleanup lists S3 keys under the Salesforce prefix to diff
      # against the enumerated record-id set.
      s3_kb_list = {
        effect    = "Allow"
        actions   = ["s3:ListBucket"]
        resources = [module.knowledge_base_bucket.s3_bucket_arn]
      }

      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table.dynamodb_table_arn]
      }
    },
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "salesforce-sync"
  }
}

resource "aws_lambda_event_source_mapping" "salesforce_sync" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  event_source_arn = aws_sqs_queue.salesforce_sync[0].arn
  function_name    = module.salesforce_sync_lambda[0].lambda_function_name

  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  # Partial-batch-failure contract: handler returns batchItemFailures for
  # records that failed. Without this SQS deletes every record on 200.
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ============================================================================
# Salesforce Scheduler Lambda
# Runs hourly at :15 (staggered off ServiceNow's :45 and top-of-hour) to
# enqueue one sync job per active connection.
# ============================================================================

module "salesforce_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-salesforce-scheduler"
  description   = "Hourly scheduler for Salesforce sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    {
      path             = "${path.module}/../lambda/salesforce-scheduler"
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

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  quiet_archive_local_exec = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    WORKSPACES_TABLE      = module.workspaces_table[0].dynamodb_table_id
    SALESFORCE_SYNC_QUEUE = aws_sqs_queue.salesforce_sync[0].url
    },
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect  = "Allow"
      actions = ["dynamodb:Scan", "dynamodb:Query"]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.salesforce_sync[0].arn]
    }
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "salesforce-scheduler"
  }
}

resource "aws_cloudwatch_event_rule" "salesforce_hourly_sync" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  name        = "${var.environment}-${var.app_name}-salesforce-hourly-sync"
  description = "Trigger Salesforce sync scheduler hourly at :15 (staggered off ServiceNow's :45 and the top-of-hour crowd)"
  # cron(minute hour day-of-month month day-of-week year)
  schedule_expression = "cron(15 * * * ? *)"

  tags = {
    Purpose = "salesforce-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "salesforce_scheduler" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  rule      = aws_cloudwatch_event_rule.salesforce_hourly_sync[0].name
  target_id = "salesforce-scheduler"
  arn       = module.salesforce_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "salesforce_scheduler_eventbridge" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.salesforce_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.salesforce_hourly_sync[0].arn
}

# ============================================================================
# SSM Parameter Placeholder for OAuth client_secret.
# Operator must populate this out-of-band (aws ssm put-parameter --overwrite).
# terraform creates a placeholder value on first apply and then ignores
# subsequent value changes so re-applies don't stomp the real credential.
# ============================================================================

resource "aws_ssm_parameter" "salesforce_client_secret" {
  count = var.enable_workspaces && var.enable_salesforce_integration ? 1 : 0

  name        = local.salesforce_client_secret_ssm_name
  description = "Salesforce Connected App consumer_secret. Operator populates via aws ssm put-parameter --overwrite; terraform ignores value changes."
  type        = "SecureString"
  value       = "PLACEHOLDER_CONFIGURE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "salesforce-oauth"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "salesforce_integration_lambda_name" {
  description = "Name of the Salesforce integration Lambda function"
  value       = try(module.salesforce_integration_lambda[0].lambda_function_name, null)
}

output "salesforce_integration_lambda_arn" {
  description = "ARN of the Salesforce integration Lambda function"
  value       = try(module.salesforce_integration_lambda[0].lambda_function_arn, null)
}

output "salesforce_integration_url" {
  description = "Lambda Function URL for Salesforce integration"
  value       = try(aws_lambda_function_url.salesforce_integration[0].function_url, null)
}

output "salesforce_sync_queue_url" {
  description = "URL of the Salesforce sync queue"
  value       = try(aws_sqs_queue.salesforce_sync[0].url, null)
}
