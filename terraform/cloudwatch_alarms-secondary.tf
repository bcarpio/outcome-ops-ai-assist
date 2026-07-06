# ============================================================================
# CloudWatch alarms -- secondary region
# ============================================================================
#
# Mirror of terraform/cloudwatch_alarms.tf under aws.secondary. Same
# DLQ + Lambda error + ECS health coverage, minus the outcome_ops_api
# alarm because that API Gateway is primary-only per ADR-043.
#
# All resources gated on `secondary_region != ""` so single-region
# deploys skip the block entirely.
# ============================================================================

locals {
  dlq_alarm_queues_secondary = [
    "${var.environment}-${var.app_name}-github-sync-dlq",
    "${var.environment}-${var.app_name}-confluence-sync-dlq",
    "${var.environment}-${var.app_name}-jira-sync-dlq",
    "${var.environment}-${var.app_name}-outlook-sync-dlq",
    "${var.environment}-${var.app_name}-teams-sync-dlq",
    "${var.environment}-${var.app_name}-sharepoint-sync-dlq",
    "${var.environment}-${var.app_name}-onedrive-sync-dlq",
    "${var.environment}-${var.app_name}-onenote-sync-dlq",
    "${var.environment}-${var.app_name}-database-sync-dlq",
    "${var.environment}-${var.app_name}-azure-devops-sync-dlq",
    "${var.environment}-${var.app_name}-azure-devops-boards-sync-dlq",
    "${var.environment}-${var.app_name}-file-ingestion-dlq.fifo",
    "${var.environment}-${var.app_name}-delete-dlq",
    "${var.environment}-${var.app_name}-code-maps-dlq",
    "${var.environment}-${var.app_name}-repo-summaries-dlq.fifo",
    # NB: pr-checks-dlq + code-generation-dlq are primary-only per
    # ADR-043 -- their secondary queues no longer exist.
  ]

  # NB: generate-code, analyze-pr are primary-only per ADR-043 --
  # excluded from the secondary error alarm set.
  lambda_error_alarm_functions_secondary = [
    "${var.environment}-${var.app_name}-chat",
    "${var.environment}-${var.app_name}-workspace-management",
    "${var.environment}-${var.app_name}-file-ingestion",
    "${var.environment}-${var.app_name}-process-batch-summary",
    "${var.environment}-${var.app_name}-process-repo-summary",
    "${var.environment}-${var.app_name}-delete-worker",
    "${var.environment}-${var.app_name}-replication-applier",
    "${var.environment}-${var.app_name}-github-sync",
    "${var.environment}-${var.app_name}-confluence-sync",
    "${var.environment}-${var.app_name}-jira-sync",
    "${var.environment}-${var.app_name}-outlook-sync",
    "${var.environment}-${var.app_name}-teams-sync",
    "${var.environment}-${var.app_name}-sharepoint-sync",
    "${var.environment}-${var.app_name}-onedrive-sync",
    "${var.environment}-${var.app_name}-onenote-sync",
    "${var.environment}-${var.app_name}-database-sync",
    "${var.environment}-${var.app_name}-azure-devops-sync",
    "${var.environment}-${var.app_name}-azure-devops-boards-sync",
  ]
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth_secondary" {
  for_each = var.secondary_region != "" ? toset(local.dlq_alarm_queues_secondary) : toset([])

  provider = aws.secondary

  alarm_name          = "${each.value}-depth"
  alarm_description   = "SQS ${each.value} has messages waiting (secondary region)."
  namespace           = "AWS/SQS"
  metric_name         = "ApproximateNumberOfMessagesVisible"
  statistic           = "Maximum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = each.value
  }

  alarm_actions = [module.audit_alerts_sns_secondary[0].topic_arn]
  ok_actions    = [module.audit_alerts_sns_secondary[0].topic_arn]

  tags = {
    Purpose = "dlq-depth-alarm-secondary"
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors_secondary" {
  for_each = var.secondary_region != "" ? toset(local.lambda_error_alarm_functions_secondary) : toset([])

  provider = aws.secondary

  alarm_name          = "${each.value}-errors"
  alarm_description   = "Lambda ${each.value} has raised errors in the last 15 minutes (secondary region)."
  namespace           = "AWS/Lambda"
  metric_name         = "Errors"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    FunctionName = each.value
  }

  alarm_actions = [module.audit_alerts_sns_secondary[0].topic_arn]
  ok_actions    = [module.audit_alerts_sns_secondary[0].topic_arn]

  tags = {
    Purpose = "lambda-error-alarm-secondary"
  }
}

resource "aws_cloudwatch_metric_alarm" "ui_ecs_running_count_secondary" {
  count = var.deploy_ui && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  alarm_name          = "${var.environment}-${var.app_name}-ui-running-count-secondary"
  alarm_description   = "UI Fargate service RunningCount below desired (secondary region)."
  namespace           = "AWS/ECS"
  metric_name         = "RunningTaskCount"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = module.ui_ecs_secondary[0].cluster_name
    ServiceName = "${var.environment}-${var.app_name}-ui"
  }

  alarm_actions = [module.audit_alerts_sns_secondary[0].topic_arn]
  ok_actions    = [module.audit_alerts_sns_secondary[0].topic_arn]

  tags = {
    Purpose = "ui-ecs-health-alarm-secondary"
  }
}
