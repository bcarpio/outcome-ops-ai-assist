# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# KMS Keys for Encryption
# ============================================================================

# KMS key for CloudWatch Logs encryption
# All Lambda log groups use this shared key
resource "aws_kms_key" "cloudwatch_logs" {
  description             = "KMS key for CloudWatch Logs encryption - ${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow CloudWatch Logs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.aws_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = {
    Purpose = "cloudwatch-logs-encryption"
  }
}

resource "aws_kms_alias" "cloudwatch_logs" {
  name          = "alias/${var.environment}-${var.app_name}-cloudwatch-logs"
  target_key_id = aws_kms_key.cloudwatch_logs.key_id
}

output "cloudwatch_logs_kms_key_arn" {
  description = "ARN of the KMS key for CloudWatch Logs encryption"
  value       = aws_kms_key.cloudwatch_logs.arn
}

# ============================================================================
# Secondary-region CloudWatch Logs KMS key (HA mode only).
# Lambda log groups in the secondary region are encrypted with this key.
# ============================================================================

resource "aws_kms_key" "cloudwatch_logs_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  description             = "KMS key for CloudWatch Logs encryption - ${var.environment} (secondary)"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "Enable IAM User Permissions"
        Effect = "Allow"
        Principal = {
          AWS = "arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"
        }
        Action   = "kms:*"
        Resource = "*"
      },
      {
        Sid    = "Allow CloudWatch Logs"
        Effect = "Allow"
        Principal = {
          Service = "logs.${var.secondary_region}.amazonaws.com"
        }
        Action = [
          "kms:Encrypt*",
          "kms:Decrypt*",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:Describe*"
        ]
        Resource = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:aws:logs:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:log-group:*"
          }
        }
      }
    ]
  })

  tags = {
    Purpose = "cloudwatch-logs-encryption"
  }
}

resource "aws_kms_alias" "cloudwatch_logs_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name          = "alias/${var.environment}-${var.app_name}-cloudwatch-logs"
  target_key_id = aws_kms_key.cloudwatch_logs_secondary[0].key_id
}

output "cloudwatch_logs_kms_key_arn_secondary" {
  description = "ARN of the secondary-region KMS key for CloudWatch Logs encryption (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_kms_key.cloudwatch_logs_secondary[0].arn : null
}
