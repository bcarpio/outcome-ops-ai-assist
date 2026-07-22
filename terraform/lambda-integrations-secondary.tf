# ============================================================================
# Secondary-region integration handler Lambdas (HA chunk 9c-3b)
# ----------------------------------------------------------------------------
# Mirror of the 9 integration Lambdas under aws.secondary. Each gets its own
# Function URL with IAM auth and an SSM parameter holding the URL so the
# secondary UI Fargate (chunk 9h) can resolve it without hard-coding.
#
# Env vars + IAM scope to SECONDARY-region resources only:
#   workspaces_table_secondary, audit_logs_table_secondary,
#   {provider}_sync_secondary SQS queues, oauth_tokens_secondary KMS,
#   db_credentials_secondary KMS (database only),
#   replication_secondary event bus, ha_secondary AppConfig.
#
# All modules gated on var.enable_workspaces && var.secondary_region != "".
# Single-region deploys (secondary_region == "") produce no resources here.
#
# Passive-mode behavior: each Lambda's handler.py routes mutation
# operations through shared.region_guard.guard_active() per chunk 9c-2.
# When secondary is passive (default after first deploy), POSTs / PUTs /
# DELETEs return HTTP 403 with {"error": "region_passive"}.
# ============================================================================

# ----------------------------------------------------------------------------
# GitHub integration (secondary)
# ----------------------------------------------------------------------------

module "github_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-github-integration"
  role_name     = "${var.environment}-${var.app_name}-github-integration-secondary"
  description   = "GitHub App OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/github-integration"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      GITHUB_BASE_URL   = var.github_base_url
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      GITHUB_SYNC_QUEUE = aws_sqs_queue.github_sync_secondary[0].url
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      # chunk 10 (HA parity): reverse pointer to the primary delete
      # queue so disconnect-cleanup issued while secondary is active
      # also purges primary-region KB/vectors. Handler reads
      # DELETE_QUEUE_SECONDARY (name is region-neutral; on the primary
      # side it points to the secondary queue).
      DELETE_QUEUE_SECONDARY = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE            = module.audit_logs_table_secondary[0].dynamodb_table_id
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/*"]
      }
      # fable-scale 06 mirror: the cached-installation-token path WRITES
      # the token back to SSM. Without PutParameter the secondary falls
      # back to a fresh token mint on every call post-failover (GitHub
      # rate-limit exposure), and the write raises AccessDenied noise.
      ssm_write_token_cache = {
        effect    = "Allow"
        actions   = ["ssm:PutParameter"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/installation-token/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        # chunk 10 (HA parity): also grant SendMessage on the primary
        # delete queue for cross-region cleanup.
        resources = [
          aws_sqs_queue.github_sync_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.delete_queue[0].arn,
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

  tags = { Purpose = "github-integration" }
}

resource "aws_lambda_function_url" "github_integration_secondary" {
  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.github_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "github_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/github-integration-url"
  description = "Lambda Function URL for GitHub integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.github_integration_secondary[0].function_url

  tags = { Purpose = "github-integration" }
}

# ----------------------------------------------------------------------------
# Confluence integration (secondary)
# ----------------------------------------------------------------------------

module "confluence_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-confluence-integration"
  role_name     = "${var.environment}-${var.app_name}-confluence-integration-secondary"
  description   = "Confluence OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/confluence-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                   = var.environment
      APP_NAME              = var.app_name
      WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
      CONFLUENCE_SYNC_QUEUE = aws_sqs_queue.confluence_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/atlassian/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.confluence_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "confluence-integration" }
}

resource "aws_lambda_function_url" "confluence_integration_secondary" {
  count = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.confluence_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "confluence_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/confluence-integration-url"
  description = "Lambda Function URL for Confluence integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.confluence_integration_secondary[0].function_url

  tags = { Purpose = "confluence-integration" }
}

# ----------------------------------------------------------------------------
# Jira integration (secondary)
# ----------------------------------------------------------------------------

module "jira_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-jira-integration"
  role_name     = "${var.environment}-${var.app_name}-jira-integration-secondary"
  description   = "Jira OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/jira-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
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
      JIRA_SYNC_QUEUE  = aws_sqs_queue.jira_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/atlassian/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.jira_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "jira-integration" }
}

resource "aws_lambda_function_url" "jira_integration_secondary" {
  count = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.jira_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "jira_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/jira-integration-url"
  description = "Lambda Function URL for Jira integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.jira_integration_secondary[0].function_url

  tags = { Purpose = "jira-integration" }
}

# ----------------------------------------------------------------------------
# Outlook integration (secondary)
# ----------------------------------------------------------------------------

module "outlook_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-outlook-integration"
  role_name     = "${var.environment}-${var.app_name}-outlook-integration-secondary"
  description   = "Outlook OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/outlook-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                = var.environment
      APP_NAME           = var.app_name
      WORKSPACES_TABLE   = module.workspaces_table_secondary[0].dynamodb_table_id
      OUTLOOK_SYNC_QUEUE = aws_sqs_queue.outlook_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.outlook_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "outlook-integration" }
}

resource "aws_lambda_function_url" "outlook_integration_secondary" {
  count    = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.outlook_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "outlook_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/outlook-integration-url"
  description = "Lambda Function URL for Outlook integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.outlook_integration_secondary[0].function_url

  tags = { Purpose = "outlook-integration" }
}

# ----------------------------------------------------------------------------
# Teams integration (secondary)
# ----------------------------------------------------------------------------

module "teams_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-teams-integration"
  role_name     = "${var.environment}-${var.app_name}-teams-integration-secondary"
  description   = "Microsoft Teams OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/teams-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
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
      TEAMS_SYNC_QUEUE = aws_sqs_queue.teams_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.teams_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "teams-integration" }
}

resource "aws_lambda_function_url" "teams_integration_secondary" {
  count    = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.teams_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "teams_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/teams-integration-url"
  description = "Lambda Function URL for Teams integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.teams_integration_secondary[0].function_url

  tags = { Purpose = "teams-integration" }
}

# ----------------------------------------------------------------------------
# SharePoint integration (secondary)
# ----------------------------------------------------------------------------

module "sharepoint_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-sharepoint-integration"
  role_name     = "${var.environment}-${var.app_name}-sharepoint-integration-secondary"
  description   = "SharePoint OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/sharepoint-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                   = var.environment
      APP_NAME              = var.app_name
      WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
      SHAREPOINT_SYNC_QUEUE = aws_sqs_queue.sharepoint_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.sharepoint_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "sharepoint-integration" }
}

resource "aws_lambda_function_url" "sharepoint_integration_secondary" {
  count    = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.sharepoint_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "sharepoint_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/sharepoint-integration-url"
  description = "Lambda Function URL for SharePoint integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.sharepoint_integration_secondary[0].function_url

  tags = { Purpose = "sharepoint-integration" }
}

# ----------------------------------------------------------------------------
# OneDrive integration (secondary)
# ----------------------------------------------------------------------------

module "onedrive_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onedrive-integration"
  role_name     = "${var.environment}-${var.app_name}-onedrive-integration-secondary"
  description   = "OneDrive OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/onedrive-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                 = var.environment
      APP_NAME            = var.app_name
      WORKSPACES_TABLE    = module.workspaces_table_secondary[0].dynamodb_table_id
      ONEDRIVE_SYNC_QUEUE = aws_sqs_queue.onedrive_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.onedrive_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "onedrive-integration" }
}

resource "aws_lambda_function_url" "onedrive_integration_secondary" {
  count    = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.onedrive_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "onedrive_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/onedrive-integration-url"
  description = "Lambda Function URL for OneDrive integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.onedrive_integration_secondary[0].function_url

  tags = { Purpose = "onedrive-integration" }
}

# ----------------------------------------------------------------------------
# OneNote integration (secondary)
# ----------------------------------------------------------------------------

module "onenote_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onenote-integration"
  role_name     = "${var.environment}-${var.app_name}-onenote-integration-secondary"
  description   = "OneNote OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/onenote-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                = var.environment
      APP_NAME           = var.app_name
      WORKSPACES_TABLE   = module.workspaces_table_secondary[0].dynamodb_table_id
      ONENOTE_SYNC_QUEUE = aws_sqs_queue.onenote_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/microsoft/*"]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.onenote_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "onenote-integration" }
}

resource "aws_lambda_function_url" "onenote_integration_secondary" {
  count    = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.onenote_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "onenote_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/onenote-integration-url"
  description = "Lambda Function URL for OneNote integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.onenote_integration_secondary[0].function_url

  tags = { Purpose = "onenote-integration" }
}

# ----------------------------------------------------------------------------
# Database integration (secondary)
# Distinct from the others: uses db_credentials KMS key (not oauth_tokens).
# Container Lambda — engine drivers (Postgres/MySQL/MSSQL) exceed Lambda's
# 250 MB unzipped layer cap, so we ship as an image like the primary.
# ----------------------------------------------------------------------------

data "aws_ssm_parameter" "database_integration_image_tag_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary
  name     = "/${var.environment}/${var.app_name}/database-integration/image-tag"
}

module "database_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-database-integration"
  role_name     = "${var.environment}-${var.app_name}-database-integration-secondary"
  description   = "Database connection management (secondary region)"
  timeout       = 60
  memory_size   = 1024
  publish       = true

  create_package = false
  package_type   = "Image"
  image_uri      = "${aws_ecr_repository.database_integration_secondary[0].repository_url}:${data.aws_ssm_parameter.database_integration_image_tag_secondary[0].value}"
  architectures  = ["x86_64"]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn

  # Container Lambdas cannot attach external layers; license-validator
  # logic must be baked into the image. Primary follows the same model.

  environment_variables = merge(
    {
      ENV                 = var.environment
      APP_NAME            = var.app_name
      WORKSPACES_TABLE    = module.workspaces_table_secondary[0].dynamodb_table_id
      DATABASE_SYNC_QUEUE = aws_sqs_queue.database_sync_secondary[0].url
      # chunk 08 (HA parity): mirror primary DELETE_QUEUE so disconnect-
      # cleanup enqueues delete jobs after failover (silent no-op otherwise).
      DELETE_QUEUE              = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE               = module.audit_logs_table_secondary[0].dynamodb_table_id
      DB_CREDENTIAL_KMS_KEY_ARN = aws_kms_replica_key.db_credentials_secondary[0].arn
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
      kms_db_credentials = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.db_credentials_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.database_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "database-integration" }
}

resource "aws_lambda_function_url" "database_integration_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  function_name      = module.database_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "database_integration_url_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/database-integration-url"
  description = "Lambda Function URL for Database integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.database_integration_secondary[0].function_url

  tags = { Purpose = "database-integration" }
}

# ============================================================================
# Outputs — Function URLs consumed by the secondary UI Fargate (chunk 9h)
# ============================================================================

output "github_integration_url_secondary" {
  description = "Lambda Function URL for GitHub integration in secondary region (null when HA disabled or gate off)"
  value       = try(aws_lambda_function_url.github_integration_secondary[0].function_url, null)
}

output "confluence_integration_url_secondary" {
  description = "Lambda Function URL for Confluence integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.confluence_integration_secondary[0].function_url, null)
}

output "jira_integration_url_secondary" {
  description = "Lambda Function URL for Jira integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.jira_integration_secondary[0].function_url, null)
}

output "outlook_integration_url_secondary" {
  description = "Lambda Function URL for Outlook integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.outlook_integration_secondary[0].function_url, null)
}

output "teams_integration_url_secondary" {
  description = "Lambda Function URL for Teams integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.teams_integration_secondary[0].function_url, null)
}

output "sharepoint_integration_url_secondary" {
  description = "Lambda Function URL for SharePoint integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.sharepoint_integration_secondary[0].function_url, null)
}

output "onedrive_integration_url_secondary" {
  description = "Lambda Function URL for OneDrive integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.onedrive_integration_secondary[0].function_url, null)
}

output "onenote_integration_url_secondary" {
  description = "Lambda Function URL for OneNote integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.onenote_integration_secondary[0].function_url, null)
}

output "database_integration_url_secondary" {
  description = "Lambda Function URL for Database integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.database_integration_secondary[0].function_url, null)
}

# ----------------------------------------------------------------------------
# Azure DevOps integration (secondary) -- chunk 09
# ----------------------------------------------------------------------------

module "azure_devops_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-azure-devops-integration"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-integration-secondary"
  description   = "Azure DevOps OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/azure-devops-integration"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  # Env mirrors the primary's azure_devops_integration_lambda except:
  # - WORKSPACES_TABLE points at the secondary-region table
  # - AZURE_DEVOPS_SYNC_QUEUE points at the secondary-region queue
  # - DELETE_QUEUE points at the secondary delete queue
  # - OAUTH_KMS_KEY_ARN points at the secondary-region KMS replica key
  #   (aws_kms_replica_key.oauth_tokens_secondary in
  #   terraform/kms-oauth.tf). Same key material as the primary via
  #   the multi-region replica, but the ARN is regional so crypto ops
  #   stay in-region under failover.
  # - AZURE_DEVOPS_TENANT_ID / _CLIENT_ID / _REDIRECT_URI / secret SSM
  #   param are SAME in both regions (one Entra ID app registration
  #   per customer).
  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
      AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
      AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param
      AZURE_DEVOPS_REDIRECT_URI            = var.azure_devops_redirect_uri

      AZURE_DEVOPS_SYNC_QUEUE = aws_sqs_queue.azure_devops_sync_secondary[0].url
      DELETE_QUEUE            = aws_sqs_queue.delete_queue_secondary[0].url
      # ADR-052 dual-region delete contract (757c38c added this to the
      # primary; mirrored here so post-failover ADO deletes clean BOTH
      # regions). Name is region-neutral: "the other region's queue".
      DELETE_QUEUE_SECONDARY = aws_sqs_queue.delete_queue[0].url
      AUDIT_TABLE            = module.audit_logs_table_secondary[0].dynamodb_table_id
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
      # SSM secret reference -- scope to the exact parameter name in
      # the SECONDARY region. The operator creates the same parameter
      # name in both regions; the secret value is the same (one Entra
      # ID app secret per customer) but lives in the regional SSM
      # store of each region per the SSM-isn't-global pattern.
      ssm_secret = {
        effect  = "Allow"
        actions = ["ssm:GetParameter"]
        resources = [
          "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}",
        ]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable when this Lambda handles
      # a Function URL callback post-failover.
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
          aws_sqs_queue.azure_devops_sync_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          # Cross-region delete fan-out (ADR-052) back to the primary.
          aws_sqs_queue.delete_queue[0].arn,
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

  tags = { Purpose = "azure-devops-integration" }
}

resource "aws_lambda_function_url" "azure_devops_integration_secondary" {
  count = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.azure_devops_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "azure_devops_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/azure-devops-integration-url"
  description = "Lambda Function URL for Azure DevOps integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.azure_devops_integration_secondary[0].function_url

  tags = { Purpose = "azure-devops-integration" }
}

output "azure_devops_integration_url_secondary" {
  description = "Lambda Function URL for Azure DevOps integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.azure_devops_integration_secondary[0].function_url, null)
}

# ----------------------------------------------------------------------------
# GitLab integration (secondary) -- ADR-050 chunk 08
# ----------------------------------------------------------------------------

# Same derived-path placeholder as the primary (lambda-gitlab.tf): terraform
# creates it with ignore_changes and the operator sets the real value in the
# SECONDARY region too (`ha-sync-secrets` copies it; `ha-list-secrets`
# reports it). SSM is regional -- without this the secondary integration
# Lambda's token refresh fails post-failover.
resource "aws_ssm_parameter" "gitlab_client_secret_secondary" {
  count = local.gitlab_manage_secret && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

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

module "gitlab_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-gitlab-integration"
  role_name     = "${var.environment}-${var.app_name}-gitlab-integration-secondary"
  description   = "GitLab OAuth and connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/gitlab-integration"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  # Env mirrors the primary's gitlab_integration_lambda except:
  # - WORKSPACES_TABLE / AUDIT_TABLE point at the secondary-region tables
  # - GITLAB_SYNC_QUEUE points at the secondary-region queue
  # - OAUTH_KMS_KEY_ARN points at the secondary-region KMS replica
  # - DELETE_QUEUE is the LOCAL (secondary) delete queue and
  #   DELETE_QUEUE_SECONDARY the PRIMARY one, so the ADR-052 dual-region
  #   delete contract holds from either side of a failover.
  # - GITLAB_BASE_URL / _CLIENT_ID / secret path / redirect URI are the
  #   SAME in both regions (one GitLab OAuth app per deploy).
  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_replica_key.oauth_tokens_secondary[0].arn

      GITLAB_BASE_URL                = var.gitlab_base_url
      GITLAB_CLIENT_ID               = var.gitlab_client_id
      GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path
      GITLAB_INSTANCES               = local.gitlab_instances_env
      GITLAB_REDIRECT_URI            = local.gitlab_redirect_uri

      GITLAB_SYNC_QUEUE      = aws_sqs_queue.gitlab_sync_secondary[0].url
      DELETE_QUEUE           = aws_sqs_queue.delete_queue_secondary[0].url
      DELETE_QUEUE_SECONDARY = aws_sqs_queue.delete_queue[0].url

      AUDIT_TABLE = module.audit_logs_table_secondary[0].dynamodb_table_id
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
        effect  = "Allow"
        actions = ["ssm:GetParameter"]
        resources = [
          "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${local.gitlab_secret_ssm_path}",
        ]
      }
      # Cross-region delete fan-out (ADR-052): local + primary queues.
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.gitlab_sync_secondary[0].arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
          aws_sqs_queue.delete_queue[0].arn,
        ]
      }
      # Include both the secondary replica ARN (encrypt/decrypt in the
      # passive region) AND the primary ARN so a token minted against
      # the primary ARN is still decryptable when this Lambda handles
      # a Function URL callback post-failover.
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

  tags = { Purpose = "gitlab-integration" }
}

resource "aws_lambda_function_url" "gitlab_integration_secondary" {
  count = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.gitlab_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "gitlab_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/gitlab-integration-url"
  description = "Lambda Function URL for GitLab integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.gitlab_integration_secondary[0].function_url

  tags = { Purpose = "gitlab-integration-url" }
}

output "gitlab_integration_url_secondary" {
  description = "Lambda Function URL for GitLab integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.gitlab_integration_secondary[0].function_url, null)
}

# ----------------------------------------------------------------------------
# ServiceNow integration (secondary) -- chunk 07 (HA parity, ADR-044)
#
# Per-workspace OAuth credentials live in DDB (KMS-encrypted with the
# oauth_tokens_secondary CMK), so no env-level SSM params to replicate.
# ----------------------------------------------------------------------------

module "servicenow_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-servicenow-integration"
  role_name     = "${var.environment}-${var.app_name}-servicenow-integration-secondary"
  description   = "ServiceNow OAuth setup + connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/servicenow-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                          = var.environment
      APP_NAME                     = var.app_name
      WORKSPACES_TABLE             = module.workspaces_table_secondary[0].dynamodb_table_id
      SERVICENOW_SYNC_QUEUE        = aws_sqs_queue.servicenow_sync_secondary[0].url
      DELETE_QUEUE                 = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE                  = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN            = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      SERVICENOW_STATE_TTL_SECONDS = "600"
      # Same callback URL as primary -- resolves against local.fqdn if
      # set. Empty for early bring-up before a custom domain exists.
      SERVICENOW_CALLBACK_URL = var.domain != "" ? "https://${local.fqdn}/api/servicenow/callback" : ""
    },
    local.servicenow_common_env,
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
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      # ServiceNow client_secret SSM param is replicated into the secondary
      # region by scripts/ha-sync-secrets; grant read there too so the
      # Lambda can source credentials post-failover.
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/servicenow/*"]
      }
      # Both regions' oauth_tokens CMKs -- a token minted against the
      # primary KMS key is still decryptable after failover routes the
      # user to this Lambda.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.servicenow_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "servicenow-integration" }
}

resource "aws_lambda_function_url" "servicenow_integration_secondary" {
  count = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.servicenow_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "servicenow_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/servicenow-integration-url"
  description = "Lambda Function URL for ServiceNow integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.servicenow_integration_secondary[0].function_url

  tags = { Purpose = "servicenow-integration" }
}

output "servicenow_integration_url_secondary" {
  description = "Lambda Function URL for ServiceNow integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.servicenow_integration_secondary[0].function_url, null)
}

# ----------------------------------------------------------------------------
# salesforce_integration -- HA parity
# ----------------------------------------------------------------------------

module "salesforce_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-salesforce-integration"
  role_name     = "${var.environment}-${var.app_name}-salesforce-integration-secondary"
  description   = "Salesforce OAuth + connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/salesforce-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                          = var.environment
      APP_NAME                     = var.app_name
      WORKSPACES_TABLE             = module.workspaces_table_secondary[0].dynamodb_table_id
      SALESFORCE_SYNC_QUEUE        = aws_sqs_queue.salesforce_sync_secondary[0].url
      DELETE_QUEUE                 = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE                  = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN            = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      SALESFORCE_STATE_TTL_SECONDS = "600"
      SALESFORCE_CALLBACK_URL      = var.domain != "" ? "https://${local.fqdn}/api/salesforce/callback" : ""
    },
    local.salesforce_common_env,
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
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      # Salesforce consumer_secret SSM param replicated into secondary
      # by scripts/ha-sync-secrets.
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/salesforce/*"]
      }
      # Both regions' oauth_tokens CMKs so a token minted primary is
      # still decryptable after failover.
      kms_oauth = {
        effect  = "Allow"
        actions = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [
          aws_kms_replica_key.oauth_tokens_secondary[0].arn,
          aws_kms_key.oauth_tokens[0].arn,
        ]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.salesforce_sync_secondary[0].arn, aws_sqs_queue.delete_queue_secondary[0].arn]
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

  tags = { Purpose = "salesforce-integration" }
}

resource "aws_lambda_function_url" "salesforce_integration_secondary" {
  count = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.salesforce_integration_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "salesforce_integration_url_secondary" {
  count = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/salesforce-integration-url"
  description = "Lambda Function URL for Salesforce integration in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.salesforce_integration_secondary[0].function_url

  tags = { Purpose = "salesforce-integration" }
}

output "salesforce_integration_url_secondary" {
  description = "Lambda Function URL for Salesforce integration in secondary region (null when HA disabled)"
  value       = try(aws_lambda_function_url.salesforce_integration_secondary[0].function_url, null)
}
