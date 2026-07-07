# ============================================================================
# CloudWatch alarms -- estate coverage
# ============================================================================
#
# Chunk 12 of the fable-code-review remediation. Before this file, the
# entire estate had five alarms: cloudwatch_symbol_graph.tf and four
# replication-DLQ alarms in eventbridge-replication + lambda-replication-
# applier. Nothing alarmed on integration sync DLQs, code-generation
# DLQ, file-ingestion DLQ, chat/query/KB Lambda errors, API Gateway
# 5xx, or Fargate health. Once chunk 02 lands (sync consumers switching
# to partial-batch-failure semantics), real failures start hitting DLQs
# and nobody was told.
#
# The design keeps this file curated, not exhaustive: for_each over a
# locals map per class of alarm. Adding a new DLQ or a new critical
# Lambda is one line in the map. Cost: ~30 standard alarms per region
# @ $0.10 each ~= $3/mo.
#
# Alarm action target reuses `module.audit_alerts_sns` -- customers
# already subscribe an operational email to it; that channel is the
# right place for "DLQ depth > 0" and "chat errors" too. If a customer
# wants a separate ops-alerts channel later, split the target into
# per-class SNS topics; the map shape stays.
# ============================================================================

locals {
  # DLQ alarms. Alarm on ApproximateNumberOfMessagesVisible > 0 across
  # two 5-minute periods so a single transient burst doesn't page.
  # Queue name is the full CFN name (as terraform generates it) so the
  # dimension resolves whether the underlying queue is a module or a
  # raw aws_sqs_queue.
  dlq_alarm_queues = [
    # Integration sync DLQs (chunk 02 makes these actually receive
    # messages via partial-batch-failure semantics).
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
    "${var.environment}-${var.app_name}-servicenow-sync-dlq",

    # Cross-family DLQs.
    "${var.environment}-${var.app_name}-file-ingestion-dlq.fifo",
    "${var.environment}-${var.app_name}-delete-dlq",
    "${var.environment}-${var.app_name}-code-maps-dlq",
    "${var.environment}-${var.app_name}-pr-checks-dlq.fifo",
    "${var.environment}-${var.app_name}-code-generation-dlq.fifo",
    "${var.environment}-${var.app_name}-repo-summaries-dlq.fifo",
  ]

  # Critical Lambda functions whose Errors > 0 in a 15-min window is
  # worth waking someone up. Deliberately NOT "every Lambda" -- 104
  # entries is picker fatigue for on-call and buries the actual
  # signal. Add here as new critical functions land.
  lambda_error_alarm_functions = [
    "${var.environment}-${var.app_name}-chat",
    "${var.environment}-${var.app_name}-query-kb",
    "${var.environment}-${var.app_name}-ask-claude",
    "${var.environment}-${var.app_name}-workspace-management",
    "${var.environment}-${var.app_name}-file-ingestion",
    "${var.environment}-${var.app_name}-process-batch-summary",
    "${var.environment}-${var.app_name}-process-repo-summary",
    "${var.environment}-${var.app_name}-generate-code",
    "${var.environment}-${var.app_name}-analyze-pr",
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
    "${var.environment}-${var.app_name}-servicenow-integration",
    "${var.environment}-${var.app_name}-servicenow-sync",
    "${var.environment}-${var.app_name}-servicenow-scheduler",
  ]
}

resource "aws_cloudwatch_metric_alarm" "dlq_depth" {
  for_each = toset(local.dlq_alarm_queues)

  alarm_name          = "${each.value}-depth"
  alarm_description   = "SQS ${each.value} has messages waiting -- something is failing to process."
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

  alarm_actions = [module.audit_alerts_sns.topic_arn]
  ok_actions    = [module.audit_alerts_sns.topic_arn]

  tags = {
    Purpose = "dlq-depth-alarm"
  }
}

resource "aws_cloudwatch_metric_alarm" "lambda_errors" {
  for_each = toset(local.lambda_error_alarm_functions)

  alarm_name          = "${each.value}-errors"
  alarm_description   = "Lambda ${each.value} has raised errors in the last 15 minutes."
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

  alarm_actions = [module.audit_alerts_sns.topic_arn]
  ok_actions    = [module.audit_alerts_sns.topic_arn]

  tags = {
    Purpose = "lambda-error-alarm"
  }
}

# API Gateway v2 5xx (outcome_ops_api) -- primary-only per ADR-043.
# Gated on enable_github_issue_integration since that flag controls
# whether the API Gateway exists at all.
resource "aws_cloudwatch_metric_alarm" "outcome_ops_api_5xx" {
  count = var.enable_github_issue_integration ? 1 : 0

  alarm_name          = "${var.environment}-${var.app_name}-outcome-ops-api-5xx"
  alarm_description   = "outcome_ops_api API Gateway v2 returning 5xx responses -- likely code-gen webhook failures."
  namespace           = "AWS/ApiGateway"
  metric_name         = "5xx"
  statistic           = "Sum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 5
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  dimensions = {
    ApiId = module.outcome_ops_api[0].api_id
  }

  alarm_actions = [module.audit_alerts_sns.topic_arn]
  ok_actions    = [module.audit_alerts_sns.topic_arn]

  tags = {
    Purpose = "api-gateway-5xx-alarm"
  }
}

# UI Fargate service health -- if RunningCount drops below Desired for
# more than 10 minutes, something has crashed and ECS is not recovering.
resource "aws_cloudwatch_metric_alarm" "ui_ecs_running_count" {
  count = var.deploy_ui ? 1 : 0

  alarm_name          = "${var.environment}-${var.app_name}-ui-running-count"
  alarm_description   = "UI Fargate service RunningCount below desired -- crash loop or ENI failure."
  namespace           = "AWS/ECS"
  metric_name         = "RunningTaskCount"
  statistic           = "Minimum"
  period              = 300
  evaluation_periods  = 2
  threshold           = 1
  comparison_operator = "LessThanThreshold"
  treat_missing_data  = "breaching"

  dimensions = {
    ClusterName = module.ui_ecs.cluster_name
    ServiceName = "${var.environment}-${var.app_name}-ui"
  }

  alarm_actions = [module.audit_alerts_sns.topic_arn]
  ok_actions    = [module.audit_alerts_sns.topic_arn]

  tags = {
    Purpose = "ui-ecs-health-alarm"
  }
}
