# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# KMS Key for OAuth Token Encryption
# Used to encrypt OAuth access/refresh tokens stored in DynamoDB
# ============================================================================

resource "aws_kms_key" "oauth_tokens" {
  count = var.enable_workspaces ? 1 : 0

  description             = "KMS key for OAuth token encryption - ${var.environment}"
  deletion_window_in_days = 7
  enable_key_rotation     = true

  # Multi-region keys allow the secondary region's Lambdas to decrypt
  # ciphertext encrypted in this region (and vice versa) without
  # cross-region kms:Decrypt calls. Without this flag, OAuth tokens
  # written in the active region's DDB and dual-write-replicated to
  # the passive region's DDB would be undecryptable post-failover.
  # Changing this on an existing key forces destruction; see HA.md
  # for the operator migration path via scripts/ha-kms-migrate.py.
  multi_region = true

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
# Secondary-region OAuth KMS replica (HA mode only).
#
# Multi-region replica keys share key material with their primary, so
# ciphertext produced under either KeyId is decryptable under the other.
# That's what makes dual-write of OAuth-token DDB rows work: the active
# region encrypts, replication copies the ciphertext blob to the passive
# region's table, and a post-failover Lambda in the (formerly passive)
# region can decrypt without round-tripping back to the primary.
# ============================================================================

resource "aws_kms_replica_key" "oauth_tokens_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  primary_key_arn         = aws_kms_key.oauth_tokens[0].arn
  description             = "KMS replica for OAuth token encryption - ${var.environment} (secondary)"
  deletion_window_in_days = 7

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
  target_key_id = aws_kms_replica_key.oauth_tokens_secondary[0].key_id
}

resource "aws_ssm_parameter" "oauth_kms_key_arn_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/kms/oauth-key-arn"
  type  = "String"
  value = aws_kms_replica_key.oauth_tokens_secondary[0].arn

  tags = {
    Purpose = "oauth-kms-key-arn"
  }
}

output "oauth_kms_key_arn_secondary" {
  description = "ARN of the secondary-region KMS replica for OAuth token encryption (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_kms_replica_key.oauth_tokens_secondary[0].arn : null
}

# IAM resource list for OAuth-token crypto grants on PRIMARY-region Lambdas:
# both regional ARNs when HA is on. Ciphertext embeds the ID of the key that
# encrypted it, and tokens refreshed while the SECONDARY was active are
# encrypted under the replica ARN -- without the replica in the primary
# grants, failback leaves those tokens undecryptable in the primary
# (kms:Decrypt AccessDenied; the 2026-07-09 incident class). Mirror-image of
# the both-ARN grants the secondary Lambdas already carry. Single-region
# deploys resolve to just the primary ARN (no-op).
locals {
  oauth_kms_token_key_arns = var.enable_workspaces ? concat(
    [aws_kms_key.oauth_tokens[0].arn],
    var.secondary_region != "" ? [aws_kms_replica_key.oauth_tokens_secondary[0].arn] : [],
  ) : []
}
