# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# CloudWatch alarms -- estate coverage
# ============================================================================
#
# of the fable-code-review remediation. Before this file, the
# entire estate had five alarms: cloudwatch_symbol_graph.tf and four
# replication-DLQ alarms in eventbridge-replication + lambda-replication-
# applier. Nothing alarmed on integration sync DLQs, code-generation
# DLQ, file-ingestion DLQ, chat/query/KB Lambda errors, API Gateway
# 5xx, or Fargate health. Once lands (sync consumers switching
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
    # Integration sync DLQs (makes these actually receive
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
    "${var.environment}-${var.app_name}-salesforce-sync-dlq",

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
    "${var.environment}-${var.app_name}-chat-streaming",
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
    "${var.environment}-${var.app_name}-salesforce-integration",
    "${var.environment}-${var.app_name}-salesforce-sync",
    "${var.environment}-${var.app_name}-salesforce-scheduler",
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

# ----------------------------------------------------------------------------
# (code-map splitter): observability for merge fan-in failures.
#
# Two failure modes worth alarming on:
#
# 1. Merge DLQ'd after max attempts -- means shards never all landed
#    within the requeue-with-delay window. The Lambda Errors alarm
#    already covers this Lambda when raises happen, but the max-
#    attempts give-up path returns clean (marks the ledger failed +
#    logs error) rather than raising. The Errors alarm doesn't fire on
#    those; the metric filter below catches them.
#
# 2. Splitter drift -- the char/3 token estimator under-sized a shard
#    and the consumer had to fall back to in-handler sub-batching.
#    Not fatal but signals the estimator needs tuning.
# ----------------------------------------------------------------------------

resource "aws_cloudwatch_log_metric_filter" "merge_dlq_give_up" {
  name           = "${var.environment}-${var.app_name}-merge-dlq-give-up"
  log_group_name = "/aws/lambda/${var.environment}-${var.app_name}-process-batch-summary"
  pattern        = "\"Merge DLQ'd after\""

  metric_transformation {
    name          = "MergeGiveUpCount"
    namespace     = "OutcomeOps/CodeMap"
    value         = "1"
    default_value = "0"
  }
}

resource "aws_cloudwatch_metric_alarm" "merge_dlq_give_up" {
  alarm_name          = "${var.environment}-${var.app_name}-merge-dlq-give-up"
  alarm_description   = "merge fan-in gave up after max requeue attempts -- shard summaries never all landed, sharded code unit lost its unified summary. Per-shard summaries survive at shard-suffixed keys."
  namespace           = "OutcomeOps/CodeMap"
  metric_name         = "MergeGiveUpCount"
  statistic           = "Sum"
  period              = 900
  evaluation_periods  = 1
  threshold           = 0
  comparison_operator = "GreaterThanThreshold"
  treat_missing_data  = "notBreaching"

  alarm_actions = [module.audit_alerts_sns.topic_arn]
  ok_actions    = [module.audit_alerts_sns.topic_arn]

  tags = {
    Purpose = "merge-dlq-give-up-alarm"
  }
}

resource "aws_cloudwatch_log_metric_filter" "splitter_drift" {
  name           = "${var.environment}-${var.app_name}-splitter-drift"
  log_group_name = "/aws/lambda/${var.environment}-${var.app_name}-process-batch-summary"
  pattern        = "\"[splitter-drift]\""

  metric_transformation {
    name          = "SplitterDriftCount"
    namespace     = "OutcomeOps/CodeMap"
    value         = "1"
    default_value = "0"
  }
}

# Splitter drift is a warning-level signal; no alarm on it by default.
# Query CloudWatch metric OutcomeOps/CodeMap/SplitterDriftCount for a
# dashboard view. Add an alarm here later if the pattern becomes noisy
# enough to warrant paging.

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
