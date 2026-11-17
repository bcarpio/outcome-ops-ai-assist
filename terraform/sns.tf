# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# SNS Topics
# ============================================================================

# Dedicated CMK for the alerts SNS topics. CloudWatch alarms cannot publish
# to SNS topics encrypted with the AWS-managed `alias/aws/sns` key -- the
# CloudWatch service principal has no permission to use that key, so the
# alarm silently fails at `kms:GenerateDataKey` before SNS ever sees the
# publish call (verified in prd 2026-07-07: alarm state = ALARM,
# `NumberOfMessagesPublished` metric empty, no email delivered). This CMK
# grants CloudWatch + EventBridge the two actions they need so alarm
# notifications actually go out. Kept separate from `data_at_rest` so
# customers who opt out of general CMK encryption still get working
# alarm delivery.
resource "aws_kms_key" "sns_alerts" {
  description             = "CMK for ${var.environment}-${var.app_name} alerts SNS topics (CloudWatch-alarm compatible)"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "AllowAccountAdmin"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "AllowCloudWatchAlarmsToUseKey"
        Effect = "Allow"
        Principal = {
          Service = "cloudwatch.amazonaws.com"
        }
        Action = [
          "kms:GenerateDataKey*",
          "kms:Decrypt",
        ]
        Resource = "*"
      },
      {
        Sid    = "AllowEventBridgeToUseKey"
        Effect = "Allow"
        Principal = {
          Service = "events.amazonaws.com"
        }
        Action = [
          "kms:GenerateDataKey*",
          "kms:Decrypt",
        ]
        Resource = "*"
      },
    ]
  })

  tags = {
    Purpose = "sns-alerts"
  }
}

resource "aws_kms_alias" "sns_alerts" {
  name          = "alias/${var.environment}-${var.app_name}-sns-alerts"
  target_key_id = aws_kms_key.sns_alerts.key_id
}

module "audit_alerts_sns" {
  source  = "terraform-aws-modules/sns/aws"
  version = "7.1.0"

  name              = "${var.environment}-${var.app_name}-audit-alerts"
  kms_master_key_id = aws_kms_key.sns_alerts.arn

  # Email subscription (only created when email is provided)
  subscriptions = var.audit_alert_email != "" ? {
    email = {
      protocol = "email"
      endpoint = var.audit_alert_email
    }
  } : {}

  tags = {
    Purpose = "audit-alerts"
  }
}

# State migration: existing SNS topic moves into the module
moved {
  from = aws_sns_topic.audit_alerts
  to   = module.audit_alerts_sns.aws_sns_topic.this[0]
}

output "audit_alerts_sns_topic_arn" {
  description = "ARN of the audit alerts SNS topic"
  value       = module.audit_alerts_sns.topic_arn
}

module "cost_alerts_sns" {
  source  = "terraform-aws-modules/sns/aws"
  version = "7.1.0"

  count = var.cost_alert_enabled ? 1 : 0

  name = "${var.environment}-${var.app_name}-cost-alerts"
  # Uses the same CloudWatch-alarm-compatible CMK as the audit-alerts
  # topic (see aws_kms_key.sns_alerts above for rationale).
  kms_master_key_id = aws_kms_key.sns_alerts.arn

  subscriptions = var.cost_alert_email != "" ? {
    email = {
      protocol = "email"
      endpoint = var.cost_alert_email
    }
  } : {}

  tags = {
    Purpose = "cost-alerts"
  }
}

output "cost_alerts_sns_topic_arn" {
  description = "ARN of the cost alerts SNS topic (when enabled)"
  value       = var.cost_alert_enabled ? module.cost_alerts_sns[0].topic_arn : null
}
