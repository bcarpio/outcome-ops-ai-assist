# ============================================================================
# SNS Topics
# ============================================================================

# trivy:ignore:AVD-AWS-0136 -- CMK encryption is optional via enable_cmk_encryption toggle
module "audit_alerts_sns" {
  source  = "terraform-aws-modules/sns/aws"
  version = "~> 7.1"

  name              = "${var.environment}-${var.app_name}-audit-alerts"
  kms_master_key_id = var.enable_cmk_encryption ? aws_kms_key.data_at_rest[0].arn : "alias/aws/sns"

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
