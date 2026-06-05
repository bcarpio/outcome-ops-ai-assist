# ============================================================================
# Azure DevOps Integration Lambda
#
# Handles Entra ID OAuth + connection / repo management for ADO repos.
# The sync worker + hourly scheduler are added in subsequent commits as
# the work for those chunks ships.
#
# Two notes worth keeping in mind when editing this file:
#
# 1. The integration is OFF by default. ``var.azure_devops_integration_enabled``
#    plus all four config values being populated is required for the Lambda
#    to do anything useful at runtime; the handler short-circuits with 404
#    if the flag is false. Customers without ADO leave the flag false and
#    pay no operational tax.
#
# 2. Single-tenant Entra ID. ``var.azure_devops_tenant_id`` is the customer's
#    tenant. The handler builds the authorize URL with that tenant -- NOT
#    ``/common/``, which Entra ID rejects for single-tenant app registrations.
#
# 3. SQS queue + DLQ are defined here because the integration Lambda must be
#    able to write to the sync queue on add-repo. The sync worker that
#    *consumes* from the queue + the hourly scheduler that fans out periodic
#    sync jobs are added in subsequent commits to this file.
# ============================================================================

module "azure_devops_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-azure-devops-integration"
  description   = "Azure DevOps OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-integration"
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
  quiet_archive_local_exec          = true

  # ADR-019: attach OutcomeOps license validator layer so the snapshot
  # helper (``_snapshot_active_repos``) has the layer's symbol available.
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param
      AZURE_DEVOPS_REDIRECT_URI            = var.azure_devops_redirect_uri
      AZURE_DEVOPS_INTEGRATION_ENABLED     = tostring(var.azure_devops_integration_enabled)

      AZURE_DEVOPS_SYNC_QUEUE = aws_sqs_queue.azure_devops_sync[0].url
      DELETE_QUEUE            = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE             = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
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

      # SSM for the customer-managed client secret. Scope to the exact
      # parameter name (not a wildcard) -- the operator created the
      # SecureString, and the integration is the only consumer.
      ssm_secret = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}",
        ]
      }

      # KMS for token encryption/decryption at rest in DDB.
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

      # KMS for the SSM SecureString that holds the client secret.
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
          aws_sqs_queue.azure_devops_sync[0].arn,
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
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "azure-devops-integration"
  }
}

resource "aws_lambda_function_url" "azure_devops_integration" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.azure_devops_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "azure_devops_integration_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/azure-devops-integration-url"
  description = "Lambda Function URL for Azure DevOps integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.azure_devops_integration[0].function_url

  tags = {
    Purpose = "azure-devops-integration-url"
  }
}

# ============================================================================
# SQS queue + DLQ for ADO sync jobs.
# Defined here because the integration Lambda must write to the queue on
# add-repos. The sync worker that *consumes* from the queue is added in a
# subsequent commit; the scheduler that fans out hourly sync jobs is added
# alongside that.
# ============================================================================

resource "aws_sqs_queue" "azure_devops_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-azure-devops-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.azure_devops_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "azure-devops-sync-queue"
  }
}

resource "aws_sqs_queue" "azure_devops_sync_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-azure-devops-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "azure-devops-sync-dlq"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "azure_devops_integration_lambda_name" {
  description = "Name of the Azure DevOps integration Lambda function"
  value       = var.enable_workspaces ? module.azure_devops_integration_lambda[0].lambda_function_name : null
}

output "azure_devops_integration_lambda_arn" {
  description = "ARN of the Azure DevOps integration Lambda function"
  value       = var.enable_workspaces ? module.azure_devops_integration_lambda[0].lambda_function_arn : null
}

output "azure_devops_integration_url" {
  description = "Lambda Function URL for Azure DevOps integration"
  value       = var.enable_workspaces ? aws_lambda_function_url.azure_devops_integration[0].function_url : null
}

output "azure_devops_sync_queue_url" {
  description = "SQS queue URL for Azure DevOps sync jobs"
  value       = var.enable_workspaces ? aws_sqs_queue.azure_devops_sync[0].url : null
}

output "azure_devops_sync_queue_arn" {
  description = "SQS queue ARN for Azure DevOps sync jobs (consumed in a subsequent commit)"
  value       = var.enable_workspaces ? aws_sqs_queue.azure_devops_sync[0].arn : null
}
