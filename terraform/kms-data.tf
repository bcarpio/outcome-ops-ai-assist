# ============================================================================
# KMS Key for Data-at-Rest Encryption (CMK)
# Shared key for S3, DynamoDB, SQS, SNS, and ECR when enable_cmk_encryption = true
# ============================================================================

resource "aws_kms_key" "data_at_rest" {
  count = var.enable_cmk_encryption ? 1 : 0

  description             = "CMK for data-at-rest encryption - ${var.environment}"
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
        Sid    = "Allow AWS Services"
        Effect = "Allow"
        Principal = {
          AWS = "*"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:ReEncrypt*",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = data.aws_caller_identity.current.account_id
          }
        }
      },
      {
        Sid    = "Allow S3 Vectors Indexing"
        Effect = "Allow"
        Principal = {
          Service = "indexing.s3vectors.amazonaws.com"
        }
        Action = [
          "kms:Decrypt",
          "kms:GenerateDataKey*",
          "kms:DescribeKey",
          "kms:CreateGrant"
        ]
        Resource = "*"
      }
    ]
  })

  tags = {
    Purpose = "data-at-rest-encryption"
  }
}

resource "aws_kms_alias" "data_at_rest" {
  count = var.enable_cmk_encryption ? 1 : 0

  name          = "alias/${var.environment}-${var.app_name}-data"
  target_key_id = aws_kms_key.data_at_rest[0].key_id
}

locals {
  data_cmk_arn = var.enable_cmk_encryption ? aws_kms_key.data_at_rest[0].arn : null
}

output "data_cmk_arn" {
  description = "ARN of the CMK for data-at-rest encryption"
  value       = local.data_cmk_arn
}
