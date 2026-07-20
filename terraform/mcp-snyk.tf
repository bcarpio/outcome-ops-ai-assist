# ============================================================================
# Snyk MCP Server
# Wraps the Snyk CLI MCP server (stdio) via supergateway for HTTP transport.
# Custom container built from containers/mcp-snyk/Dockerfile.
# ============================================================================

# -----------------------------------------------------------------------------
# ECR Repository for the Snyk MCP container image
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "mcp_snyk" {
  count = var.mcp_snyk_enabled ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-mcp-snyk"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "mcp-snyk"
  }
}

# -----------------------------------------------------------------------------
# Snyk MCP Server Module
# -----------------------------------------------------------------------------

locals {
  mcp_snyk_token_ssm_path = "/${var.environment}/${var.app_name}/mcp/snyk-token"
  mcp_snyk_token_ssm_arn  = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_snyk_token_ssm_path}"
}

module "mcp_snyk" {
  source = "./modules/mcp-server"
  count  = var.mcp_snyk_enabled ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region

  server_name     = "snyk"
  container_image = "${aws_ecr_repository.mcp_snyk[0].repository_url}:latest"
  container_port  = 8080
  cpu             = 512
  memory          = 1024

  # Module default is 100% spot (base 0 / on-demand weight 0 / spot 100).
  # When the master switch disables spot, flip to a 1-task on-demand base
  # -- otherwise the strategy would have zero usable capacity.
  ondemand_base   = var.fargate_enable_spot ? 0 : 1
  ondemand_weight = var.fargate_enable_spot ? 0 : 1
  spot_weight     = var.fargate_enable_spot ? 100 : 0

  environment_variables = merge(
    {},
    var.mcp_snyk_org != "" ? { SNYK_CFG_ORG = var.mcp_snyk_org } : {},
  )

  secrets = {
    SNYK_TOKEN = local.mcp_snyk_token_ssm_arn
  }

  ecs_cluster_arn    = module.ui_ecs.cluster_arn
  vpc_id             = var.ui_vpc_id
  private_subnet_ids = var.ui_private_subnet_ids
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp[0].name

  # ALB routing - Lambda calls MCP through ALB, bypassing OIDC
  use_alb                    = var.deploy_ui
  alb_listener_arn           = local.mcp_alb_listener_arn
  alb_listener_rule_priority = 6
  mcp_path_pattern           = "/mcp/snyk"

  # Allow ALB + OutcomeOps gateway to reach the MCP container.
  # The gateway is a Fargate task in the same VPC and reaches siblings
  # via Cloud Map private DNS (no ALB hop, no TLS, no cert trust). The
  # conditional + compact() keeps the disable path
  # (mcp_server_enabled = false) clean when module.mcp_outcomeops has
  # zero instances.
  allowed_security_group_ids = merge(
    { ui_alb = module.ui_alb_sg.security_group_id },
    var.mcp_server_enabled ? { mcp_outcomeops = module.mcp_outcomeops[0].security_group_id } : {},
  )
}

# -----------------------------------------------------------------------------
# Auto-Register in DynamoDB
# -----------------------------------------------------------------------------

resource "aws_dynamodb_table_item" "mcp_snyk" {
  count = var.mcp_snyk_enabled ? 1 : 0

  table_name = module.workspaces_table[0].dynamodb_table_id
  hash_key   = "PK"
  range_key  = "SK"

  item = jsonencode({
    PK     = { S = "MCP#catalog-snyk" }
    SK     = { S = "METADATA" }
    GSI1PK = { S = "ORG#default" }
    GSI1SK = { S = "MCP#catalog-snyk" }
    mcp_id = { S = "catalog-snyk" }
    name   = { S = "Snyk" }
    url    = { S = "${local.mcp_base_url}/mcp/snyk" }
    # Cloud Map private DNS - gateway reads this and bypasses the ALB.
    # External Lambda callers (chat, workspace-management) can't resolve
    # private DNS and continue to use `url`.
    internal_url          = { S = module.mcp_snyk[0].service_url }
    auth_type             = { S = "none" }
    ssm_token_path        = { S = local.mcp_snyk_token_ssm_path }
    ssm_token_header      = { S = "SNYK_TOKEN" }
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

  depends_on = [module.mcp_snyk]
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "mcp_snyk_url" {
  description = "URL of the Snyk MCP server (through ALB)"
  value       = var.mcp_snyk_enabled ? "${local.mcp_base_url}/mcp/snyk" : null
}

output "mcp_snyk_ecr_url" {
  description = "ECR repository URL for the Snyk MCP container"
  value       = var.mcp_snyk_enabled ? aws_ecr_repository.mcp_snyk[0].repository_url : null
}
