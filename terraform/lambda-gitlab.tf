# ============================================================================
# GitLab Integration Lambda (ADR-050)
#
# Handles GitLab OAuth 2.0 (Model A delegated OAuth) + connection / repo
# management for GitLab projects. The sync worker + hourly scheduler are
# added in a subsequent chunk.
#
# Notes worth keeping in mind when editing this file:
#
# 1. The integration is OFF by default. ``var.enable_gitlab_integration``
#    gates the ENTIRE terraform surface -- integration Lambda + Function URL
#    + SSM publish + IAM. Customers without GitLab leave the flag false and
#    provision none of it.
#
# 2. SaaS-first. ``var.gitlab_base_url`` defaults to https://gitlab.com. A
#    later chunk makes self-managed GitLab a first-class base URL (ADR-050
#    Decision 1).
#
# 3. Model A delegated OAuth. ONE admin-registered OAuth app (client id in
#    tfvars, secret in operator-managed SSM). Each user authorizes once; the
#    token is user-scoped, so access is automatically limited to what the
#    authorizing user can see.
#
# 4. The OAuth app SECRET follows the house pattern (cf. the OIDC login secret):
#    terraform CREATES a SecureString placeholder at the DERIVED path
#    /<env>/<app>/gitlab/client-secret with lifecycle ignore_changes, and the
#    operator overwrites the value AFTER apply via `aws ssm put-parameter
#    --overwrite`. No SSM path in tfvars. A customer who wants to manage the
#    param themselves sets gitlab_client_secret_ssm_param (override) and terraform
#    then neither creates nor owns it. The redirect URI is likewise derived from
#    the deploy FQDN unless gitlab_redirect_uri overrides it.
# ============================================================================

locals {
  gitlab_secret_ssm_path = (
    var.gitlab_client_secret_ssm_param != "" ? var.gitlab_client_secret_ssm_param :
    "/${var.environment}/${var.app_name}/gitlab/client-secret"
  )
  gitlab_redirect_uri = (
    var.gitlab_redirect_uri != "" ? var.gitlab_redirect_uri :
    "https://${local.fqdn}/api/gitlab/callback"
  )
  # terraform owns the secret only when the operator did NOT override the path.
  gitlab_manage_secret = var.enable_workspaces && var.enable_gitlab_integration && var.gitlab_client_secret_ssm_param == ""
}

# Placeholder secret -- overwrite the value post-apply; ignore_changes keeps your
# real value across later applies.
resource "aws_ssm_parameter" "gitlab_client_secret" {
  count = local.gitlab_manage_secret ? 1 : 0

  name        = local.gitlab_secret_ssm_path
  description = "GitLab OAuth app secret (placeholder -- overwrite after first apply)"
  type        = "SecureString"
  value       = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "gitlab-client-secret"
  }
}

module "gitlab_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-gitlab-integration"
  description   = "GitLab OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/gitlab-integration"
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

      GITLAB_BASE_URL                = var.gitlab_base_url
      GITLAB_CLIENT_ID               = var.gitlab_client_id
      GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path
      GITLAB_REDIRECT_URI            = local.gitlab_redirect_uri

      # chunk-03: the handler enqueues the initial sync job on add-repos
      # and full repo-delete cleanup on delete-repo / disconnect (ADR-052,
      # via shared.delete_dispatch -- same contract as GitHub).
      GITLAB_SYNC_QUEUE = aws_sqs_queue.gitlab_sync[0].url
      DELETE_QUEUE      = aws_sqs_queue.delete_queue[0].url
      # Secondary-region delete queue (HA) so repo-delete cleanup reaches
      # both regions (delete-bug 02). Empty in single-region mode.
      DELETE_QUEUE_SECONDARY = var.secondary_region != "" ? aws_sqs_queue.delete_queue_secondary[0].url : ""

      AUDIT_TABLE = module.audit_logs_table.dynamodb_table_id
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

      # SSM for the client secret. Scope to the exact derived (or overridden)
      # parameter name -- the integration is the only consumer.
      ssm_secret = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.gitlab_secret_ssm_path}",
        ]
      }

      # chunk-03: enqueue the initial sync job (gitlab sync queue) + full
      # repo-delete cleanup (shared delete queue, + secondary delete queue
      # under HA per ADR-052) from the handler.
      sqs_send = {
        effect = "Allow"
        actions = [
          "sqs:SendMessage"
        ]
        resources = concat(
          [
            aws_sqs_queue.gitlab_sync[0].arn,
            aws_sqs_queue.delete_queue[0].arn,
          ],
          var.secondary_region != "" ? [aws_sqs_queue.delete_queue_secondary[0].arn] : [],
        )
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
    Purpose = "gitlab-integration"
  }
}

resource "aws_lambda_function_url" "gitlab_integration" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name      = module.gitlab_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "gitlab_integration_url" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/gitlab-integration-url"
  description = "Lambda Function URL for GitLab integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.gitlab_integration[0].function_url

  tags = {
    Purpose = "gitlab-integration-url"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "gitlab_integration_lambda_name" {
  description = "Name of the GitLab integration Lambda function"
  value       = try(module.gitlab_integration_lambda[0].lambda_function_name, null)
}

output "gitlab_integration_lambda_arn" {
  description = "ARN of the GitLab integration Lambda function"
  value       = try(module.gitlab_integration_lambda[0].lambda_function_arn, null)
}

output "gitlab_integration_url" {
  description = "Lambda Function URL for GitLab integration"
  value       = try(aws_lambda_function_url.gitlab_integration[0].function_url, null)
}

# ============================================================================
# SQS queue + DLQ for GitLab sync jobs (ADR-050, chunk-03).
# The integration Lambda writes to this queue on add-repos; the scheduler
# fans out hourly sync jobs; the sync worker consumes it.
# ============================================================================

resource "aws_sqs_queue" "gitlab_sync" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-gitlab-sync"
  visibility_timeout_seconds = 900   # 15 minutes for long-running syncs
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.gitlab_sync_dlq[0].arn
    maxReceiveCount     = 3
  })

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "gitlab-sync-queue"
  }
}

resource "aws_sqs_queue" "gitlab_sync_dlq" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-gitlab-sync-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "gitlab-sync-dlq"
  }
}

output "gitlab_sync_queue_url" {
  description = "SQS queue URL for GitLab sync jobs"
  value       = try(aws_sqs_queue.gitlab_sync[0].url, null)
}

output "gitlab_sync_queue_arn" {
  description = "SQS queue ARN for GitLab sync jobs"
  value       = try(aws_sqs_queue.gitlab_sync[0].arn, null)
}

# ============================================================================
# GitLab Sync Scheduler Lambda (ADR-050, chunk-03)
# Runs hourly via EventBridge to fan out sync jobs for every enabled
# GITLAB_REPO# row. Honors is_active_region() so the secondary-region copy
# returns immediately when passive.
# ============================================================================

module "gitlab_scheduler_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-gitlab-scheduler"
  description   = "Hourly scheduler for GitLab sync jobs"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    {
      path             = "${path.module}/../lambda/gitlab-scheduler"
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
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV               = var.environment
    APP_NAME          = var.app_name
    WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
    GITLAB_SYNC_QUEUE = aws_sqs_queue.gitlab_sync[0].url
    },
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge({
    # The scheduler scans WORKSPACES_TABLE for GITLAB_REPO# rows.
    # Scan is paginated in the handler per feedback_enterprise_scale_pagination.
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table[0].dynamodb_table_arn]
    }

    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.gitlab_sync[0].arn]
    }
    },
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "gitlab-scheduler"
  }
}

resource "aws_cloudwatch_event_rule" "gitlab_hourly_sync" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  name                = "${var.environment}-${var.app_name}-gitlab-hourly-sync"
  description         = "Trigger GitLab sync scheduler every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "gitlab-scheduler"
  }
}

resource "aws_cloudwatch_event_target" "gitlab_scheduler" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  rule      = aws_cloudwatch_event_rule.gitlab_hourly_sync[0].name
  target_id = "gitlab-scheduler"
  arn       = module.gitlab_scheduler_lambda[0].lambda_function_arn
}

resource "aws_lambda_permission" "gitlab_scheduler_eventbridge" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.gitlab_scheduler_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.gitlab_hourly_sync[0].arn
}

# ============================================================================
# GitLab Sync Worker Lambda (ADR-050, chunk-03)
# SQS-triggered. Pulls source content from GitLab, mirrors it to S3, and
# enqueues one repo-summaries message so the code-map + symbol-graph
# pipeline runs for GitLab too.
# ============================================================================

module "gitlab_sync_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-gitlab-sync"
  description   = "Pulls GitLab repo content + fans out to downstream KB workers"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for large monorepos
  memory_size   = 1024

  source_path = [
    {
      path             = "${path.module}/../lambda/gitlab-sync"
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

  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn

      GITLAB_BASE_URL                = var.gitlab_base_url
      GITLAB_CLIENT_ID               = var.gitlab_client_id
      GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path

      # chunk-03: sync worker writes files DIRECTLY to S3 (not via
      # file-ingestion / repo-summaries) so we don't contaminate the
      # github-shaped consumers + their DLQs. process-documents is
      # S3-triggered + source-agnostic, so KB chat search picks up
      # the uploaded files automatically.
      KB_BUCKET         = module.knowledge_base_bucket.s3_bucket_id
      GITLAB_SYNC_QUEUE = aws_sqs_queue.gitlab_sync[0].url
      # After the per-file S3 upload finishes the sync worker enqueues
      # ONE repo-summaries message so the existing code-map +
      # symbol-graph pipeline (process-repo-summary + process-batch-
      # summary) runs for GitLab too. Downstream consumers route the
      # file-content fetch to the S3 mirror via shared.source_fetch.
      REPO_SUMMARIES_QUEUE = module.repo_summaries_queue.queue_url
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
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
          "dynamodb:Query",
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*",
        ]
      }

      # SSM for the client secret (read by shared.oauth.gitlab during token
      # refresh). Scope to the exact derived (or overridden) parameter name.
      ssm_secret = {
        effect  = "Allow"
        actions = ["ssm:GetParameter"]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.gitlab_secret_ssm_path}",
        ]
      }

      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_key.oauth_tokens[0].arn]
      }

      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }

      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.gitlab_sync[0].arn]
      }

      # Write blobs directly to the KB bucket. process-documents is
      # S3-triggered + source-agnostic so chat retrieval picks up GitLab
      # files without a downstream Lambda refactor.
      s3_kb_write = {
        effect    = "Allow"
        actions   = ["s3:PutObject", "s3:PutObjectAcl"]
        resources = ["${module.knowledge_base_bucket.s3_bucket_arn}/*"]
      }

      # Enqueue one repo-summaries job per sync so the code-map +
      # symbol-graph pipeline fans into the same queue github-sync uses.
      sqs_send_repo_summaries = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [module.repo_summaries_queue.queue_arn]
      }

      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table.dynamodb_table_arn]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "gitlab-sync"
  }
}

resource "aws_lambda_event_source_mapping" "gitlab_sync" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  event_source_arn = aws_sqs_queue.gitlab_sync[0].arn
  function_name    = module.gitlab_sync_lambda[0].lambda_function_name

  batch_size                         = 1
  maximum_batching_window_in_seconds = 0
  function_response_types            = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5
  }
}

output "gitlab_sync_lambda_name" {
  description = "Name of the GitLab sync Lambda function"
  value       = try(module.gitlab_sync_lambda[0].lambda_function_name, null)
}

output "gitlab_sync_lambda_arn" {
  description = "ARN of the GitLab sync Lambda function"
  value       = try(module.gitlab_sync_lambda[0].lambda_function_arn, null)
}

output "gitlab_scheduler_lambda_name" {
  description = "Name of the GitLab scheduler Lambda function"
  value       = try(module.gitlab_scheduler_lambda[0].lambda_function_name, null)
}

output "gitlab_scheduler_lambda_arn" {
  description = "ARN of the GitLab scheduler Lambda function"
  value       = try(module.gitlab_scheduler_lambda[0].lambda_function_arn, null)
}
