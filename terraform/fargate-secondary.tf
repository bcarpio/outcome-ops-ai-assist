# ============================================================================
# Secondary-region MCP servers + security scanner (HA chunk 9i)
# ----------------------------------------------------------------------------
# Mirror of:
#   - aws_service_discovery_private_dns_namespace.mcp (in mcp.tf)
#   - module.mcp_outcomeops      (in mcp-outcomeops.tf)
#   - module.mcp_sonarqube       (in mcp.tf)
#   - module.security_scanner_lambda + SSM + target group + listener rules
#     (in security-scanner.tf)
#
# All attach to the SECONDARY ALB (chunk 9b) via path-based listener rules.
# Same priorities as primary; conflicts are impossible across two different
# ALBs.
#
# The mcp-outcomeops + mcp-sonarqube ECR images replicate from primary
# automatically via aws_ecr_replication_configuration.ha (chunk 9a). The
# secondary Fargate tasks pull from us-east-2 ECR for warm starts —
# repository names match primary; the replication service auto-creates
# the destination repos on first push.
#
# Security scanner secrets (random_id, random_password) use the SAME
# values in BOTH regions so the auth URL + JWT secret remain consistent
# across failover. The secondary SSM SecureStrings reference the primary's
# random resources directly.
#
# All resources gated on local.deploy_ui_secondary (chunk 9b) AND the
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
  # cross-region replication config from chunk 9a.
  container_image = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.secondary_region}.amazonaws.com/${var.environment}-${var.app_name}-mcp-outcomeops:latest"
  container_port  = 8080
  cpu             = 512
  memory          = 1024

  environment_variables = {
    PORT               = "8080"
    AWS_DEFAULT_REGION = var.secondary_region
    DYNAMODB_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
    S3_VECTORS_BUCKET  = aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name
    S3_VECTORS_INDEX   = aws_s3vectors_index.knowledge_base_secondary[0].index_name
    KB_BUCKET          = module.knowledge_base_bucket_secondary.s3_bucket_id
    ENVIRONMENT        = var.environment
    MCP_BASE_URL       = local.mcp_base_url_secondary
    AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
    SYMBOL_GRAPH_TABLE = module.symbol_graph_table_secondary[0].dynamodb_table_id
    CODE_MAPS_TABLE    = module.code_maps_table_secondary[0].dynamodb_table_id
  }

  ecs_cluster_arn    = module.ui_ecs_secondary.cluster_arn
  vpc_id             = var.ui_vpc_id_secondary
  private_subnet_ids = var.ui_private_subnet_ids_secondary
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp_secondary[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp_secondary[0].name

  use_alb                    = local.deploy_ui_secondary
  alb_listener_arn           = local.mcp_alb_listener_arn_secondary
  alb_listener_rule_priority = 3
  mcp_path_pattern           = "/mcp/v1"

  allowed_security_group_ids = [module.ui_alb_sg_secondary.security_group_id]
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
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
        ]
      },
      {
        Sid    = "SSMReadMcpTokens"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
        ]
        Resource = [
          "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/mcp/*",
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
    ]
  })
}

# ----------------------------------------------------------------------------
# SonarQube MCP server (secondary)
# ----------------------------------------------------------------------------

module "mcp_sonarqube_secondary" {
  source = "./modules/mcp-server"
  count  = local.deploy_ui_secondary && var.mcp_sonarqube_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.secondary_region

  server_name     = "sonarqube"
  container_image = "mcp/sonarqube:latest"
  container_port  = 8080
  cpu             = 512
  memory          = 1024

  environment_variables = merge(
    {
      SONARQUBE_TRANSPORT = "http"
      SONARQUBE_HTTP_HOST = "0.0.0.0"
      SONARQUBE_HTTP_PORT = "8080"
    },
    var.mcp_sonarqube_url != "" ? { SONARQUBE_URL = var.mcp_sonarqube_url } : {},
    var.mcp_sonarqube_org != "" ? { SONARQUBE_ORG = var.mcp_sonarqube_org } : {},
  )

  secrets = {
    SONARQUBE_TOKEN = "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_sonarqube_token_ssm_path}"
  }

  ecs_cluster_arn    = module.ui_ecs_secondary.cluster_arn
  vpc_id             = var.ui_vpc_id_secondary
  private_subnet_ids = var.ui_private_subnet_ids_secondary
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp_secondary[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp_secondary[0].name

  use_alb                    = local.deploy_ui_secondary
  alb_listener_arn           = local.mcp_alb_listener_arn_secondary
  alb_listener_rule_priority = 7
  mcp_path_pattern           = "/mcp"

  allowed_security_group_ids = compact([
    module.ui_alb_sg_secondary.security_group_id,
    var.mcp_server_enabled ? module.mcp_outcomeops_secondary[0].security_group_id : "",
  ])
}

# Operator must pre-create the SonarQube token in secondary's SSM before
# the Fargate task can pull secrets. Document in HA.md (chunk 9l).
# Read by the secondary MCP-sonarqube task via the SECRETS array above.

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
  value       = (local.deploy_ui_secondary && var.mcp_sonarqube_enabled) ? "${local.mcp_base_url_secondary}/mcp" : null
}
