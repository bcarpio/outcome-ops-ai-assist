# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Secondary-region background Lambdas + SNS topics ()
# ----------------------------------------------------------------------------
# Mirror of:
#   - module.audit_alerts_sns                       (sns.tf)
#   - module.cost_alerts_sns                        (sns.tf)
#   - module.audit_alert_lambda                     (lambda-audit-alert.tf)
#   - module.analytics_aggregator_lambda            (lambda-analytics-aggregator.tf)
#   - module.analytics_cost_alert_lambda            (lambda-analytics-cost-alert.tf)
#
# Active/passive behavior:
# - audit-alert: gated on is_active_region() so only active fires SNS
#.
# - analytics-aggregator: NOT gated — both regions process their own
#   DDB stream into their own analytics-table. The replicated audit
#   rows arrive via dual_write, secondary's aggregator processes them,
#   secondary's analytics-table fills. Both regions see consistent
#   analytics regardless of role.
# - analytics-cost-alert: gated on is_active_region().
# - audit-stream-publisher: NOT mirrored — gated on
#   audit_stream_export_enabled which is false for the owner's prd.
#   When a customer turns on SIEM export AND HA, that's a follow-up
#   chunk that also needs to duplicate the Kinesis stream.
#
# All gated on var.secondary_region != "" plus their feature flag.
# ============================================================================

# ----------------------------------------------------------------------------
# Secondary-region SNS topics
# ----------------------------------------------------------------------------

# trivy:ignore:AVD-AWS-0136 -- CMK encryption is optional via enable_cmk_encryption toggle
module "audit_alerts_sns_secondary" {
  source  = "terraform-aws-modules/sns/aws"
  version = "7.1.0"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name              = "${var.environment}-${var.app_name}-audit-alerts"
  kms_master_key_id = var.enable_cmk_encryption ? aws_kms_key.data_at_rest_secondary[0].arn : "alias/aws/sns"

  subscriptions = var.audit_alert_email != "" ? {
    email = {
      protocol = "email"
      endpoint = var.audit_alert_email
    }
  } : {}

  tags = { Purpose = "audit-alerts" }
}

# trivy:ignore:AVD-AWS-0136 -- CMK encryption is optional via enable_cmk_encryption toggle
module "cost_alerts_sns_secondary" {
  source  = "terraform-aws-modules/sns/aws"
  version = "7.1.0"

  count = var.secondary_region != "" && var.cost_alert_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  name              = "${var.environment}-${var.app_name}-cost-alerts"
  kms_master_key_id = var.enable_cmk_encryption ? aws_kms_key.data_at_rest_secondary[0].arn : "alias/aws/sns"

  subscriptions = var.cost_alert_email != "" ? {
    email = {
      protocol = "email"
      endpoint = var.cost_alert_email
    }
  } : {}

  tags = { Purpose = "cost-alerts" }
}

# ----------------------------------------------------------------------------
# audit-alert (secondary) — DDB-stream-triggered refusal detector.
# is_active_region() gate keeps it silent while passive.
# ----------------------------------------------------------------------------

module "audit_alert_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-audit-alert"
  role_name     = "${var.environment}-${var.app_name}-audit-alert-secondary"
  description   = "Send SNS alerts for AI refusals (secondary region)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 128

  source_path = [
    { path = "${path.module}/../lambda/audit-alert", pip_requirements = false },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV           = var.environment
    APP_NAME      = var.app_name
    SNS_TOPIC_ARN = module.audit_alerts_sns_secondary[0].topic_arn
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    sns_publish = {
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [module.audit_alerts_sns_secondary[0].topic_arn]
    }
    dynamodb_stream = {
      effect    = "Allow"
      actions   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
      resources = [module.audit_logs_table_secondary[0].dynamodb_table_stream_arn]
    }
    },
    # Secondary topics use the data-at-rest CMK when enable_cmk_encryption is
    # set (alias/aws/sns otherwise, which needs no identity grant). Publishing
    # to a CMK-encrypted topic needs GenerateDataKey + Decrypt on that key.
    var.enable_cmk_encryption ? {
      sns_kms = {
        effect    = "Allow"
        actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
        resources = [local.data_cmk_arn_secondary]
      }
    } : {},
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "audit-alert" }
}

resource "aws_lambda_event_source_mapping" "audit_logs_stream_secondary" {
  count    = var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  event_source_arn  = module.audit_logs_table_secondary[0].dynamodb_table_stream_arn
  function_name     = module.audit_alert_lambda_secondary[0].lambda_function_name
  starting_position = "LATEST"
  batch_size        = 10
  enabled           = true

  maximum_retry_attempts        = 3
  maximum_record_age_in_seconds = 3600

  destination_config {
    on_failure {
      destination_arn = module.audit_alerts_sns_secondary[0].topic_arn
    }
  }

  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            status = {
              S = ["refusal"]
            }
          }
        }
      })
    }
  }
}

# ----------------------------------------------------------------------------
# analytics-aggregator (secondary) — DDB-stream-triggered per-day
# roll-up writer. NOT gated on is_active_region(): both regions
# independently aggregate their own replicated audit stream into their
# own analytics-table.
# ----------------------------------------------------------------------------

module "analytics_aggregator_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-analytics-aggregator"
  role_name     = "${var.environment}-${var.app_name}-analytics-aggregator-secondary"
  description   = "Aggregate audit-log writes into daily analytics rolls (secondary region)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/analytics-aggregator", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  # (HA cleanup): AUDIT_TABLE removed (dead -- handler doesn't
  # reference it; it consumes the audit-logs stream, not the table).
  environment_variables = {
    ENV             = var.environment
    APP_NAME        = var.app_name
    ANALYTICS_TABLE = module.analytics_table_secondary[0].dynamodb_table_id
  }

  attach_policy_statements = true
  policy_statements = {
    dynamodb_stream = {
      effect    = "Allow"
      actions   = ["dynamodb:GetRecords", "dynamodb:GetShardIterator", "dynamodb:DescribeStream", "dynamodb:ListStreams"]
      resources = [module.audit_logs_table_secondary[0].dynamodb_table_stream_arn]
    }
    dynamodb_analytics_write = {
      effect = "Allow"
      # (HA parity): add PutItem to match primary. UpdateItem-only
      # was tight but incomplete -- initial-row inserts (no prior row to
      # ADD-attribute against) fell through to a fresh row via PutItem in
      # the handler's fallback path.
      actions   = ["dynamodb:GetItem", "dynamodb:UpdateItem", "dynamodb:PutItem"]
      resources = [module.analytics_table_secondary[0].dynamodb_table_arn]
    }
  }

  tags = { Purpose = "analytics-aggregator" }
}

resource "aws_lambda_event_source_mapping" "analytics_aggregator_stream_secondary" {
  count    = var.secondary_region != "" ? 1 : 0
  provider = aws.secondary

  event_source_arn        = module.audit_logs_table_secondary[0].dynamodb_table_stream_arn
  function_name           = module.analytics_aggregator_lambda_secondary[0].lambda_function_name
  starting_position       = "LATEST"
  batch_size              = 25
  enabled                 = true
  function_response_types = ["ReportBatchItemFailures"]

  # (HA parity): mirror primary retry bounds. Without these a
  # poison record could stall the shard forever.
  maximum_retry_attempts        = 3
  maximum_record_age_in_seconds = 3600

  # (HA parity): mirror primary's SYSTEM-exclusion filter so
  # system-level audit rows (workspace_id="" or "SYSTEM") don't get
  # aggregated as if they were tenant traffic.
  filter_criteria {
    filter {
      pattern = jsonencode({
        eventName = ["INSERT"]
        dynamodb = {
          NewImage = {
            workspace_id = {
              S = [{ "anything-but" = ["", "SYSTEM"] }]
            }
          }
        }
      })
    }
  }
}

# ----------------------------------------------------------------------------
# analytics-cost-alert (secondary) — daily-cron cost-threshold alerter.
# is_active_region() gate keeps it silent while passive.
# ----------------------------------------------------------------------------

module "analytics_cost_alert_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" && var.cost_alert_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-analytics-cost-alert"
  role_name     = "${var.environment}-${var.app_name}-analytics-cost-alert-secondary"
  description   = "Daily cost-threshold alert (secondary region)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 120
  memory_size   = 256

  source_path = [
    { path = "${path.module}/../lambda/analytics-cost-alert", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge({
    ENV                   = var.environment
    APP_NAME              = var.app_name
    SNS_TOPIC_ARN         = module.cost_alerts_sns_secondary[0].topic_arn
    ANALYTICS_TABLE       = module.analytics_table_secondary[0].dynamodb_table_id
    WORKSPACES_TABLE      = module.workspaces_table_secondary[0].dynamodb_table_id
    AUDIT_TABLE           = module.audit_logs_table_secondary[0].dynamodb_table_id
    AUDIT_RETENTION_DAYS  = tostring(var.audit_retention_days)
    ORG_MONTHLY_USD       = tostring(var.cost_alert_org_monthly_usd)
    WORKSPACE_MONTHLY_USD = tostring(var.cost_alert_workspace_monthly_usd)
    },
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    sns_publish = {
      effect    = "Allow"
      actions   = ["sns:Publish"]
      resources = [module.cost_alerts_sns_secondary[0].topic_arn]
    }
    dynamodb_analytics = {
      effect = "Allow"
      # (HA alignment): drop Scan (handler doesn't scan
      # analytics), add PutItem (matches handler's alert-fired
      # bookkeeping). Now converges with primary.
      actions   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:PutItem"]
      resources = [module.analytics_table_secondary[0].dynamodb_table_arn]
    }
    dynamodb_workspaces = {
      effect    = "Allow"
      actions   = ["dynamodb:GetItem", "dynamodb:Scan"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    dynamodb_audit = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem"]
      resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
    }
    },
    # See audit_alert_lambda_secondary above: CMK-encrypted topic publish.
    var.enable_cmk_encryption ? {
      sns_kms = {
        effect    = "Allow"
        actions   = ["kms:GenerateDataKey*", "kms:Decrypt"]
        resources = [local.data_cmk_arn_secondary]
      }
    } : {},
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "analytics-cost-alert" }
}

resource "aws_cloudwatch_event_rule" "analytics_cost_alert_daily_secondary" {
  count    = var.secondary_region != "" && var.cost_alert_enabled ? 1 : 0
  provider = aws.secondary

  name        = "${var.environment}-${var.app_name}-analytics-cost-alert-daily"
  description = "Daily roll-up cost-threshold check (secondary region)"
  # (HA alignment): use the same tfvar-driven schedule as the
  # primary; the hardcoded rate(24 hours) was a silent drift.
  schedule_expression = var.cost_alert_schedule_expression
  tags                = { Purpose = "analytics-cost-alert" }
}

resource "aws_cloudwatch_event_target" "analytics_cost_alert_secondary" {
  count    = var.secondary_region != "" && var.cost_alert_enabled ? 1 : 0
  provider = aws.secondary

  rule      = aws_cloudwatch_event_rule.analytics_cost_alert_daily_secondary[0].name
  target_id = "analytics-cost-alert"
  arn       = module.analytics_cost_alert_lambda_secondary[0].lambda_function_arn
}

resource "aws_lambda_permission" "analytics_cost_alert_eventbridge_secondary" {
  count    = var.secondary_region != "" && var.cost_alert_enabled ? 1 : 0
  provider = aws.secondary

  statement_id  = "AllowEventBridgeInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.analytics_cost_alert_lambda_secondary[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.analytics_cost_alert_daily_secondary[0].arn
}

# ----------------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------------

output "audit_alerts_sns_topic_arn_secondary" {
  description = "ARN of the secondary-region audit alerts SNS topic (null when HA disabled)"
  value       = var.secondary_region != "" ? module.audit_alerts_sns_secondary[0].topic_arn : null
}

output "cost_alerts_sns_topic_arn_secondary" {
  description = "ARN of the secondary-region cost alerts SNS topic (null when HA / cost-alert disabled)"
  value       = (var.secondary_region != "" && var.cost_alert_enabled) ? module.cost_alerts_sns_secondary[0].topic_arn : null
}
