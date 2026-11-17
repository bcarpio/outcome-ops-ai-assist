# ============================================================================
# Analytics Cost Alert Lambda
# Scheduled daily; reads the analytics aggregates table to compute MTD spend
# per workspace and org-wide. Publishes to the cost-alerts SNS topic the
# first time a threshold is crossed in the current month.
#
# Enabled via var.cost_alert_enabled. Thresholds (org and per-workspace) are
# configured via var.cost_alert_org_monthly_usd and
# var.cost_alert_workspace_monthly_usd; set either to 0 to disable.
# ============================================================================

module "analytics_cost_alert_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.cost_alert_enabled ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-analytics-cost-alert"
  description   = "Publish cost-alert SNS notifications when workspace/org budgets are crossed"
  handler       = "handler.handler"
  runtime       = "python3.12"
  # chunk 10 (HA alignment): bumped 60->120 to match secondary. Cost
  # aggregation across a full month of analytics rows can push past 60s
  # at enterprise scale.
  timeout     = 120
  memory_size = 256

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  source_path = [
    {
      path             = "${path.module}/../lambda/analytics-cost-alert"
      pip_requirements = true
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  quiet_archive_local_exec = true

  # chunk 10 (HA parity): merge ha_scheduler_env so is_active_region()
  # gates SNS publishes on the primary too. Without it a demoted-but-
  # alive primary + promoted secondary both publish cost-threshold
  # alerts (double-email scenario the gate exists to prevent).
  environment_variables = merge(
    {
      ENV                   = var.environment
      APP_NAME              = var.app_name
      ANALYTICS_TABLE       = module.analytics_table.dynamodb_table_id
      WORKSPACES_TABLE      = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_id : ""
      SNS_TOPIC_ARN         = module.cost_alerts_sns[0].topic_arn
      ORG_MONTHLY_USD       = tostring(var.cost_alert_org_monthly_usd)
      WORKSPACE_MONTHLY_USD = tostring(var.cost_alert_workspace_monthly_usd)
      AUDIT_TABLE           = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS  = tostring(var.audit_retention_days)
    },
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_analytics = {
        effect = "Allow"
        # chunk 10 (HA alignment): grant the union of what the handler
        # actually uses (query + put_item on analytics; scan + get_item
        # on workspaces). Primary was PutItem-only for analytics; adding
        # Scan is a no-op for analytics since the handler doesn't scan
        # it -- kept out. Secondary had Scan on analytics erroneously;
        # chunk 10 drops it there.
        actions = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:PutItem"
        ]
        resources = [
          module.analytics_table.dynamodb_table_arn
        ]
      }

      dynamodb_workspaces = {
        effect = "Allow"
        actions = [
          "dynamodb:Scan",
          "dynamodb:GetItem"
        ]
        resources = var.enable_workspaces ? [
          module.workspaces_table[0].dynamodb_table_arn
        ] : []
      }

      sns_publish = {
        effect = "Allow"
        actions = [
          "sns:Publish"
        ]
        resources = [
          module.cost_alerts_sns[0].topic_arn
        ]
      }

      # DynamoDB audit log writes (plan/audit-completeness)
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
    # chunk 10 (HA parity): appconfig:GetLatestConfiguration on the
    # primary AppConfig so is_active_region() can gate SNS publishes.
    local.ha_scheduler_iam,
  )

  # Permission for EventBridge to invoke the Lambda. The module's
  # allowed_triggers shortcut only works when the function is published as a
  # numbered version; we keep the function on $LATEST, so we attach the
  # permission directly against the unqualified function ARN.
  create_current_version_allowed_triggers = false

  tags = {
    Purpose = "analytics-cost-alert"
  }
}

resource "aws_lambda_permission" "analytics_cost_alert_eventbridge" {
  count = var.cost_alert_enabled ? 1 : 0

  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.analytics_cost_alert_lambda[0].lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.analytics_cost_alert_schedule[0].arn
}

resource "aws_cloudwatch_event_rule" "analytics_cost_alert_schedule" {
  count = var.cost_alert_enabled ? 1 : 0

  name                = "${var.environment}-${var.app_name}-cost-alert"
  description         = "Daily schedule for analytics cost alert Lambda"
  schedule_expression = var.cost_alert_schedule_expression

  tags = {
    Purpose = "analytics-cost-alert"
  }
}

resource "aws_cloudwatch_event_target" "analytics_cost_alert_target" {
  count = var.cost_alert_enabled ? 1 : 0

  rule      = aws_cloudwatch_event_rule.analytics_cost_alert_schedule[0].name
  target_id = "analytics-cost-alert"
  arn       = module.analytics_cost_alert_lambda[0].lambda_function_arn
}

output "analytics_cost_alert_lambda_arn" {
  description = "ARN of the analytics cost-alert Lambda (when enabled)"
  value       = var.cost_alert_enabled ? module.analytics_cost_alert_lambda[0].lambda_function_arn : null
}
