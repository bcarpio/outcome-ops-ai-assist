# ============================================================================
# Database Integration Lambdas (container-image deployed)
#
# Two Lambdas in the trio ship as container images:
#   - database-integration : UI picker / connection CRUD
#   - database-sync        : SQS-driven schema introspection worker
#
# The scheduler stays zip-based (it's tiny - just a DDB scan + SQS fan-out
# - no introspection engine dependency).
#
# Container-image deployment is required here because the schema introspection
# engine
# plus the Postgres/MySQL/MSSQL driver set totals over 350 MB unzipped, well
# past Lambda's 250 MB unzipped layer cap. Container images allow up to 10
# GB which gives plenty of headroom for future engines (Oracle, SAP HANA).
#
# Image tags are written to SSM by scripts/build-database-images.sh; this
# Terraform reads them at plan time so a `terraform apply` after a build
# picks up the new image automatically.
# ============================================================================

# Read image tags from SSM (set by build-database-images.sh)
data "aws_ssm_parameter" "database_integration_image_tag" {
  count = var.enable_workspaces ? 1 : 0
  name  = "/${var.environment}/${var.app_name}/database-integration/image-tag"
}

data "aws_ssm_parameter" "database_sync_image_tag" {
  count = var.enable_workspaces ? 1 : 0
  name  = "/${var.environment}/${var.app_name}/database-sync/image-tag"
}

# ============================================================================
# Database Integration Lambda (container image)
# ============================================================================

module "database_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-database-integration"
  description   = "Database connection management + introspection picker for OutcomeOps AI Assist"
  timeout       = 60
  memory_size   = 1024
  publish       = true

  # Container image deployment
  create_package = false
  package_type   = "Image"
  image_uri      = "${aws_ecr_repository.database_integration[0].repository_url}:${data.aws_ssm_parameter.database_integration_image_tag[0].value}"
  architectures  = ["x86_64"]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  environment_variables = merge(
    {
      ENV                       = var.environment
      APP_NAME                  = var.app_name
      WORKSPACES_TABLE          = module.workspaces_table[0].dynamodb_table_id
      DB_CREDENTIAL_KMS_KEY_ARN = aws_kms_key.db_credentials[0].arn
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
          "dynamodb:DeleteItem",
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
    Purpose = "database-integration"
  }
}

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
  visibility_timeout_seconds = 900
  message_retention_seconds  = 86400
  receive_wait_time_seconds  = 20

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.database_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "database-sync-queue"
  }
}

resource "aws_sqs_queue" "database_sync_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-database-sync-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "database-sync-dlq"
  }
}

# ============================================================================
# Database Sync Lambda (container image)
#
# Note: the OutcomeOps license validator is baked into this image at build
# time (see lambda/database-sync/Dockerfile + scripts/build-database-images.sh).
# Container-image Lambdas can't attach Lambda layers, so the license layer's
# content is downloaded via get-layer-version-by-arn and unpacked into the
# image's site-packages. The handler code imports `outcomeops_license`
# exactly as it would with a zip+layer deployment.
# ============================================================================

module "database_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-database-sync"
  description   = "Schema introspection sync for OutcomeOps AI Assist"
  timeout       = 900
  memory_size   = 2048
  publish       = true

  create_package = false
  package_type   = "Image"
  image_uri      = "${aws_ecr_repository.database_sync[0].repository_url}:${data.aws_ssm_parameter.database_sync_image_tag[0].value}"
  architectures  = ["x86_64"]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

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
      # chunk 07: empty string when no subscribers configured - the
      # handler's publish path is gated on truthiness, so a missing
      # topic is a clean no-op (audit row still fires).
      DATABASE_DRIFT_TOPIC_ARN = length(aws_sns_topic.database_drift) > 0 ? aws_sns_topic.database_drift[0].arn : ""
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

      # chunk 07: publish-grant on the database-drift topic. Uses a
      # nonexistent-arn placeholder when no topic was created so the
      # policy attachment stays well-formed; the handler's publish
      # path is gated on the env-var being non-empty, so nothing tries
      # to publish in that case.
      publish_drift = {
        effect    = "Allow"
        actions   = ["sns:Publish"]
        resources = length(aws_sns_topic.database_drift) > 0 ? [aws_sns_topic.database_drift[0].arn] : ["arn:aws:sns:*:*:nonexistent"]
      }
    },
    local.outcomeops_license_iam,
  )

  tags = {
    Purpose = "database-sync"
  }
}

resource "aws_lambda_event_source_mapping" "database_sync" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.database_sync[0].arn
  function_name    = module.database_sync_lambda[0].lambda_function_name

  batch_size                         = 1
  maximum_batching_window_in_seconds = 0

  scaling_config {
    maximum_concurrency = 5
  }
}

# ============================================================================
# Database Sync Scheduler Lambda (zip - no introspection-engine dep, stays small)
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

  environment_variables = merge({
    ENV                 = var.environment
    APP_NAME            = var.app_name
    WORKSPACES_TABLE    = module.workspaces_table[0].dynamodb_table_id
    DATABASE_SYNC_QUEUE = aws_sqs_queue.database_sync[0].url
    },
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge({
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
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "database-scheduler"
  }
}

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
