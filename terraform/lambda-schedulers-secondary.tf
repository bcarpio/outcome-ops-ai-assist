# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Secondary-region scheduler Lambdas ()
# ----------------------------------------------------------------------------
# 10 hourly schedulers under aws.secondary, each with its own
# EventBridge rule firing locally. Schedulers already gate on
# shared.active_region.is_active_region() per — in the
# secondary region they fire every hour and immediately short-circuit
# returning passive-region-skip until AppConfig is flipped to active.
#
# All gated on var.enable_workspaces && var.secondary_region != "".
# ============================================================================

# Common source_path pattern — bundles the scheduler + shared/ for the
# active_region helper. Repeated for each scheduler because their
# primary directories differ.

# ----------------------------------------------------------------------------
# GitHub scheduler (secondary)
# ----------------------------------------------------------------------------

module "github_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-github-scheduler"
  role_name     = "${var.environment}-${var.app_name}-github-scheduler-secondary"
  description   = "Hourly scheduler for GitHub sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/github-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV               = var.environment
    APP_NAME          = var.app_name
    WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
    GITHUB_SYNC_QUEUE = aws_sqs_queue.github_sync_secondary[0].url
    # Secondary mirror: scan fan-out worker pool.
    GITHUB_SCHEDULER_PARALLEL_WORKERS = tostring(var.github_scheduler_parallel_workers)
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.github_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "github-scheduler" }
}

resource "aws_cloudwatch_event_rule" "github_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-github-hourly-sync"
  description         = "Trigger GitHub sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "github-scheduler" }
}

resource "aws_cloudwatch_event_target" "github_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.github_hourly_sync_secondary[0].name
  target_id = "github-scheduler"
  arn       = module.github_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "github_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.github_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.github_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Confluence scheduler (secondary)
# ----------------------------------------------------------------------------

module "confluence_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-confluence-scheduler"
  role_name     = "${var.environment}-${var.app_name}-confluence-scheduler-secondary"
  description   = "Hourly scheduler for Confluence sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/confluence-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
    CONFLUENCE_SYNC_QUEUE = aws_sqs_queue.confluence_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.confluence_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "confluence-scheduler" }
}

resource "aws_cloudwatch_event_rule" "confluence_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-confluence-hourly-sync"
  description         = "Trigger Confluence sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "confluence-scheduler" }
}

resource "aws_cloudwatch_event_target" "confluence_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.confluence_hourly_sync_secondary[0].name
  target_id = "confluence-scheduler"
  arn       = module.confluence_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "confluence_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_confluence_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.confluence_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.confluence_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Jira scheduler (secondary)
# ----------------------------------------------------------------------------

module "jira_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-jira-scheduler"
  role_name     = "${var.environment}-${var.app_name}-jira-scheduler-secondary"
  description   = "Hourly scheduler for Jira sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/jira-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV              = var.environment
    APP_NAME         = var.app_name
    WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id
    JIRA_SYNC_QUEUE  = aws_sqs_queue.jira_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.jira_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "jira-scheduler" }
}

resource "aws_cloudwatch_event_rule" "jira_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-jira-hourly-sync"
  description         = "Trigger Jira sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "jira-scheduler" }
}

resource "aws_cloudwatch_event_target" "jira_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.jira_hourly_sync_secondary[0].name
  target_id = "jira-scheduler"
  arn       = module.jira_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "jira_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_jira_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.jira_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.jira_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Database scheduler (secondary)
# ----------------------------------------------------------------------------

module "database_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-database-scheduler"
  role_name     = "${var.environment}-${var.app_name}-database-scheduler-secondary"
  description   = "Hourly scheduler for database sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/database-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                 = var.environment
    APP_NAME            = var.app_name
    WORKSPACES_TABLE    = module.workspaces_table_secondary[0].dynamodb_table_id
    DATABASE_SYNC_QUEUE = aws_sqs_queue.database_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.database_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "database-scheduler" }
}

resource "aws_cloudwatch_event_rule" "database_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-database-hourly-sync"
  description         = "Trigger database sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "database-scheduler" }
}

resource "aws_cloudwatch_event_target" "database_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.database_hourly_sync_secondary[0].name
  target_id = "database-scheduler"
  arn       = module.database_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "database_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_database_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.database_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.database_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Outlook / Teams / SharePoint / OneDrive / OneNote schedulers (secondary)
# Microsoft-flavor schedulers share an identical shape. Repeating
# inline because each has a unique sync-queue reference.
# ----------------------------------------------------------------------------

module "outlook_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-outlook-scheduler"
  role_name     = "${var.environment}-${var.app_name}-outlook-scheduler-secondary"
  description   = "Hourly scheduler for Outlook sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/outlook-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                  = var.environment
    APP_NAME             = var.app_name
    WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
    OUTLOOK_SYNC_QUEUE   = aws_sqs_queue.outlook_sync_secondary[0].url
    AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
    AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
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
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "outlook-scheduler" }
}

resource "aws_cloudwatch_event_rule" "outlook_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-outlook-hourly-sync"
  description         = "Trigger Outlook sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "outlook-scheduler" }
}

resource "aws_cloudwatch_event_target" "outlook_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.outlook_hourly_sync_secondary[0].name
  target_id = "outlook-scheduler"
  arn       = module.outlook_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "outlook_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_outlook_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.outlook_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.outlook_hourly_sync_secondary[0].arn
}

module "teams_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-teams-scheduler"
  role_name     = "${var.environment}-${var.app_name}-teams-scheduler-secondary"
  description   = "Hourly scheduler for Teams sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/teams-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV              = var.environment
    APP_NAME         = var.app_name
    WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id
    TEAMS_SYNC_QUEUE = aws_sqs_queue.teams_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.teams_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "teams-scheduler" }
}

resource "aws_cloudwatch_event_rule" "teams_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-teams-hourly-sync"
  description         = "Trigger Teams sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "teams-scheduler" }
}

resource "aws_cloudwatch_event_target" "teams_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.teams_hourly_sync_secondary[0].name
  target_id = "teams-scheduler"
  arn       = module.teams_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "teams_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_teams_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.teams_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.teams_hourly_sync_secondary[0].arn
}

module "sharepoint_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-sharepoint-scheduler"
  role_name     = "${var.environment}-${var.app_name}-sharepoint-scheduler-secondary"
  description   = "Hourly scheduler for SharePoint sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/sharepoint-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
    SHAREPOINT_SYNC_QUEUE = aws_sqs_queue.sharepoint_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.sharepoint_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "sharepoint-scheduler" }
}

resource "aws_cloudwatch_event_rule" "sharepoint_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-sharepoint-hourly-sync"
  description         = "Trigger SharePoint sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "sharepoint-scheduler" }
}

resource "aws_cloudwatch_event_target" "sharepoint_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.sharepoint_hourly_sync_secondary[0].name
  target_id = "sharepoint-scheduler"
  arn       = module.sharepoint_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "sharepoint_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_sharepoint_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.sharepoint_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.sharepoint_hourly_sync_secondary[0].arn
}

module "onedrive_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onedrive-scheduler"
  role_name     = "${var.environment}-${var.app_name}-onedrive-scheduler-secondary"
  description   = "Hourly scheduler for OneDrive sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/onedrive-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                 = var.environment
    APP_NAME            = var.app_name
    WORKSPACES_TABLE    = module.workspaces_table_secondary[0].dynamodb_table_id
    ONEDRIVE_SYNC_QUEUE = aws_sqs_queue.onedrive_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.onedrive_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "onedrive-scheduler" }
}

resource "aws_cloudwatch_event_rule" "onedrive_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-onedrive-hourly-sync"
  description         = "Trigger OneDrive sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "onedrive-scheduler" }
}

resource "aws_cloudwatch_event_target" "onedrive_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.onedrive_hourly_sync_secondary[0].name
  target_id = "onedrive-scheduler"
  arn       = module.onedrive_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "onedrive_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_onedrive_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.onedrive_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.onedrive_hourly_sync_secondary[0].arn
}

module "onenote_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-onenote-scheduler"
  role_name     = "${var.environment}-${var.app_name}-onenote-scheduler-secondary"
  description   = "Hourly scheduler for OneNote sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/onenote-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                = var.environment
    APP_NAME           = var.app_name
    WORKSPACES_TABLE   = module.workspaces_table_secondary[0].dynamodb_table_id
    ONENOTE_SYNC_QUEUE = aws_sqs_queue.onenote_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.onenote_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "onenote-scheduler" }
}

resource "aws_cloudwatch_event_rule" "onenote_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-onenote-hourly-sync"
  description         = "Trigger OneNote sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "onenote-scheduler" }
}

resource "aws_cloudwatch_event_target" "onenote_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.onenote_hourly_sync_secondary[0].name
  target_id = "onenote-scheduler"
  arn       = module.onenote_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "onenote_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_onenote_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.onenote_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.onenote_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Azure DevOps scheduler (secondary) -- 
#
# Mirrors the primary's hourly EventBridge rule. The scheduler handler
# short-circuits on is_active_region() == False so the passive copy
# logs and returns immediately each tick.
# ----------------------------------------------------------------------------

module "azure_devops_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-azure-devops-scheduler"
  role_name     = "${var.environment}-${var.app_name}-azure-devops-scheduler-secondary"
  description   = "Hourly scheduler for Azure DevOps sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/azure-devops-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                     = var.environment
    APP_NAME                = var.app_name
    WORKSPACES_TABLE        = module.workspaces_table_secondary[0].dynamodb_table_id
    AZURE_DEVOPS_SYNC_QUEUE = aws_sqs_queue.azure_devops_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.azure_devops_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "azure-devops-scheduler" }
}

resource "aws_cloudwatch_event_rule" "azure_devops_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-azure-devops-hourly-sync"
  description         = "Trigger Azure DevOps sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "azure-devops-scheduler" }
}

resource "aws_cloudwatch_event_target" "azure_devops_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.azure_devops_hourly_sync_secondary[0].name
  target_id = "azure-devops-scheduler"
  arn       = module.azure_devops_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "azure_devops_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_azure_devops_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.azure_devops_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.azure_devops_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# GitLab scheduler (secondary) -- ADR-050
# ----------------------------------------------------------------------------

module "gitlab_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-gitlab-scheduler"
  role_name     = "${var.environment}-${var.app_name}-gitlab-scheduler-secondary"
  description   = "Hourly scheduler for GitLab sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/gitlab-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV               = var.environment
    APP_NAME          = var.app_name
    WORKSPACES_TABLE  = module.workspaces_table_secondary[0].dynamodb_table_id
    GITLAB_SYNC_QUEUE = aws_sqs_queue.gitlab_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.gitlab_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "gitlab-scheduler" }
}

resource "aws_cloudwatch_event_rule" "gitlab_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-gitlab-hourly-sync"
  description         = "Trigger GitLab sync scheduler every hour (secondary region)"
  schedule_expression = "rate(1 hour)"
  tags                = { Purpose = "gitlab-scheduler" }
}

resource "aws_cloudwatch_event_target" "gitlab_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.gitlab_hourly_sync_secondary[0].name
  target_id = "gitlab-scheduler"
  arn       = module.gitlab_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "gitlab_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_gitlab_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.gitlab_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.gitlab_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# ServiceNow scheduler (secondary) -- (HA parity, ADR-044)
# ----------------------------------------------------------------------------

module "servicenow_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-servicenow-scheduler"
  role_name     = "${var.environment}-${var.app_name}-servicenow-scheduler-secondary"
  description   = "Hourly scheduler for ServiceNow sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/servicenow-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
    SERVICENOW_SYNC_QUEUE = aws_sqs_queue.servicenow_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect  = "Allow"
      actions = ["dynamodb:Scan", "dynamodb:Query"]
      resources = [
        module.workspaces_table_secondary[0].dynamodb_table_arn,
        "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
      ]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.servicenow_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "servicenow-scheduler" }
}

resource "aws_cloudwatch_event_rule" "servicenow_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-servicenow-hourly-sync"
  description         = "Trigger ServiceNow sync scheduler hourly at :45 (secondary region)"
  schedule_expression = "cron(45 * * * ? *)"
  tags                = { Purpose = "servicenow-scheduler" }
}

resource "aws_cloudwatch_event_target" "servicenow_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.servicenow_hourly_sync_secondary[0].name
  target_id = "servicenow-scheduler"
  arn       = module.servicenow_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "servicenow_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_servicenow_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.servicenow_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.servicenow_hourly_sync_secondary[0].arn
}

# ----------------------------------------------------------------------------
# salesforce_scheduler -- HA parity
# ----------------------------------------------------------------------------

module "salesforce_scheduler_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-salesforce-scheduler"
  role_name     = "${var.environment}-${var.app_name}-salesforce-scheduler-secondary"
  description   = "Hourly scheduler for Salesforce sync jobs (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/salesforce-scheduler", pip_requirements = false, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
    SALESFORCE_SYNC_QUEUE = aws_sqs_queue.salesforce_sync_secondary[0].url
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect  = "Allow"
      actions = ["dynamodb:Scan", "dynamodb:Query"]
      resources = [
        module.workspaces_table_secondary[0].dynamodb_table_arn,
        "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
      ]
    }
    sqs_send = {
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [aws_sqs_queue.salesforce_sync_secondary[0].arn]
    }
    },
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "salesforce-scheduler" }
}

resource "aws_cloudwatch_event_rule" "salesforce_hourly_sync_secondary" {
  count    = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  name                = "${var.environment}-${var.app_name}-salesforce-hourly-sync"
  description         = "Trigger Salesforce sync scheduler hourly at :15 (secondary region)"
  schedule_expression = "cron(15 * * * ? *)"
  tags                = { Purpose = "salesforce-scheduler" }
}

resource "aws_cloudwatch_event_target" "salesforce_scheduler_secondary" {
  count    = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.salesforce_hourly_sync_secondary[0].name
  target_id = "salesforce-scheduler"
  arn       = module.salesforce_scheduler_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "salesforce_scheduler_eventbridge_secondary" {
  count    = var.enable_workspaces && var.enable_salesforce_integration && var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.salesforce_scheduler_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.salesforce_hourly_sync_secondary[0].arn
}
