# ============================================================================
# Azure DevOps Boards integration -- secondary region (HA mode)
#
# Mirrors terraform/lambda-azure-devops-boards.tf under the aws.secondary
# provider. Ships together in one file (unlike the Repos side, which is
# split across lambda-integrations-secondary / lambda-sync-workers-secondary
# / lambda-schedulers-secondary) so the Boards HA surface stays legible
# next to its primary in the review diff.
#
# Every resource is guarded on ``var.enable_workspaces && var.secondary_region != ""``
# so single-region deploys pay nothing.
# ============================================================================

# ---------------------------------------------------------------------------
# Secondary SQS queue + DLQ for Boards sync
# ---------------------------------------------------------------------------

resource "aws_sqs_queue" "azure_devops_boards_sync_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                              = "${var.environment}-${var.app_name}-azure-devops-boards-sync"
  visibility_timeout_seconds        = 960
  message_retention_seconds         = 86400
  kms_master_key_id                 = local.data_cmk_arn_secondary
  kms_data_key_reuse_period_seconds = 300
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.azure_devops_boards_sync_dlq_secondary[0].arn
    maxReceiveCount     = 3
  })

  tags = { Purpose = "azure-devops-boards-sync-queue" }
}

resource "aws_sqs_queue" "azure_devops_boards_sync_dlq_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                              = "${var.environment}-${var.app_name}-azure-devops-boards-sync-dlq"
  message_retention_seconds         = 1209600
  kms_master_key_id                 = local.data_cmk_arn_secondary
  kms_data_key_reuse_period_seconds = 300

  tags = { Purpose = "azure-devops-boards-sync-dlq" }
}

# ---------------------------------------------------------------------------
# Boards Integration Lambda (secondary)
# ---------------------------------------------------------------------------

module "azure_devops_boards_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-integration"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-boards-integration-secondary"
  description   = "Azure DevOps Boards OAuth + connection/project CRUD (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-boards-integration"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id

      # OAuth token encryption via the secondary MRK replica.
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param
      # Same derived value as the primary (see the primary TF for the
      # rationale).
      AZURE_DEVOPS_BOARDS_REDIRECT_URI        = "https://${local.fqdn}/api/azure-devops-boards/callback"
      AZURE_DEVOPS_BOARDS_INTEGRATION_ENABLED = tostring(var.azure_devops_boards_integration_enabled)

      AZURE_DEVOPS_BOARDS_SYNC_QUEUE = aws_sqs_queue.azure_devops_boards_sync_secondary[0].url
      DELETE_QUEUE                   = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE                    = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_secret = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}"]
      }
      # Same both-ARN grant as the Repos secondary integration.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.azure_devops_boards_sync_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "azure-devops-boards-integration" }
}

resource "aws_lambda_function_url" "azure_devops_boards_integration_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.azure_devops_boards_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "azure_devops_boards_integration_url_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/azure-devops-boards-integration-url"
  description = "Lambda Function URL for Azure DevOps Boards integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.azure_devops_boards_integration_secondary[0].function_url

  tags = { Purpose = "azure-devops-boards-integration" }
}

output "azure_devops_boards_integration_url_secondary" {
  description = "Lambda Function URL for Azure DevOps Boards integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.azure_devops_boards_integration_secondary[0].function_url : null
}

# ---------------------------------------------------------------------------
# Boards Sync Worker Lambda (secondary)
# ---------------------------------------------------------------------------

module "azure_devops_boards_sync_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-sync"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-boards-sync-secondary"
  description   = "Azure DevOps Boards work-item sync (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/azure-devops-boards-sync", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

      FILE_INGESTION_QUEUE = aws_sqs_queue.file_ingestion_secondary[0].url
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      ssm_secret = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}"]
      }
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.azure_devops_boards_sync_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.file_ingestion_secondary[0].arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "azure-devops-boards-sync" }
}

resource "aws_lambda_event_source_mapping" "azure_devops_boards_sync_secondary" {
  count                   = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider                = aws.secondary
  event_source_arn        = aws_sqs_queue.azure_devops_boards_sync_secondary[0].arn
  function_name           = module.azure_devops_boards_sync_lambda_secondary[0].lambda_function_name
  batch_size              = 1
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

# ---------------------------------------------------------------------------
# Boards Scheduler Lambda (secondary)
# ---------------------------------------------------------------------------

module "azure_devops_boards_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count     = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  providers = { aws = aws.secondary }

  function_name = "${var.environment}-${var.app_name}-azure-devops-boards-scheduler"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-boards-scheduler-secondary"
  description   = "Hourly Boards sync fan-out (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/azure-devops-boards-scheduler", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc", "!\\.pytest_cache/.*"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id

      AZURE_DEVOPS_BOARDS_SYNC_QUEUE = aws_sqs_queue.azure_devops_boards_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_scan = {
        effect    = "Allow"
        actions   = ["dynamodb:Scan"]
        resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.azure_devops_boards_sync_secondary[0].arn]
      }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "azure-devops-boards-scheduler" }
}

resource "aws_cloudwatch_event_rule" "azure_devops_boards_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-azure-devops-boards-hourly-sync"
  description         = "Hourly Boards sync scheduler trigger (secondary region; is_active_region gates the actual work)"
  schedule_expression = "rate(1 hour)"

  tags = { Purpose = "azure-devops-boards-scheduler-trigger" }
}

resource "aws_cloudwatch_event_target" "azure_devops_boards_scheduler_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule = aws_cloudwatch_event_rule.azure_devops_boards_hourly_sync_secondary[0].name
  arn  = module.azure_devops_boards_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "azure_devops_boards_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.azure_devops_boards_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.azure_devops_boards_hourly_sync_secondary[0].arn
}
