# ============================================================================
# Database-drift SNS topic (chunk 07)
#
# Per-environment topic that the database-sync Lambda publishes to when a
# sync detects schemas or tables that were added or removed against the
# connection's prior-sync snapshot. Steady-state syncs (no drift) publish
# nothing - admins only hear from this topic when something genuinely
# changed.
#
# Created conditionally: empty `database_drift_notification_emails` =
# no topic, no subscriptions, no IAM publish-grant on the Lambda. The
# DATABASE_SYNCED audit row still carries `schemas_added` /
# `schemas_removed` / `tables_added` / `tables_removed` regardless, so
# downstream alerting via the existing audit pipeline still works.
#
# Customers who want non-email subscribers (Slack-webhook Lambda, SQS,
# PagerDuty) subscribe their own endpoint to the topic ARN exported below
# after `terraform apply`.
# ============================================================================

variable "database_drift_notification_emails" {
  type        = list(string)
  default     = []
  description = <<-EOT
    Email addresses subscribed to the database-drift SNS topic.
    Notifications fire when a sync detects schemas or tables that
    were added or removed against the prior-sync snapshot.

    Empty list = no SNS topic created, no notifications. The
    DATABASE_SYNCED audit row still carries the schemas_added /
    tables_added / etc. fields regardless, so audit-table polling
    or downstream alerting via the existing audit pipeline still
    works.

    Subscribers receive plain-text emails listing the drift. The
    topic ARN is also exported as a terraform output for customers
    who want to subscribe additional endpoints (Lambda for
    Slack-webhook transform, SQS for queue routing, etc.).
  EOT
}

# trivy:ignore:AVD-AWS-0136 -- KMS-encrypted via the AWS-managed alias/aws/sns
resource "aws_sns_topic" "database_drift" {
  count = length(var.database_drift_notification_emails) > 0 ? 1 : 0

  name              = "${var.environment}-${var.app_name}-database-drift"
  kms_master_key_id = "alias/aws/sns"

  tags = {
    Environment = var.environment
    Purpose     = "database-drift-notifications"
  }
}

resource "aws_sns_topic_subscription" "database_drift_emails" {
  for_each = length(aws_sns_topic.database_drift) > 0 ? toset(var.database_drift_notification_emails) : toset([])

  topic_arn = aws_sns_topic.database_drift[0].arn
  protocol  = "email"
  endpoint  = each.value
}

output "database_drift_topic_arn" {
  description = "Database-drift SNS topic ARN; empty string when no subscribers configured."
  value       = length(aws_sns_topic.database_drift) > 0 ? aws_sns_topic.database_drift[0].arn : ""
}
