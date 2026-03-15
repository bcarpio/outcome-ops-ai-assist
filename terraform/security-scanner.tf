# ============================================================================
# Security Scanner Auth Endpoint
# Provides password-based authentication for AWS Security Agent (headless).
# Bypasses OIDC via a dedicated ALB listener rule at a secret path.
# The Lambda serves a login form (GET) and validates credentials (POST),
# issuing a JWT cookie that the Express proxy accepts as an auth alternative.
# ============================================================================

locals {
  scanner_enabled    = var.security_scanner_enabled && var.deploy_ui
  scanner_ssm_prefix = "/${var.environment}/${var.app_name}/security-scanner"
}

# -----------------------------------------------------------------------------
# Random Secrets
# -----------------------------------------------------------------------------

resource "random_id" "scanner_path" {
  count       = local.scanner_enabled ? 1 : 0
  byte_length = 16
}

resource "random_password" "scanner_password" {
  count   = local.scanner_enabled ? 1 : 0
  length  = 32
  special = true
}

resource "random_password" "scanner_jwt_secret" {
  count   = local.scanner_enabled ? 1 : 0
  length  = 64
  special = false
}

resource "random_password" "scanner_header_key" {
  count   = local.scanner_enabled ? 1 : 0
  length  = 32
  special = false
}

# -----------------------------------------------------------------------------
# SSM Parameters
# -----------------------------------------------------------------------------

resource "aws_ssm_parameter" "scanner_path" {
  count = local.scanner_enabled ? 1 : 0
  name  = "${local.scanner_ssm_prefix}/path"
  type  = "SecureString"
  value = random_id.scanner_path[0].hex

  tags = { Purpose = "security-scanner" }
}

resource "aws_ssm_parameter" "scanner_password" {
  count = local.scanner_enabled ? 1 : 0
  name  = "${local.scanner_ssm_prefix}/password"
  type  = "SecureString"
  value = random_password.scanner_password[0].result

  tags = { Purpose = "security-scanner" }
}

resource "aws_ssm_parameter" "scanner_jwt_secret" {
  count = local.scanner_enabled ? 1 : 0
  name  = "${local.scanner_ssm_prefix}/jwt-secret"
  type  = "SecureString"
  value = random_password.scanner_jwt_secret[0].result

  tags = { Purpose = "security-scanner" }
}

# -----------------------------------------------------------------------------
# DynamoDB Service Account
# Created with empty password_hash. Lambda hashes on first login using
# SSM-stored plaintext and writes the bcrypt hash back to DynamoDB.
# lifecycle ignore_changes prevents Terraform from overwriting the hash.
# -----------------------------------------------------------------------------

resource "aws_dynamodb_table_item" "scanner_user" {
  count      = local.scanner_enabled ? 1 : 0
  table_name = module.workspaces_table[0].dynamodb_table_id
  hash_key   = "PK"
  range_key  = "SK"

  item = jsonencode({
    PK                 = { S = "USER#${var.security_scanner_email}" }
    SK                 = { S = "PROFILE" }
    user_email         = { S = var.security_scanner_email }
    name               = { S = "Security Scanner" }
    auth_type          = { S = "password" }
    password_hash      = { S = "" }
    is_service_account = { BOOL = true }
    created_at         = { S = "2026-01-01T00:00:00Z" }
  })

  lifecycle {
    ignore_changes = [item]
  }
}

# -----------------------------------------------------------------------------
# Lambda Function - Login Form Handler
# Receives ALB events (not Function URL). Serves GET login form and
# POST credential validation. Issues JWT cookie on success.
# -----------------------------------------------------------------------------

module "security_scanner_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = local.scanner_enabled ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-security-scanner"
  description   = "Security scanner auth endpoint for AWS Security Agent"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 15
  memory_size   = 128

  source_path = [
    {
      path             = "${path.module}/../lambda/security-scanner"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
      ]
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true

  environment_variables = {
    ENV              = var.environment
    APP_NAME         = var.app_name
    WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id
    SCANNER_PATH     = random_id.scanner_path[0].hex
    SCANNER_EMAIL    = var.security_scanner_email
    SSM_PASSWORD     = aws_ssm_parameter.scanner_password[0].name
    SSM_JWT_SECRET   = aws_ssm_parameter.scanner_jwt_secret[0].name
    REDIRECT_URL     = local.use_oidc ? "https://${local.fqdn}/workspaces" : "http://${module.ui_alb.dns_name}/workspaces"
  }

  attach_policy_statements = true
  policy_statements = {
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn
      ]
    }

    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        aws_ssm_parameter.scanner_password[0].arn,
        aws_ssm_parameter.scanner_jwt_secret[0].arn,
        aws_ssm_parameter.scanner_path[0].arn
      ]
    }

    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }
  }

  tags = {
    Purpose = "security-scanner"
  }
}

# -----------------------------------------------------------------------------
# ALB Target Group + Listener Rule (bypasses OIDC)
# Priority 2 - evaluated before OIDC catch-all and MCP rules.
# Path: /auth/<secret-hex>
# -----------------------------------------------------------------------------

resource "aws_lb_target_group" "scanner" {
  count       = local.scanner_enabled ? 1 : 0
  name        = "${var.environment}-scanner"
  target_type = "lambda"

  tags = { Purpose = "security-scanner" }
}

resource "aws_lambda_permission" "scanner_alb" {
  count         = local.scanner_enabled ? 1 : 0
  statement_id  = "AllowALBInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.security_scanner_lambda[0].lambda_function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.scanner[0].arn
}

resource "aws_lb_target_group_attachment" "scanner" {
  count            = local.scanner_enabled ? 1 : 0
  target_group_arn = aws_lb_target_group.scanner[0].arn
  target_id        = module.security_scanner_lambda[0].lambda_function_arn
  depends_on       = [aws_lambda_permission.scanner_alb]
}

resource "aws_lb_listener_rule" "scanner" {
  count        = local.scanner_enabled ? 1 : 0
  listener_arn = local.mcp_alb_listener_arn
  priority     = 2

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.scanner[0].arn
  }

  condition {
    path_pattern {
      values = ["/auth/${random_id.scanner_path[0].hex}", "/auth/${random_id.scanner_path[0].hex}/*"]
    }
  }
}

# -----------------------------------------------------------------------------
# Scanner Header Bypass - skip OIDC for requests with the scanner header
# The AWS Security Agent sends this custom header on every request
# (validation, login, and scanning). This bypasses OIDC so the agent
# can reach the app without going through Microsoft login.
# Priority 4 - after scanner auth (2), MCP (3/5), before OAuth callbacks (10).
# -----------------------------------------------------------------------------

resource "aws_lb_listener_rule" "scanner_header_bypass" {
  count        = local.scanner_enabled ? 1 : 0
  listener_arn = local.mcp_alb_listener_arn
  priority     = 4

  action {
    type             = "forward"
    target_group_arn = module.ui_alb.target_groups["ui"].arn
  }

  condition {
    http_header {
      http_header_name = "x-scanner-key"
      values           = [random_password.scanner_header_key[0].result]
    }
  }
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "security_scanner_auth_url" {
  description = "Auth URL for AWS Security Agent configuration"
  value       = local.scanner_enabled ? "${local.mcp_base_url}/auth/${random_id.scanner_path[0].hex}" : null
  sensitive   = true
}

output "security_scanner_email" {
  description = "Service account email for AWS Security Agent"
  value       = local.scanner_enabled ? var.security_scanner_email : null
}

output "security_scanner_password" {
  description = "Service account password for AWS Security Agent"
  value       = local.scanner_enabled ? random_password.scanner_password[0].result : null
  sensitive   = true
}

output "security_scanner_header_key" {
  description = "Custom header value (X-Scanner-Key) for ALB OIDC bypass"
  value       = local.scanner_enabled ? random_password.scanner_header_key[0].result : null
  sensitive   = true
}
