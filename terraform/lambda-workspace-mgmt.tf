# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Workspace Management Lambda
# Handles workspace CRUD, membership, and repo management
# Uses Lambda Web Adapter (LWA) for HTTP endpoints
# ============================================================================

locals {
  # ADR-048 v1.1: the group-binding picker's app-only Microsoft Graph reads
  # default to the deploy's EXISTING OIDC login App Registration -- add the
  # `Group.Read.All` (Application) permission + admin consent to that same app
  # and nothing new needs registering. Consequences of reusing it:
  #   * client id / tenant come from the oidc_* tfvars we already carry, and
  #   * the secret is the login app's own secret, read from the derived
  #     house-pattern path (local.oidc_ssm_path) -- NOT a path in tfvars.
  # A security-conscious customer who wants a dedicated read-only Graph app
  # overrides azure_ad_graph_client_id (+ optionally azure_ad_graph_secret_ssm_path
  # for a separate secret); those overrides win when set.
  azure_ad_graph_client_id = var.azure_ad_graph_client_id != "" ? var.azure_ad_graph_client_id : var.oidc_client_id
  azure_ad_graph_tenant_id = var.azure_ad_tenant_id != "" ? var.azure_ad_tenant_id : var.oidc_tenant_id
  azure_ad_graph_secret_ssm_path = (
    var.azure_ad_graph_secret_ssm_path != "" ? var.azure_ad_graph_secret_ssm_path :
    (var.oidc_enabled ? local.oidc_ssm_path : "")
  )
}

module "workspace_management_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-workspace-management"
  description   = "Workspace management for OutcomeOps AI Assist"
  handler       = "run.sh"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 512

  # Lambda Web Adapter layer for HTTP endpoints. The platform-CA layer is
  # also attached unconditionally so that under BYO-internal-CA deploys the
  # outbound requests calls in lambda/workspace-management/handler.py to
  # the MCP servers actually trust the listener. Empty
  # platform_ca_bundle_ssm_param_arns ships pure certifi so this is a no-op
  # for default-public-cert deploys.
  layers = [
    "arn:aws:lambda:${var.aws_region}:753240598075:layer:LambdaAdapterLayerX86:25",
    aws_lambda_layer_version.platform_ca.arn,
  ]

  # Source code with pip requirements (includes shared module so handler.py
  # can `from shared.audit_writer import ...` at runtime; chunk 02 / 03 / 05
  # all depend on this).
  source_path = [
    {
      path             = "${path.module}/../lambda/workspace-management"
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

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables for LWA
  environment_variables = merge({
    ENV                          = var.environment
    APP_NAME                     = var.app_name
    GITHUB_BASE_URL              = var.github_base_url
    AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"
    AWS_LWA_READINESS_CHECK_PATH = "/health"
    PORT                         = "8000"
    KB_BUCKET                    = module.knowledge_base_bucket.s3_bucket_id
    DELETE_QUEUE_URL             = aws_sqs_queue.delete_queue[0].url
    # Secondary-region delete queue (HA) so repo-delete cleanup reaches
    # both regions (delete-bug 02). Empty in single-region mode.
    DELETE_QUEUE_URL_SECONDARY = var.secondary_region != "" ? aws_sqs_queue.delete_queue_secondary[0].url : ""
    OAUTH_KMS_KEY_ARN          = aws_kms_key.oauth_tokens[0].arn
    AUDIT_TABLE                = module.audit_logs_table.dynamodb_table_id
    AUDIT_RETENTION_DAYS       = tostring(var.audit_retention_days)
    ANALYTICS_TABLE            = module.analytics_table.dynamodb_table_id
    # PII redaction posture (read for GET /workspaces/{id}/privacy so the
    # UI knows whether to render the toggle). plan/ chunk 07.
    PII_REDACTION_ENABLED = tostring(var.pii_redaction_enabled)

    # Per-integration enablement manifest surfaced at
    # GET /api/config/integrations for the UI to decide which
    # integration sections to render. Each integration's TF file
    # gates its actual resources on the same var (chunks 02-11).
    INTEGRATION_AZURE_DEVOPS_ENABLED        = tostring(var.enable_azure_devops_integration)
    INTEGRATION_AZURE_DEVOPS_BOARDS_ENABLED = tostring(var.enable_azure_devops_boards_integration)
    INTEGRATION_GITHUB_ENABLED              = tostring(var.enable_github_integration)
    INTEGRATION_GITHUB_ISSUES_ENABLED       = tostring(var.enable_github_issue_integration)
    INTEGRATION_GITLAB_ENABLED              = tostring(var.enable_gitlab_integration)
    INTEGRATION_CONFLUENCE_ENABLED          = tostring(var.enable_confluence_integration)
    INTEGRATION_JIRA_ENABLED                = tostring(var.enable_jira_integration)
    INTEGRATION_OUTLOOK_ENABLED             = tostring(var.enable_outlook_integration)
    INTEGRATION_TEAMS_ENABLED               = tostring(var.enable_teams_integration)
    INTEGRATION_SHAREPOINT_ENABLED          = tostring(var.enable_sharepoint_integration)
    INTEGRATION_ONEDRIVE_ENABLED            = tostring(var.enable_onedrive_integration)
    INTEGRATION_ONENOTE_ENABLED             = tostring(var.enable_onenote_integration)
    INTEGRATION_DATABASE_ENABLED            = tostring(var.enable_database_integration)
    INTEGRATION_SERVICENOW_ENABLED          = tostring(var.enable_servicenow_integration)
    INTEGRATION_SALESFORCE_ENABLED          = tostring(var.enable_salesforce_integration)

    # chunk 03 (ADR-048 v1.1): Azure AD group selection UX. Patterns scope the
    # binding picker; the Graph fallback (app-only, SSM secret) populates the
    # full picker + resolves the >200-group memberOf overage. Off => the picker
    # is fed by the caller's own token groups + paste (credential-free).
    # Graph creds default to the OIDC login app (see locals above); tenant falls
    # back to oidc_tenant_id. This is workspace-mgmt-only -- the MCP server's
    # AZURE_AD_TENANT_ID stays governed by its own var so this does not switch on
    # chunk-02 MCP JWT validation.
    AZURE_AD_TENANT_ID                    = local.azure_ad_graph_tenant_id
    AZURE_AD_GROUP_PATTERNS               = jsonencode(var.azure_ad_group_patterns)
    AZURE_AD_GROUP_SYNC_ENABLED           = tostring(var.azure_ad_group_sync_enabled)
    AZURE_AD_GROUP_NAME_CLAIM_ENABLED     = tostring(var.azure_ad_group_name_claim_enabled)
    AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED = tostring(var.azure_ad_group_graph_fallback_enabled)
    AZURE_AD_GRAPH_CLIENT_ID              = local.azure_ad_graph_client_id
    AZURE_AD_GRAPH_SECRET_SSM_PATH        = local.azure_ad_graph_secret_ssm_path

    # Point requests / urllib3 / httpx / stdlib ssl + urllib.request at
    # the merged PEM shipped in the platform-CA layer so BYO-internal-CA
    # MCP listeners are trusted. Both env vars are needed because
    # `requests` honors REQUESTS_CA_BUNDLE and stdlib honors SSL_CERT_FILE.
    REQUESTS_CA_BUNDLE = "/opt/ca-bundle.pem"
    SSL_CERT_FILE      = "/opt/ca-bundle.pem"
    },
    # HA dual-write: REPLICATION_EVENT_BUS_NAME + THIS_REGION so
    # shared.dual_write emits replication events for the handler's
    # mutations. Empty in single-region mode (var.secondary_region == "").
    local.ha_writer_env,
    local.ha_scheduler_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # DynamoDB for workspaces table
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query",
        "dynamodb:Scan",
        "dynamodb:BatchWriteItem"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    # DynamoDB for audit logs (admin-action writes via shared.audit_writer
    # plus the new chunk-05 audit-query endpoints which read base table +
    # GSI1 and may scan as a last-resort org-admin path).
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:Scan",
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn,
        "${module.audit_logs_table.dynamodb_table_arn}/index/*",
      ]
    }

    # DynamoDB for analytics aggregates (dashboard reads)
    dynamodb_analytics = {
      effect = "Allow"
      actions = [
        "dynamodb:Query",
        "dynamodb:GetItem"
      ]
      resources = [
        module.analytics_table.dynamodb_table_arn
      ]
    }

    # SSM for configuration (table names, GitHub PAT)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # KMS for decrypting SSM parameters (GitHub PAT)
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # S3 for document upload/list/delete (presigned URLs and listing)
    s3_documents = {
      effect = "Allow"
      actions = [
        "s3:PutObject",
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:ListBucket"
      ]
      resources = [
        module.knowledge_base_bucket.s3_bucket_arn,
        "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
      ]
    }

    # KMS for MCP credential encryption
    oauth_kms = {
      effect = "Allow"
      actions = [
        "kms:Encrypt",
        "kms:Decrypt",
        "kms:GenerateDataKey"
      ]
      resources = local.oauth_kms_token_key_arns
    }

    # SQS for sending delete messages (primary + secondary queue under HA).
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage"
      ]
      resources = concat(
        [aws_sqs_queue.delete_queue[0].arn],
        var.secondary_region != "" ? [aws_sqs_queue.delete_queue_secondary[0].arn] : [],
      )
    }
    },
    # HA dual-write: events:PutEvents on the local replication bus.
    # Empty (no-op) in single-region mode.
    local.ha_writer_events_iam,
    local.ha_scheduler_iam,
    # ADR-048 v1.1: read the app-only Graph client secret for the binding
    # picker + >200-group memberOf fallback. Granted only when the fallback is
    # enabled; the effective path defaults to the OIDC login app's own secret
    # (local.azure_ad_graph_secret_ssm_path) but honors a dedicated override.
    # aws/ssm KMS decrypt is already granted above.
    (var.azure_ad_group_graph_fallback_enabled && local.azure_ad_graph_secret_ssm_path != "") ? {
      graph_secret_read = {
        effect  = "Allow"
        actions = ["ssm:GetParameter"]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.azure_ad_graph_secret_ssm_path}"
        ]
      }
    } : {},
  )

  tags = {
    Purpose = "workspace-management"
  }
}

# Lambda Function URL with IAM auth - only Fargate task role can invoke
resource "aws_lambda_function_url" "workspace_management" {
  count = var.enable_workspaces ? 1 : 0

  function_name      = module.workspace_management_lambda[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST", "GET", "PATCH", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

# Store workspace management URL in SSM for Fargate proxy
resource "aws_ssm_parameter" "workspace_management_url" {
  count = var.enable_workspaces ? 1 : 0

  name        = "/${var.environment}/${var.app_name}/api/workspace-management-url"
  description = "Lambda Function URL for workspace management (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.workspace_management[0].function_url

  tags = {
    Purpose = "workspace-management-url"
  }
}

output "workspace_management_lambda_name" {
  description = "Name of the workspace management Lambda function"
  value       = var.enable_workspaces ? module.workspace_management_lambda[0].lambda_function_name : null
}

output "workspace_management_lambda_arn" {
  description = "ARN of the workspace management Lambda function"
  value       = var.enable_workspaces ? module.workspace_management_lambda[0].lambda_function_arn : null
}

output "workspace_management_url" {
  description = "Lambda Function URL for workspace management"
  value       = var.enable_workspaces ? aws_lambda_function_url.workspace_management[0].function_url : null
}
