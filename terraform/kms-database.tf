# ============================================================================
# KMS Key for Database Credential Encryption
#
# Used to encrypt database connection credentials (username/password/
# private key / etc.) stored in DynamoDB under DB_CREDENTIAL#{conn_id}.
#
# Separate from oauth_tokens key on purpose — different credential class,
# different IAM principals (only database-* Lambdas need access), so an
# auditor can confirm DB cred access without the OAuth key surface
# muddying the analysis. Same encryption strength, customer KMS in
# customer account (BYOC).
# ============================================================================

resource "aws_kms_key" "db_credentials" {
  count = var.enable_workspaces ? 1 : 0

  description             = "KMS key for database credential encryption - ${var.environment}"
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
    Purpose = "database-credential-encryption"
  }
}

resource "aws_kms_alias" "db_credentials" {
  count = var.enable_workspaces ? 1 : 0

  name          = "alias/${var.environment}-${var.app_name}-db-credentials"
  target_key_id = aws_kms_key.db_credentials[0].key_id
}

resource "aws_ssm_parameter" "db_credential_kms_key_arn" {
  count = var.enable_workspaces ? 1 : 0

  name  = "/${var.environment}/${var.app_name}/kms/db-credential-key-arn"
  type  = "String"
  value = aws_kms_key.db_credentials[0].arn

  tags = {
    Purpose = "db-credential-kms-key-arn"
  }
}

output "db_credential_kms_key_arn" {
  description = "ARN of the KMS key for database credential encryption"
  value       = var.enable_workspaces ? aws_kms_key.db_credentials[0].arn : null
}

# ============================================================================
# Secondary-region DB credential KMS key (HA mode only).
# ============================================================================

resource "aws_kms_key" "db_credentials_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  description             = "KMS key for database credential encryption - ${var.environment} (secondary)"
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
    Purpose = "database-credential-encryption"
  }
}

resource "aws_kms_alias" "db_credentials_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name          = "alias/${var.environment}-${var.app_name}-db-credentials"
  target_key_id = aws_kms_key.db_credentials_secondary[0].key_id
}

resource "aws_ssm_parameter" "db_credential_kms_key_arn_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/kms/db-credential-key-arn"
  type  = "String"
  value = aws_kms_key.db_credentials_secondary[0].arn

  tags = {
    Purpose = "db-credential-kms-key-arn"
  }
}

output "db_credential_kms_key_arn_secondary" {
  description = "ARN of the secondary-region KMS key for database credential encryption (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_kms_key.db_credentials_secondary[0].arn : null
}
