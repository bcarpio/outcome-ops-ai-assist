# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# EventBridge cross-region replication transport (HA mode)
# ----------------------------------------------------------------------------
# Each region runs an event bus named ${env}-${app}-replication. The
# dual-write helper (chunk 04) PutEvents to its LOCAL bus; a rule on the
# local bus forwards every event to the OTHER region's bus; the
# replicator-applier Lambda (chunk 05) subscribes to its local bus and
# applies the event to local DDB / S3 / S3 Vectors.
#
# Symmetric: both regions have the same bus + rule + DLQ + alarm + IAM
# shape. The failover flip determines which direction is actually
# emitting events (the active region writes; the passive region's local
# bus just sits there until the active region's writer fires).
#
# Single-account, two-region: bus policy trusts the same account but
# scopes by source rule ARN. Cross-account customers would need to widen
# the bus policy's Principal — not in scope today.
# ============================================================================

# ----------------------------------------------------------------------------
# Primary region bus + cross-region replication to secondary
# ----------------------------------------------------------------------------

resource "aws_cloudwatch_event_bus" "replication" {
  name = "${var.environment}-${var.app_name}-replication"

  tags = {
    Purpose = "ha-replication"
  }
}

# Bus policy: allow the secondary region's replicate-to-primary rule to
# PutEvents into this bus.
resource "aws_cloudwatch_event_bus_policy" "replication_inbound_from_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  event_bus_name = aws_cloudwatch_event_bus.replication.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowSecondaryRegionToPutEvents"
        Effect    = "Allow"
        Principal = { AWS = data.aws_caller_identity.current.account_id }
        Action    = "events:PutEvents"
        Resource  = aws_cloudwatch_event_bus.replication.arn
        Condition = {
          StringEquals = {
            "aws:SourceArn" = "arn:aws:events:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:rule/${aws_cloudwatch_event_bus.replication.name}/replicate-to-primary"
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "replicate_to_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  name           = "replicate-to-secondary"
  event_bus_name = aws_cloudwatch_event_bus.replication.name
  description    = "Forward HA replication events to the secondary region"

  # detail.source_region filter prevents loops: only forward events that
  # originated in primary. Without it, an event forwarded primary->secondary
  # would also match the symmetric replicate-to-primary rule on secondary's
  # bus and bounce back, hitting EventBridge's THIRD_REGION_HOP guard.
  event_pattern = jsonencode({
    source = ["${var.environment}.${var.app_name}.ha-replication"]
    detail = {
      source_region = [var.aws_region]
    }
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_target" "replicate_to_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  rule           = aws_cloudwatch_event_rule.replicate_to_secondary[0].name
  event_bus_name = aws_cloudwatch_event_bus.replication.name
  target_id      = "replicate-to-secondary"

  arn      = "arn:aws:events:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:event-bus/${aws_cloudwatch_event_bus.replication.name}"
  role_arn = aws_iam_role.event_bus_cross_region_putter_to_secondary[0].arn

  dead_letter_config {
    arn = aws_sqs_queue.replication_to_secondary_dlq[0].arn
  }

  # retry_policy intentionally omitted: EventBridge rejects it for
  # event-bus-to-event-bus targets (only invocation targets accept it).
  # AWS uses default 24h retry / 185 attempts under the hood anyway,
  # which matches what we'd have configured. Beyond that the DLQ above
  # catches the event for ha-replay-dlq to drain.
}

resource "aws_iam_role" "event_bus_cross_region_putter_to_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  name = "${var.environment}-${var.app_name}-ha-putevents-to-secondary"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "events.amazonaws.com" }
      }
    ]
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_iam_role_policy" "event_bus_cross_region_putter_to_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  role = aws_iam_role.event_bus_cross_region_putter_to_secondary[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = "arn:aws:events:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:event-bus/${aws_cloudwatch_event_bus.replication.name}"
      }
    ]
  })
}

resource "aws_sqs_queue" "replication_to_secondary_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-replication-to-secondary-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "ha-replication"
  }
}

# EventBridge needs an SQS resource policy to actually write to the DLQ.
resource "aws_sqs_queue_policy" "replication_to_secondary_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  queue_url = aws_sqs_queue.replication_to_secondary_dlq[0].url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.replication_to_secondary_dlq[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.replicate_to_secondary[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "replication_to_secondary_dlq_depth" {
  count = var.secondary_region != "" ? 1 : 0

  alarm_name          = "${var.environment}-${var.app_name}-replication-to-secondary-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Cross-region replication DLQ (primary -> secondary) has messages; replication wedged. See docs/HA.md."
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.replication_to_secondary_dlq[0].name
  }

  tags = {
    Purpose = "ha-replication"
  }
}

# ----------------------------------------------------------------------------
# Secondary region bus + cross-region replication to primary
# ----------------------------------------------------------------------------

resource "aws_cloudwatch_event_bus" "replication_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name = "${var.environment}-${var.app_name}-replication"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_bus_policy" "replication_inbound_from_primary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  event_bus_name = aws_cloudwatch_event_bus.replication_secondary[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "AllowPrimaryRegionToPutEvents"
        Effect    = "Allow"
        Principal = { AWS = data.aws_caller_identity.current.account_id }
        Action    = "events:PutEvents"
        Resource  = aws_cloudwatch_event_bus.replication_secondary[0].arn
        Condition = {
          StringEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.replicate_to_secondary[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_event_rule" "replicate_to_primary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name           = "replicate-to-primary"
  event_bus_name = aws_cloudwatch_event_bus.replication_secondary[0].name
  description    = "Forward HA replication events to the primary region"

  # detail.source_region filter prevents loops: only forward events that
  # originated in secondary. Without it, a primary->secondary forwarded
  # event would re-match here and bounce back, hitting EventBridge's
  # THIRD_REGION_HOP guard.
  event_pattern = jsonencode({
    source = ["${var.environment}.${var.app_name}.ha-replication"]
    detail = {
      source_region = [var.secondary_region]
    }
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_cloudwatch_event_target" "replicate_to_primary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  rule           = aws_cloudwatch_event_rule.replicate_to_primary[0].name
  event_bus_name = aws_cloudwatch_event_bus.replication_secondary[0].name
  target_id      = "replicate-to-primary"

  arn      = aws_cloudwatch_event_bus.replication.arn
  role_arn = aws_iam_role.event_bus_cross_region_putter_to_primary[0].arn

  dead_letter_config {
    arn = aws_sqs_queue.replication_to_primary_dlq[0].arn
  }

  # retry_policy intentionally omitted — see replicate_to_secondary above.
}

resource "aws_iam_role" "event_bus_cross_region_putter_to_primary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name = "${var.environment}-${var.app_name}-ha-putevents-to-primary"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "sts:AssumeRole"
        Principal = { Service = "events.amazonaws.com" }
      }
    ]
  })

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_iam_role_policy" "event_bus_cross_region_putter_to_primary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  role = aws_iam_role.event_bus_cross_region_putter_to_primary[0].name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "events:PutEvents"
        Resource = aws_cloudwatch_event_bus.replication.arn
      }
    ]
  })
}

resource "aws_sqs_queue" "replication_to_primary_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                      = "${var.environment}-${var.app_name}-replication-to-primary-dlq"
  message_retention_seconds = 1209600

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn_secondary

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_sqs_queue_policy" "replication_to_primary_dlq" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  queue_url = aws_sqs_queue.replication_to_primary_dlq[0].url

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Principal = { Service = "events.amazonaws.com" }
        Action    = "sqs:SendMessage"
        Resource  = aws_sqs_queue.replication_to_primary_dlq[0].arn
        Condition = {
          ArnEquals = {
            "aws:SourceArn" = aws_cloudwatch_event_rule.replicate_to_primary[0].arn
          }
        }
      }
    ]
  })
}

resource "aws_cloudwatch_metric_alarm" "replication_to_primary_dlq_depth" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  alarm_name          = "${var.environment}-${var.app_name}-replication-to-primary-dlq-depth"
  comparison_operator = "GreaterThanThreshold"
  evaluation_periods  = 1
  metric_name         = "ApproximateNumberOfMessagesVisible"
  namespace           = "AWS/SQS"
  period              = 60
  statistic           = "Maximum"
  threshold           = 0
  alarm_description   = "Cross-region replication DLQ (secondary -> primary) has messages; replication wedged. See docs/HA.md."
  treat_missing_data  = "notBreaching"

  dimensions = {
    QueueName = aws_sqs_queue.replication_to_primary_dlq[0].name
  }

  tags = {
    Purpose = "ha-replication"
  }
}

# ----------------------------------------------------------------------------
# Outputs / SSM mirror parameters
# Lambdas in chunks 04 + 06 consume the local bus name as an env var.
# Each region's SSM has its own bus name under the same path so a
# Lambda running in either region resolves correctly.
# ----------------------------------------------------------------------------

resource "aws_ssm_parameter" "replication_event_bus_name" {
  name        = "/${var.environment}/${var.app_name}/ha/replication-event-bus-name"
  type        = "String"
  value       = aws_cloudwatch_event_bus.replication.name
  description = "Name of the local HA replication event bus"

  tags = {
    Purpose = "ha-replication"
  }
}

resource "aws_ssm_parameter" "replication_event_bus_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/ha/replication-event-bus-name"
  type        = "String"
  value       = aws_cloudwatch_event_bus.replication_secondary[0].name
  description = "Name of the local HA replication event bus (secondary region)"

  tags = {
    Purpose = "ha-replication"
  }
}

output "replication_event_bus_name" {
  description = "Name of the primary-region HA replication event bus"
  value       = aws_cloudwatch_event_bus.replication.name
}

output "replication_event_bus_arn" {
  description = "ARN of the primary-region HA replication event bus"
  value       = aws_cloudwatch_event_bus.replication.arn
}

output "replication_event_bus_arn_secondary" {
  description = "ARN of the secondary-region HA replication event bus (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_cloudwatch_event_bus.replication_secondary[0].arn : null
}

output "replication_to_secondary_dlq_arn" {
  description = "ARN of the primary->secondary replication DLQ (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_sqs_queue.replication_to_secondary_dlq[0].arn : null
}

output "replication_to_primary_dlq_arn" {
  description = "ARN of the secondary->primary replication DLQ (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_sqs_queue.replication_to_primary_dlq[0].arn : null
}
