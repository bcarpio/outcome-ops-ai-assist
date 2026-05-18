# ============================================================================
# KMS Key for OAuth Token Encryption
# Used to encrypt OAuth access/refresh tokens stored in DynamoDB
# ============================================================================

resource "aws_kms_key" "oauth_tokens" {
  count = var.enable_workspaces ? 1 : 0

  description             = "KMS key for OAuth token encryption - ${var.environment}"
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
        Sid    = "Allow Lambda Functions"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = {
    Purpose = "oauth-token-encryption"
  }
}

resource "aws_kms_alias" "oauth_tokens" {
  count = var.enable_workspaces ? 1 : 0

  name          = "alias/${var.environment}-${var.app_name}-oauth-tokens"
  target_key_id = aws_kms_key.oauth_tokens[0].key_id
}

resource "aws_ssm_parameter" "oauth_kms_key_arn" {
  count = var.enable_workspaces ? 1 : 0

  name  = "/${var.environment}/${var.app_name}/kms/oauth-key-arn"
  type  = "String"
  value = aws_kms_key.oauth_tokens[0].arn

  tags = {
    Purpose = "oauth-kms-key-arn"
  }
}

output "oauth_kms_key_arn" {
  description = "ARN of the KMS key for OAuth token encryption"
  value       = var.enable_workspaces ? aws_kms_key.oauth_tokens[0].arn : null
}

# ============================================================================
# Secondary-region OAuth KMS key (HA mode only).
# ============================================================================

resource "aws_kms_key" "oauth_tokens_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  description             = "KMS key for OAuth token encryption - ${var.environment} (secondary)"
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
        Sid    = "Allow Lambda Functions"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        Resource = "*"
        Condition = {
          StringEquals = {
            "kms:CallerAccount" = data.aws_caller_identity.current.account_id
          }
        }
      }
    ]
  })

  tags = {
    Purpose = "oauth-token-encryption"
  }
}

resource "aws_kms_alias" "oauth_tokens_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name          = "alias/${var.environment}-${var.app_name}-oauth-tokens"
  target_key_id = aws_kms_key.oauth_tokens_secondary[0].key_id
}

resource "aws_ssm_parameter" "oauth_kms_key_arn_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/kms/oauth-key-arn"
  type  = "String"
  value = aws_kms_key.oauth_tokens_secondary[0].arn

  tags = {
    Purpose = "oauth-kms-key-arn"
  }
}

output "oauth_kms_key_arn_secondary" {
  description = "ARN of the secondary-region KMS key for OAuth token encryption (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_kms_key.oauth_tokens_secondary[0].arn : null
}
