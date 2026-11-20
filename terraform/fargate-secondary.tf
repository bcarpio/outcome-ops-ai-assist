# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Secondary-region MCP servers + security scanner ()
# ----------------------------------------------------------------------------
# Mirror of:
#   - aws_service_discovery_private_dns_namespace.mcp (in mcp.tf)
#   - module.mcp_outcomeops      (in mcp-outcomeops.tf)
#   - module.security_scanner_lambda + SSM + target group + listener rules
#     (in security-scanner.tf)
#
# All attach to the SECONDARY ALB via path-based listener rules.
# Same priorities as primary; conflicts are impossible across two different
# ALBs.
#
# The mcp-outcomeops + mcp-sonarqube ECR images replicate from primary
# automatically via aws_ecr_replication_configuration.ha. The
# secondary Fargate tasks pull from us-east-2 ECR for warm starts —
# repository names match primary; the replication service auto-creates
# the destination repos on first push.
#
# Security scanner secrets (random_id, random_password) use the SAME
# values in BOTH regions so the auth URL + JWT secret remain consistent
# across failover. The secondary SSM SecureStrings reference the primary's
# random resources directly.
#
# All resources gated on local.deploy_ui_secondary AND the
# relevant feature flag (mcp_server_enabled / mcp_sonarqube_enabled /
# scanner_enabled).
# ============================================================================

locals {
  # ALB listener ARN in secondary — HTTPS when OIDC enabled, HTTP otherwise.
  mcp_alb_listener_arn_secondary = local.deploy_ui_secondary ? (
    local.use_oidc ? aws_lb_listener.https_secondary[0].arn : module.ui_alb_secondary.listeners["http"].arn
  ) : ""

  # MCP server URL as seen by Lambda (through secondary ALB) — local-region.
  mcp_base_url_secondary = local.use_oidc ? "https://${var.ui_subdomain}2.${var.domain}" : (
    local.deploy_ui_secondary ? "http://${module.ui_alb_secondary.dns_name}" : ""
  )

  scanner_enabled_secondary = local.scanner_enabled && local.deploy_ui_secondary
}

# ----------------------------------------------------------------------------
# Cloud Map private DNS namespace (secondary)
# Tied to the secondary VPC. MCP servers in secondary discover each other
# via this namespace; chat in secondary reaches MCP-outcomeops by name.
# ----------------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "mcp_secondary" {
  count = local.deploy_ui_secondary && local.any_mcp_enabled ? 1 : 0

  provider = aws.secondary

  name = "${var.environment}-mcp.local"
  vpc  = var.ui_vpc_id_secondary

  tags = {
    Purpose = "mcp-service-discovery"
  }
}

# ----------------------------------------------------------------------------
# OutcomeOps MCP server (secondary)
# ----------------------------------------------------------------------------

# Secondary-region copy of the MCP image tag (the build target writes it to SSM
# in the secondary region too). Consumed only by the secondary MCP task below.
data "aws_ssm_parameter" "mcp_image_tag_secondary" {
  count    = local.deploy_ui_secondary && var.mcp_server_enabled ? 1 : 0
  provider = aws.secondary
  name     = "/${var.environment}/${var.app_name}/mcp-outcomeops/image-tag"
}

module "mcp_outcomeops_secondary" {
  source = "./modules/mcp-server"
  count  = local.deploy_ui_secondary && var.mcp_server_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.secondary_region

  server_name = "outcomeops"
  # Same repo name as primary; us-east-2 ECR copy populated via the
  # cross-region replication config from .
  container_image = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.secondary_region}.amazonaws.com/${var.environment}-${var.app_name}-mcp-outcomeops:${nonsensitive(data.aws_ssm_parameter.mcp_image_tag_secondary[0].value)}"
  container_port  = 8080

  # (HA parity): tfvars-driven sizing + autoscaling,
  # same shape as primary. Requires sessions in DDB.
  cpu                                  = var.mcp_server_cpu
  memory                               = var.mcp_server_memory
  desired_count                        = var.mcp_server_desired_count
  ondemand_base                        = var.mcp_server_ondemand_base
  ondemand_weight                      = var.mcp_server_ondemand_weight
  spot_weight                          = var.fargate_enable_spot ? var.mcp_server_spot_weight : 0
  min_healthy_percent                  = var.mcp_server_min_healthy_percent
  enable_autoscaling                   = var.mcp_server_enable_autoscaling
  autoscaling_min_capacity             = var.mcp_server_min_count
  autoscaling_max_capacity             = var.mcp_server_max_count
  autoscaling_target_requests_per_task = var.mcp_server_target_requests_per_task
  alb_arn_suffix                       = local.deploy_ui_secondary ? module.ui_alb_secondary.arn_suffix : ""

  environment_variables = merge({
    PORT               = "8080"
    GITHUB_BASE_URL    = var.github_base_url
    AWS_DEFAULT_REGION = var.secondary_region
    DYNAMODB_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
    S3_VECTORS_BUCKET  = aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name
    S3_VECTORS_INDEX   = aws_s3vectors_index.knowledge_base_secondary[0].index_name
    KB_BUCKET          = module.knowledge_base_bucket_secondary.s3_bucket_id
    ENVIRONMENT        = var.environment
    MCP_BASE_URL       = local.mcp_base_url_secondary
    # (ADR-049, HA parity): Azure AD OAuth (JWT) auth. Shared code +
    # same tfvars -> identical behavior in both regions.
    AZURE_AD_TENANT_ID      = var.azure_ad_tenant_id
    MCP_JWT_AUDIENCE        = var.mcp_jwt_audience
    MCP_STATIC_KEYS_ENABLED = tostring(var.mcp_static_keys_enabled)
    AUDIT_TABLE             = module.audit_logs_table_secondary[0].dynamodb_table_id
    AUDIT_RETENTION_DAYS    = tostring(var.audit_retention_days)
    # (HA parity): DDB-backed session store, secondary
    # region's own table. Sessions are region-scoped by design (
    # comment in terraform/dynamodb-mcp-sessions.tf).
    MCP_SESSION_STORE  = "ddb"
    MCP_SESSIONS_TABLE = module.mcp_sessions_table_secondary[0].dynamodb_table_id
    SYMBOL_GRAPH_TABLE = module.symbol_graph_table_secondary[0].dynamodb_table_id
    CODE_MAPS_TABLE    = module.code_maps_table_secondary[0].dynamodb_table_id
    # (HA parity, ADR-033): mirror primary rerank env. Without
    # these three the secondary silently drops the per-lane rerank
    # (retrieval quality drops exactly when everything else is on fire).
    BEDROCK_RERANK_MODEL_ID = var.bedrock_rerank_model_id
    RERANK_ENABLED          = tostring(var.bedrock_rerank_enabled)
    RERANK_MAX_FETCH        = tostring(var.rerank_max_fetch)
    # search_hybrid (ADR-059, HA parity): router classify_intent needs the
    # BASIC chat model. Claude app profiles are disabled in prd (region-deny
    # SCP), so fall back to the us.* system-profile model id -- mirror of
    # primary (mcp-outcomeops.tf). Without it the symbol-graph lane fails
    # augment_error on failover.
    BEDROCK_BASIC_MODEL_ID = var.bedrock_basic_model_id
    # Audit pricing for router classify and rerank (mirror of primary).
    BEDROCK_BASIC_INPUT_PRICE   = tostring(var.bedrock_basic_input_price)
    BEDROCK_BASIC_OUTPUT_PRICE  = tostring(var.bedrock_basic_output_price)
    BEDROCK_RERANK_PRICE_PER_1K = tostring(var.bedrock_rerank_price_per_1k)
    # ADR-030 v1.1 (HA parity): ADO branch of read_source needs the
    # same env plumbing on the secondary. Region-neutral values flow
    # from the same tfvars as the primary.
    AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
    AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
    AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param
    OAUTH_KMS_KEY_ARN                    = var.enable_workspaces ? try(aws_kms_replica_key.oauth_tokens_secondary[0].arn, "") : ""
    },
    # HA dual-write: emit to the SECONDARY region's replication bus when
    # this region is active post-failover.
    local.ha_writer_env_secondary,
    # ADR-016 v1.2 -- MCP tool retrieval (chat workload), secondary parity.
    local.bedrock_chat_env_secondary,
  )

  ecs_cluster_arn    = try(module.ui_ecs_secondary[0].cluster_arn, "")
  vpc_id             = var.ui_vpc_id_secondary
  private_subnet_ids = var.ui_private_subnet_ids_secondary
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp_secondary[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp_secondary[0].name

  use_alb                    = local.deploy_ui_secondary
  alb_listener_arn           = local.mcp_alb_listener_arn_secondary
  alb_listener_rule_priority = 3
  mcp_path_pattern           = "/mcp/v1"

  role_name_suffix = "-secondary"

  allowed_security_group_ids = {
    ui_alb = module.ui_alb_sg_secondary.security_group_id
  }
}

# ADR-030 v1.1 (HA parity): KMS decrypt on the OAuth-tokens replica CMK
# so the ADO branch of read_source can unwrap the refresh_token from DDB
# after failover. Separate policy so it only exists when the replica key
# exists (var.enable_workspaces + secondary_region set).
resource "aws_iam_role_policy" "mcp_outcomeops_secondary_ado_kms" {
  count = local.deploy_ui_secondary && var.mcp_server_enabled && var.enable_workspaces ? 1 : 0

  provider = aws.secondary

  name = "${var.environment}-${var.app_name}-mcp-outcomeops-ado-kms"
  role = module.mcp_outcomeops_secondary[0].task_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Decrypt stored refresh tokens + Encrypt the cached access token /
        # rotated refresh token via get_valid_access_token (ADR-050 v1.1).
        # Mirrors the primary mcp-outcomeops KMSOAuthTokens grant.
        Sid      = "KMSOAuthTokensSecondary"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:Encrypt"]
        Resource = [try(aws_kms_replica_key.oauth_tokens_secondary[0].arn, "")]
      }
    ]
  })
}

resource "aws_iam_role_policy" "mcp_outcomeops_secondary" {
  count = local.deploy_ui_secondary && var.mcp_server_enabled ? 1 : 0

  provider = aws.secondary

  name = "${var.environment}-${var.app_name}-mcp-outcomeops"
  role = module.mcp_outcomeops_secondary[0].task_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "DynamoDBAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
          "dynamodb:UpdateItem",
          "dynamodb:Scan",
        ]
        Resource = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      },
      {
        # (HA parity): session store, secondary region.
        Sid    = "MCPSessionsAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = [
          module.mcp_sessions_table_secondary[0].dynamodb_table_arn,
        ]
      },
      {
        Sid    = "S3VectorsQuery"
        Effect = "Allow"
        Action = [
          "s3vectors:QueryVectors",
          "s3vectors:GetVectors",
        ]
        Resource = [
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*",
        ]
      },
      {
        Sid    = "S3KBRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket",
        ]
        Resource = [
          module.knowledge_base_bucket_secondary.s3_bucket_arn,
          "${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*",
        ]
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
        ]
        # foundation-model grant stays -- ADR-016 section 5: application
        # inference profiles require BOTH the profile ARN and the
        # underlying model ARN. inference-profile/* covers the profiles
        # in bedrock-inference-profiles-secondary.tf.
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      },
      {
        Sid    = "SSMReadMcpTokens"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters",
        ]
        Resource = concat(
          [
            "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/mcp/*",
            # read_source mints GitHub App installation tokens at request
            # time (mirrors the primary grant per CLAUDE.md HA-parity).
            "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/app-id",
            "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/private-key",
          ],
          # ADR-030 v1.1 (HA parity): ADO branch client-secret SSM read.
          var.azure_devops_client_secret_ssm_param != "" ? [
            "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}"
          ] : [],
        )
      },
      # ADR-030 v1.1 (HA parity): KMS decrypt for the SecureString.
      {
        Sid    = "KMSDecryptSSMDefaultAliasSecondary"
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = [
          "arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      },
      {
        Sid    = "SymbolGraphRead"
        Effect = "Allow"
        Action = [
          "dynamodb:Query",
          "dynamodb:GetItem",
          "dynamodb:BatchGetItem",
        ]
        Resource = [
          module.symbol_graph_table_secondary[0].dynamodb_table_arn,
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-kind-index",
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-prefix-index",
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-name-index",
        ]
      },
      {
        Sid    = "CodeMapsPointerRead"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:Query",
        ]
        Resource = [
          module.code_maps_table_secondary[0].dynamodb_table_arn,
          "${module.code_maps_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      },
      {
        Sid    = "DynamoDBAuditWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem",
        ]
        Resource = [
          module.audit_logs_table_secondary[0].dynamodb_table_arn,
        ]
      },
      {
        # HA dual-write: emit api-key last_used_at replication events to
        # the secondary region's bus when active post-failover.
        Sid    = "HAEventsPutEvents"
        Effect = "Allow"
        Action = [
          "events:PutEvents",
        ]
        Resource = [
          aws_cloudwatch_event_bus.replication_secondary[0].arn,
        ]
      },
      {
        # Parity with primary: symbol_graph_reader QueryLatency metric emit.
        Sid      = "CloudWatchSymbolGraphMetrics"
        Effect   = "Allow"
        Action   = ["cloudwatch:PutMetricData"]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "OutcomeOps/SymbolGraph"
          }
        }
      },
    ]
  })
}

# ----------------------------------------------------------------------------
# Security scanner (secondary)
# Reuses primary's random_id / random_password values so the auth URL +
# JWT secret remain consistent across regions. The SSM SecureStrings are
# created PER REGION (KMS keys are regional), but they store the SAME
# plaintext values.
# ----------------------------------------------------------------------------

resource "aws_ssm_parameter" "scanner_path_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  name  = "${local.scanner_ssm_prefix}/path"
  type  = "SecureString"
  value = random_id.scanner_path[0].hex

  tags = { Purpose = "security-scanner" }
}

resource "aws_ssm_parameter" "scanner_password_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  name  = "${local.scanner_ssm_prefix}/password"
  type  = "SecureString"
  value = random_password.scanner_password[0].result

  tags = { Purpose = "security-scanner" }
}

resource "aws_ssm_parameter" "scanner_jwt_secret_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  name  = "${local.scanner_ssm_prefix}/jwt-secret"
  type  = "SecureString"
  value = random_password.scanner_jwt_secret[0].result

  tags = { Purpose = "security-scanner" }
}

module "security_scanner_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = local.scanner_enabled_secondary ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-security-scanner"
  role_name     = "${var.environment}-${var.app_name}-security-scanner-secondary"
  description   = "Security scanner auth endpoint (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 15
  memory_size   = 128

  source_path = [
    { path = "${path.module}/../lambda/security-scanner", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = {
    ENV              = var.environment
    APP_NAME         = var.app_name
    WORKSPACES_TABLE = module.workspaces_table_secondary[0].dynamodb_table_id
    SCANNER_PATH     = random_id.scanner_path[0].hex
    SCANNER_EMAIL    = var.security_scanner_email
    SSM_PASSWORD     = aws_ssm_parameter.scanner_password_secondary[0].name
    SSM_JWT_SECRET   = aws_ssm_parameter.scanner_jwt_secret_secondary[0].name
    REDIRECT_URL     = local.use_oidc ? "https://${local.fqdn}/workspaces" : "http://${module.ui_alb_secondary.dns_name}/workspaces"
  }

  attach_policy_statements = true
  policy_statements = {
    dynamodb = {
      effect    = "Allow"
      actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
      resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
    }
    ssm_read = {
      effect  = "Allow"
      actions = ["ssm:GetParameter"]
      resources = [
        aws_ssm_parameter.scanner_password_secondary[0].arn,
        aws_ssm_parameter.scanner_jwt_secret_secondary[0].arn,
        aws_ssm_parameter.scanner_path_secondary[0].arn,
      ]
    }
    kms_decrypt = {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
    }
  }

  tags = { Purpose = "security-scanner" }
}

resource "aws_lb_target_group" "scanner_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  name        = "${var.environment}-scanner"
  target_type = "lambda"

  tags = { Purpose = "security-scanner" }
}

resource "aws_lambda_permission" "scanner_alb_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  statement_id  = "AllowALBInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.security_scanner_lambda_secondary[0].lambda_function_name
  principal     = "elasticloadbalancing.amazonaws.com"
  source_arn    = aws_lb_target_group.scanner_secondary[0].arn
}

resource "aws_lb_target_group_attachment" "scanner_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  target_group_arn = aws_lb_target_group.scanner_secondary[0].arn
  target_id        = module.security_scanner_lambda_secondary[0].lambda_function_arn
  depends_on       = [aws_lambda_permission.scanner_alb_secondary]
}

resource "aws_lb_listener_rule" "scanner_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  listener_arn = local.mcp_alb_listener_arn_secondary
  priority     = 2

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.scanner_secondary[0].arn
  }

  condition {
    path_pattern {
      values = ["/auth/${random_id.scanner_path[0].hex}", "/auth/${random_id.scanner_path[0].hex}/*"]
    }
  }
}

resource "aws_lb_listener_rule" "scanner_header_bypass_secondary" {
  count = local.scanner_enabled_secondary ? 1 : 0

  provider = aws.secondary

  listener_arn = local.mcp_alb_listener_arn_secondary
  priority     = 4

  action {
    type             = "forward"
    target_group_arn = module.ui_alb_secondary.target_groups["ui"].arn
  }

  condition {
    http_header {
      http_header_name = "x-scanner-key"
      values           = [random_password.scanner_header_key[0].result]
    }
  }
}

# ----------------------------------------------------------------------------
# Outputs
# ----------------------------------------------------------------------------

output "mcp_server_url_secondary" {
  description = "URL of the OutcomeOps MCP server in secondary region (null when HA / MCP disabled)"
  value       = (local.deploy_ui_secondary && var.mcp_server_enabled) ? "${local.mcp_base_url_secondary}/mcp/v1" : null
}

output "mcp_sonarqube_url_secondary" {
  description = "URL of the SonarQube MCP server in secondary region (null when HA / Sonar disabled)"
  value       = (local.deploy_ui_secondary && var.mcp_sonarqube_enabled) ? "${local.mcp_base_url_secondary}/mcp/sonarqube" : null
}
