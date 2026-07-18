# ============================================================================
# GitLab Integration Lambda (ADR-050)
#
# Handles GitLab OAuth 2.0 (Model A delegated OAuth) + connection / repo
# management for GitLab projects. The sync worker + hourly scheduler are
# added in a subsequent chunk.
#
# Notes worth keeping in mind when editing this file:
#
# 1. The integration is OFF by default. ``var.enable_gitlab_integration``
#    gates the ENTIRE terraform surface -- integration Lambda + Function URL
#    + SSM publish + IAM. Customers without GitLab leave the flag false and
#    provision none of it.
#
# 2. SaaS-first. ``var.gitlab_base_url`` defaults to https://gitlab.com. A
#    later chunk makes self-managed GitLab a first-class base URL (ADR-050
#    Decision 1).
#
# 3. Model A delegated OAuth. ONE admin-registered OAuth app (client id in
#    tfvars, secret in operator-managed SSM). Each user authorizes once; the
#    token is user-scoped, so access is automatically limited to what the
#    authorizing user can see.
#
# 4. The OAuth app SECRET follows the house pattern (cf. the OIDC login secret):
#    terraform CREATES a SecureString placeholder at the DERIVED path
#    /<env>/<app>/gitlab/client-secret with lifecycle ignore_changes, and the
#    operator overwrites the value AFTER apply via `aws ssm put-parameter
#    --overwrite`. No SSM path in tfvars. A customer who wants to manage the
#    param themselves sets gitlab_client_secret_ssm_param (override) and terraform
#    then neither creates nor owns it. The redirect URI is likewise derived from
#    the deploy FQDN unless gitlab_redirect_uri overrides it.
# ============================================================================

locals {
  gitlab_secret_ssm_path = (
    var.gitlab_client_secret_ssm_param != "" ? var.gitlab_client_secret_ssm_param :
    "/${var.environment}/${var.app_name}/gitlab/client-secret"
  )
  gitlab_redirect_uri = (
    var.gitlab_redirect_uri != "" ? var.gitlab_redirect_uri :
    "https://${local.fqdn}/api/gitlab/callback"
  )
  # terraform owns the secret only when the operator did NOT override the path.
  gitlab_manage_secret = var.enable_workspaces && var.enable_gitlab_integration && var.gitlab_client_secret_ssm_param == ""
}

# Placeholder secret -- overwrite the value post-apply; ignore_changes keeps your
# real value across later applies.
resource "aws_ssm_parameter" "gitlab_client_secret" {
  count = local.gitlab_manage_secret ? 1 : 0

  name        = local.gitlab_secret_ssm_path
  description = "GitLab OAuth app secret (placeholder -- overwrite after first apply)"
  type        = "SecureString"
  value       = "CHANGE_ME"

  lifecycle {
    ignore_changes = [value]
  }

  tags = {
    Purpose = "gitlab-client-secret"
  }
}

module "gitlab_integration_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-gitlab-integration"
  description   = "GitLab OAuth and connection management for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 512

  source_path = [
    {
      path             = "${path.module}/../lambda/gitlab-integration"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!requirements-dev\\.txt",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn
  quiet_archive_local_exec          = true

  # ADR-019: attach OutcomeOps license validator layer so the snapshot
  # helper (``_snapshot_active_repos``) has the layer's symbol available.
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV              = var.environment
      APP_NAME         = var.app_name
      WORKSPACES_TABLE = module.workspaces_table[0].dynamodb_table_id

      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn

      GITLAB_BASE_URL                = var.gitlab_base_url
      GITLAB_CLIENT_ID               = var.gitlab_client_id
      GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path
      GITLAB_REDIRECT_URI            = local.gitlab_redirect_uri

      AUDIT_TABLE = module.audit_logs_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
    local.ha_scheduler_env,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
          "dynamodb:Query"
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      }

      # SSM for the client secret. Scope to the exact derived (or overridden)
      # parameter name -- the integration is the only consumer.
      ssm_secret = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.gitlab_secret_ssm_path}",
        ]
      }

      # KMS for token encryption/decryption at rest in DDB.
      kms_oauth = {
        effect = "Allow"
        actions = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        resources = [
          aws_kms_key.oauth_tokens[0].arn
        ]
      }

      # KMS for the SSM SecureString that holds the client secret.
      kms_ssm = {
        effect = "Allow"
        actions = [
          "kms:Decrypt"
        ]
        resources = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      }

      dynamodb_audit = {
        effect = "Allow"
        actions = [
          "dynamodb:PutItem"
        ]
        resources = [
          module.audit_logs_table.dynamodb_table_arn
        ]
      }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "gitlab-integration"
  }
}

resource "aws_lambda_function_url" "gitlab_integration" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  function_name      = module.gitlab_integration_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["GET", "POST", "PUT", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "gitlab_integration_url" {
  count = var.enable_workspaces && var.enable_gitlab_integration ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/gitlab-integration-url"
  description = "Lambda Function URL for GitLab integration (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.gitlab_integration[0].function_url

  tags = {
    Purpose = "gitlab-integration-url"
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "gitlab_integration_lambda_name" {
  description = "Name of the GitLab integration Lambda function"
  value       = try(module.gitlab_integration_lambda[0].lambda_function_name, null)
}

output "gitlab_integration_lambda_arn" {
  description = "ARN of the GitLab integration Lambda function"
  value       = try(module.gitlab_integration_lambda[0].lambda_function_arn, null)
}

output "gitlab_integration_url" {
  description = "Lambda Function URL for GitLab integration"
  value       = try(aws_lambda_function_url.gitlab_integration[0].function_url, null)
}
