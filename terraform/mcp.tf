# ============================================================================
# MCP Server Catalog
# Deploys MCP server containers (Fargate Spot) with ALB routing.
# Lambda calls MCP servers through the ALB (no VPC required for Lambda).
# Terraform deploys the infrastructure; org admins control access via the UI.
# ============================================================================

locals {
  any_mcp_enabled = var.mcp_sonarqube_enabled || var.mcp_snyk_enabled || var.mcp_server_enabled

  # SSM paths follow convention: /{env}/{app}/mcp/{server}-token
  mcp_sonarqube_token_ssm_path = "/${var.environment}/${var.app_name}/mcp/sonarqube-token"
  mcp_sonarqube_token_ssm_arn  = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_sonarqube_token_ssm_path}"

  # ALB listener ARN - use HTTPS when OIDC is enabled, HTTP when not
  mcp_alb_listener_arn = var.deploy_ui ? (
    local.use_oidc ? aws_lb_listener.https[0].arn : module.ui_alb.listeners["http"].arn
  ) : ""

  # MCP server URL as seen by Lambda (through ALB)
  mcp_base_url = local.use_oidc ? "https://${local.fqdn}" : (var.deploy_ui ? "http://${module.ui_alb.dns_name}" : "")
}

# -----------------------------------------------------------------------------
# Cloud Map Private DNS Namespace
# Shared by all MCP servers. Kept for internal service discovery.
# -----------------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "mcp" {
  count = local.any_mcp_enabled ? 1 : 0

  name = "${var.environment}-mcp.local"
  vpc  = var.ui_vpc_id

  tags = {
    Purpose = "mcp-service-discovery"
  }
}

# -----------------------------------------------------------------------------
# SonarQube MCP Server
# Docker image: mcp/sonarqube (official from SonarSource)
# https://github.com/SonarSource/sonarqube-mcp-server
# -----------------------------------------------------------------------------

module "mcp_sonarqube" {
  source = "./modules/mcp-server"
  count  = var.mcp_sonarqube_enabled ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region

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
    SONARQUBE_TOKEN = local.mcp_sonarqube_token_ssm_arn
  }

  ecs_cluster_arn    = module.ui_ecs.cluster_arn
  vpc_id             = var.ui_vpc_id
  private_subnet_ids = var.ui_private_subnet_ids
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp[0].name

  # ALB routing - Lambda calls MCP through ALB, bypassing OIDC
  alb_listener_arn           = local.mcp_alb_listener_arn
  alb_listener_rule_priority = 7
  mcp_path_pattern           = "/mcp"

  # Allow ALB + OutcomeOps gateway to reach the MCP container.
  # The gateway is a Fargate task in the same VPC and reaches siblings
  # via Cloud Map private DNS (no ALB hop, no TLS, no cert trust). The
  # conditional + compact() keeps the disable path
  # (mcp_server_enabled = false) clean when module.mcp_outcomeops has
  # zero instances.
  allowed_security_group_ids = compact([
    module.ui_alb_sg.security_group_id,
    var.mcp_server_enabled ? module.mcp_outcomeops[0].security_group_id : "",
  ])
}

# -----------------------------------------------------------------------------
# Auto-Register MCP Servers in DynamoDB
# Creates catalog records so servers appear in the MCP management UI.
# NOT auto-global -- org admins assign to workspaces via the UI.
# -----------------------------------------------------------------------------

resource "aws_dynamodb_table_item" "mcp_sonarqube" {
  count = var.mcp_sonarqube_enabled ? 1 : 0

  table_name = module.workspaces_table[0].dynamodb_table_id
  hash_key   = "PK"
  range_key  = "SK"

  item = jsonencode({
    PK     = { S = "MCP#catalog-sonarqube" }
    SK     = { S = "METADATA" }
    GSI1PK = { S = "ORG#default" }
    GSI1SK = { S = "MCP#catalog-sonarqube" }
    mcp_id = { S = "catalog-sonarqube" }
    name   = { S = "SonarQube" }
    url    = { S = "${local.mcp_base_url}/mcp" }
    # Cloud Map private DNS - gateway reads this and bypasses the ALB.
    # External Lambda callers (chat, workspace-management) can't resolve
    # private DNS and continue to use `url`.
    internal_url          = { S = module.mcp_sonarqube[0].service_url }
    auth_type             = { S = "none" }
    ssm_token_path        = { S = local.mcp_sonarqube_token_ssm_path }
    ssm_token_header      = { S = "SONARQUBE_TOKEN" }
    is_global             = { BOOL = false }
    is_catalog            = { BOOL = true }
    refresh_interval_days = { N = "7" }
    status                = { S = "pending" }
    tool_count            = { N = "0" }
    created_by            = { S = "terraform" }
    created_at            = { S = "2026-01-01T00:00:00Z" }
    updated_at            = { S = "2026-01-01T00:00:00Z" }
  })

  lifecycle {
    ignore_changes = [item]
  }

  depends_on = [module.mcp_sonarqube]
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "mcp_sonarqube_url" {
  description = "URL of the SonarQube MCP server (through ALB)"
  value       = var.mcp_sonarqube_enabled ? "${local.mcp_base_url}/mcp" : null
}
