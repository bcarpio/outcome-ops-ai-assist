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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-github-integration"
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
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      GITHUB_SYNC_QUEUE = aws_sqs_queue.github_sync_secondary[0].url
      DELETE_QUEUE      = aws_sqs_queue.delete_queue_secondary[0].url
      AUDIT_TABLE       = module.audit_logs_table_secondary[0].dynamodb_table_id
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
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage"]
        resources = [
          aws_sqs_queue.github_sync_secondary[0].arn,
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

  tags = { Purpose = "github-integration" }
}

resource "aws_lambda_function_url" "github_integration_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-confluence-integration"
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
      AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN     = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.confluence_sync_secondary[0].arn]
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
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-jira-integration"
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
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      JIRA_SYNC_QUEUE   = aws_sqs_queue.jira_sync_secondary[0].url
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.jira_sync_secondary[0].arn]
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
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-outlook-integration"
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
      AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN  = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.outlook_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-teams-integration"
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
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
      TEAMS_SYNC_QUEUE  = aws_sqs_queue.teams_sync_secondary[0].url
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.teams_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-sharepoint-integration"
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
      AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN     = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.sharepoint_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onedrive-integration"
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
      AUDIT_TABLE         = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN   = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.onedrive_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onenote-integration"
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
      AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN  = aws_kms_replica_key.oauth_tokens_secondary[0].arn
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
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.onenote_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
# ----------------------------------------------------------------------------

module "database_integration_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-database-integration"
  description   = "Database connection management (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/database-integration", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                       = var.environment
      APP_NAME                  = var.app_name
      WORKSPACES_TABLE          = module.workspaces_table_secondary[0].dynamodb_table_id
      DATABASE_SYNC_QUEUE       = aws_sqs_queue.database_sync_secondary[0].url
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
      kms_credentials = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.db_credentials_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.database_sync_secondary[0].arn]
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  count    = var.enable_workspaces && var.secondary_region != "" ? 1 : 0
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
  description = "Lambda Function URL for GitHub integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.github_integration_secondary[0].function_url : null
}

output "confluence_integration_url_secondary" {
  description = "Lambda Function URL for Confluence integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.confluence_integration_secondary[0].function_url : null
}

output "jira_integration_url_secondary" {
  description = "Lambda Function URL for Jira integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.jira_integration_secondary[0].function_url : null
}

output "outlook_integration_url_secondary" {
  description = "Lambda Function URL for Outlook integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.outlook_integration_secondary[0].function_url : null
}

output "teams_integration_url_secondary" {
  description = "Lambda Function URL for Teams integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.teams_integration_secondary[0].function_url : null
}

output "sharepoint_integration_url_secondary" {
  description = "Lambda Function URL for SharePoint integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.sharepoint_integration_secondary[0].function_url : null
}

output "onedrive_integration_url_secondary" {
  description = "Lambda Function URL for OneDrive integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.onedrive_integration_secondary[0].function_url : null
}

output "onenote_integration_url_secondary" {
  description = "Lambda Function URL for OneNote integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.onenote_integration_secondary[0].function_url : null
}

output "database_integration_url_secondary" {
  description = "Lambda Function URL for Database integration in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.database_integration_secondary[0].function_url : null
}
