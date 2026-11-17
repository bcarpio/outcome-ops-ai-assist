# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Azure DevOps Boards Integration Lambda
#
# Handles Entra ID OAuth + connection / project management for ADO work items.
# The sync worker + hourly scheduler are added in chunks 05-06 as separate
# resources in this file (mirroring the Repos integration file's layout).
#
# Notes worth keeping in mind when editing this file:
#
# 1. The integration is OFF by default. ``var.enable_azure_devops_boards_integration``
#    gates the ENTIRE terraform surface -- integration Lambda + Function URL
#    + sync + scheduler + SQS + DLQ + EventBridge + IAM. Customers without
#    Boards leave the flag false and provision none of it.
#
# 2. Reuses the Repos integration's tenant/client-id/client-secret tfvars.
#    Customer registers ONE Entra ID app for both integrations; the app must
#    grant BOTH vso.code (Repos) AND vso.work (Boards) API permissions AND
#    register BOTH redirect URIs (/api/azure-devops/callback for Repos and
#    /api/azure-devops-boards/callback for Boards).
#
# 3. The KMS grant on the OAuth-tokens key includes BOTH the primary MRK
#    ARN and (when HA is enabled) the secondary replica ARN. Same pattern
#    as the Repos integration -- prevents cross-region decrypt failures on
#    tokens minted against either regional ARN.
# ============================================================================

module "azure_devops_boards_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-integration"
  description   = "Azure DevOps Boards (work items) OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-boards-integration"
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
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true


  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn

      # Reused from the Repos integration tfvars -- one Entra ID app
      # authenticates both integrations against the customer's tenant.
      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

      # Boards-specific. Redirect URI derived from the deploy's FQDN
      # -- customer registers the same value in Entra ID (see runbook
      # §12.2) but doesn't type it in tfvars twice.
      AZURE_DEVOPS_BOARDS_REDIRECT_URI = "https://${local.fqdn}/api/azure-devops-boards/callback"

      AZURE_DEVOPS_BOARDS_SYNC_QUEUE = aws_sqs_queue.azure_devops_boards_sync[0].url
      DELETE_QUEUE                   = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE                    = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS           = tostring(var.audit_retention_days)
    },
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

      ssm_secret = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}",
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

      kms_ssm = {
        effect = "Allow"
        actions = [
          "kms:Decrypt"
        ]
        resources = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      }

      sqs_send = {
        effect = "Allow"
        actions = [
          "sqs:SendMessage"
        ]
        resources = [
          aws_sqs_queue.azure_devops_boards_sync[0].arn,
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
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "azure-devops-boards-integration"
  }
}

resource "aws_lambda_function_url" "azure_devops_boards_integration" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  function_name      = module.azure_devops_boards_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "azure_devops_boards_integration_url" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/azure-devops-boards-integration-url"
  description = "Lambda Function URL for Azure DevOps Boards integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.azure_devops_boards_integration[0].function_url

  tags = {
    Purpose = "azure-devops-boards-integration-url"
  }
}

# ============================================================================
# SQS queue + DLQ for Boards sync jobs.
# Defined here because the integration Lambda writes to the queue on
# add-projects. The sync worker that consumes from the queue is added in
# ; the scheduler that fans out hourly sync jobs is added in .
# ============================================================================

resource "aws_sqs_queue" "azure_devops_boards_sync" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-azure-devops-boards-sync"
  visibility_timeout_seconds = 960
  message_retention_seconds  = 86400
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.azure_devops_boards_sync_dlq[0].arn
    maxReceiveCount     = 3
  })
  kms_master_key_id                 = local.data_cmk_arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Purpose = "azure-devops-boards-sync-queue"
  }
}

resource "aws_sqs_queue" "azure_devops_boards_sync_dlq" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  name                              = "${var.environment}-${var.app_name}-azure-devops-boards-sync-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = local.data_cmk_arn
  kms_data_key_reuse_period_seconds = 300

  tags = {
    Purpose = "azure-devops-boards-sync-dlq"
  }
}

# ============================================================================
# Boards Sync Worker Lambda
# ============================================================================

module "azure_devops_boards_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-sync"
  description   = "Per-project WIQL fetch + work-item ingest for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes -- initial sync of up to 5000 work items
  memory_size   = 1024

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-boards-sync"
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
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true


  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion[0].url
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
    },
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
          "dynamodb:Query"
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      }

      ssm_secret = {
        effect  = "Allow"
        actions = ["ssm:GetParameter"]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}",
        ]
      }

      # KMS on both primary + secondary replica ARNs (multi-region
      # key material shared, but IAM permissions are ARN-specific --
      # same pattern as commit 389c1e7).
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = concat(
          [aws_kms_key.oauth_tokens[0].arn],
          var.secondary_region != "" ? [aws_kms_replica_key.oauth_tokens_secondary[0].arn] : []
        )
      }

      kms_ssm = {
        effect  = "Allow"
        actions = ["kms:Decrypt"]
        resources = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      }

      sqs_receive = {
        effect = "Allow"
        actions = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes",
        ]
        resources = [aws_sqs_queue.azure_devops_boards_sync[0].arn]
      }

      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.file_ingestion[0].arn]
      }

      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table.dynamodb_table_arn]
      }
    },
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "azure-devops-boards-sync"
  }
}

resource "aws_lambda_event_source_mapping" "azure_devops_boards_sync" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  event_source_arn = aws_sqs_queue.azure_devops_boards_sync[0].arn
  function_name    = module.azure_devops_boards_sync_lambda[0].lambda_function_name

  # Batch size 1 -- one project sync per invocation. Mirrors jira-sync.
  batch_size = 1

  function_response_types = ["ReportBatchItemFailures"]

  # (HA parity): mirror secondary's maximum_concurrency = 5.
  # Without a cap, a scheduler-driven backlog would burst-invoke against
  # Boards' per-project ADO API rate limit -- primary was unbounded, the
  # secondary was already correct.
  scaling_config {
    maximum_concurrency = 5
  }
}

# ============================================================================
# Boards Scheduler Lambda
#
# Hourly EventBridge trigger. Scans workspace-projects table, enqueues
# a sync message per enabled AZDO_BOARDS_PROJECT# row. Active-region
# guard prevents double-fires during dual-write windows.
# ============================================================================

module "azure_devops_boards_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-scheduler"
  description   = "Hourly scan-and-enqueue of Boards sync jobs for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-boards-scheduler"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id

      AZURE_DEVOPS_BOARDS_SYNC_QUEUE = aws_sqs_queue.azure_devops_boards_sync[0].url
    },
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_scan = {
        effect    = "Allow"
        actions   = ["dynamodb:Scan"]
        resources = [module.workspaces_table[0].dynamodb_table_arn]
      }

      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.azure_devops_boards_sync[0].arn]
      }
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "azure-devops-boards-scheduler"
  }
}

resource "aws_cloudwatch_event_rule" "azure_devops_boards_hourly_sync" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  name                = "${var.environment}-${var.app_name}-azure-devops-boards-hourly-sync"
  description         = "Hourly Boards sync scheduler trigger"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "azure-devops-boards-scheduler-trigger"
  }
}

resource "aws_cloudwatch_event_target" "azure_devops_boards_scheduler" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  rule = aws_cloudwatch_event_rule.azure_devops_boards_hourly_sync[0].name
  arn  = module.azure_devops_boards_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "azure_devops_boards_scheduler_eventbridge" {
  count = var.enable_workspaces && var.enable_azure_devops_boards_integration ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.azure_devops_boards_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.azure_devops_boards_hourly_sync[0].arn
}
