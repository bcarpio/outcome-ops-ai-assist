# ============================================================================
# CloudWatch alarms for the symbol-graph retrieval path (plan-gkg chunk 08)
#
# The plan's "Query-latency monitoring" section calls for a single alarm:
#
#   Graph query p99 latency: target <50ms, alarm at >200ms.
#
# Latency is published by the chunk-04 reader as a custom metric in the
# ``OutcomeOps/SymbolGraph`` namespace, metric name ``QueryLatency``. The
# alarm wakes operators when retrieval slows beyond the plan target so
# we can investigate before chat / analyze-pr / generate-code start
# eating that 150ms+ delta on every structural turn.
#
# The plan deliberately does NOT add chat-end-to-end or
# analyze-pr-end-to-end alarms here -- those are covered by the
# feature-agnostic Lambda duration alarms each Lambda module already
# owns. This file is graph-only.
#
# Notification routing: per the plan ("Alarms route to the existing
# analytics-cost-alert SNS topic"), we publish into ``cost_alerts_sns``,
# which is the topic the ``analytics-cost-alert`` Lambda already
# publishes its threshold-crossing emails to. The topic is gated on
# ``var.cost_alert_enabled``; when cost alerts are disabled, the alarm
# is still created but has no actions (matches existing behaviour for
# other optional-SNS resources).
#
# Naming + tagging conform to ADR-001 (snake_case file, Purpose tag).
# ============================================================================

resource "aws_cloudwatch_metric_alarm" "symbol_graph_query_latency_p99" {
  alarm_name        = "${var.environment}-${var.app_name}-symbol-graph-query-latency-p99"
  alarm_description = "p99 latency for the chunk-04 symbol-graph reader exceeded 200ms (plan-gkg chunk 08 target: <50ms). Investigate DDB throttling / item-size regression / cold-start spikes before chat + analyze-pr eat the delta."

  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 2
  threshold           = 200 # milliseconds
  treat_missing_data  = "notBreaching"

  metric_name        = "QueryLatency"
  namespace          = "OutcomeOps/SymbolGraph"
  period             = 60
  extended_statistic = "p99"
  unit               = "Milliseconds"

  # Route to the cost-alert SNS topic (per plan-gkg/08 "Alarms route to
  # the existing analytics-cost-alert SNS topic"). When cost_alert_enabled
  # is false the topic doesn't exist and the alarm has no actions; the
  # alarm still raises in the AWS console for visibility.
  alarm_actions = var.cost_alert_enabled ? [module.cost_alerts_sns[0].topic_arn] : []
  ok_actions    = var.cost_alert_enabled ? [module.cost_alerts_sns[0].topic_arn] : []

  tags = {
    Purpose = "symbol-graph"
  }
}

output "symbol_graph_query_latency_alarm_arn" {
  description = "ARN of the symbol-graph query-latency p99 alarm (plan-gkg chunk 08)"
  value       = aws_cloudwatch_metric_alarm.symbol_graph_query_latency_p99.arn
}
