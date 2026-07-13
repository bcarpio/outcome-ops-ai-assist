# ============================================================================
# MCP Server Catalog
# Deploys MCP server containers (Fargate Spot) with ALB routing.
# Lambda calls MCP servers through the ALB (no VPC required for Lambda).
# Terraform deploys the infrastructure; org admins control access via the UI.
# ============================================================================

locals {
  # SonarQube is now served by NATIVE tools in the OutcomeOps MCP server
  # (lambda/mcp-server/sonarcloud.py), not a Fargate service -- so it no longer
  # needs the Cloud Map namespace / ALB routing that the Fargate MCP servers do.
  any_mcp_enabled = var.mcp_snyk_enabled || var.mcp_server_enabled

  # SSM token path consumed by the native SonarQube tools (SONARQUBE_TOKEN_SSM_PATH).
  mcp_sonarqube_token_ssm_path = "/${var.environment}/${var.app_name}/mcp/sonarqube-token"

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
