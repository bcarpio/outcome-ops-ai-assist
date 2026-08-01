# ============================================================================
# SonarQube Cloud MCP Server (OutcomeOps-owned container)
# Replaces the third-party mcp/sonarqube image (fatal I/O-reactor bug under
# load). Minimal MCP server built from containers/mcp-sonarqube/ that calls
# SonarCloud's REST API per-request. Cataloged like any other MCP server, so
# BOTH the chat and the OutcomeOps gateway reach it via the standard proxy path.
# Reference template for future container-only MCP servers a customer adds.
# ============================================================================

# Immutable image tag written by build-sonarqube-image. The ECR repo is
# IMMUTABLE, so the task def pulls this unique tag rather than :latest.
data "aws_ssm_parameter" "mcp_sonarqube_image_tag" {
  count = var.mcp_sonarqube_enabled ? 1 : 0
  name  = "/${var.environment}/${var.app_name}/mcp-sonarqube/image-tag"
}

resource "aws_ecr_repository" "mcp_sonarqube" {
  count = var.mcp_sonarqube_enabled ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-mcp-sonarqube"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "mcp-sonarqube"
  }
}

locals {
  mcp_sonarqube_token_ssm_arn = "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_sonarqube_token_ssm_path}"
}

module "mcp_sonarqube" {
  source = "./modules/mcp-server"
  count  = var.mcp_sonarqube_enabled ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region

  server_name     = "sonarqube"
  container_image = "${aws_ecr_repository.mcp_sonarqube[0].repository_url}:${nonsensitive(data.aws_ssm_parameter.mcp_sonarqube_image_tag[0].value)}"
  container_port  = 8080
  cpu             = var.mcp_sonarqube_cpu
  memory          = var.mcp_sonarqube_memory

  # Single, low-traffic, connection-sensitive service -- on-demand FARGATE (not
  # Spot). The container is stateless + robust, but on-demand avoids restart
  # blips for a near-free task.
  # Follows the fleet-wide spot posture (was hardcoded on-demand from
  # the container-only template). Stateless SonarCloud API wrapper --
  # a spot reclamation costs one brief reconnect, acceptable wherever
  # fargate_enable_spot is true.
  ondemand_base   = var.fargate_enable_spot ? 0 : 1
  ondemand_weight = var.fargate_enable_spot ? 0 : 1
  spot_weight     = var.fargate_enable_spot ? 100 : 0

  # SonarCloud org/url as plain env; the token is injected as a secret from SSM
  # (SONARQUBE_TOKEN) and read by the container -- callers never see it.
  environment_variables = merge(
    { SONARQUBE_URL = var.mcp_sonarqube_url != "" ? var.mcp_sonarqube_url : "https://sonarcloud.io" },
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

  use_alb                    = var.deploy_ui
  alb_listener_arn           = local.mcp_alb_listener_arn
  alb_listener_rule_priority = 7
  mcp_path_pattern           = "/mcp/sonarqube"

  allowed_security_group_ids = merge(
    { ui_alb = module.ui_alb_sg.security_group_id },
    var.mcp_server_enabled ? { mcp_outcomeops = module.mcp_outcomeops[0].security_group_id } : {},
  )
}

# Catalog record so BOTH the gateway and the chat proxy to it. auth_type=none +
# no token header: the container reads its own SonarCloud token from the
# injected secret, so callers just forward MCP JSON-RPC.
resource "aws_dynamodb_table_item" "mcp_sonarqube" {
  count = var.mcp_sonarqube_enabled ? 1 : 0

  table_name = module.workspaces_table[0].dynamodb_table_id
  hash_key   = "PK"
  range_key  = "SK"

  item = jsonencode({
    PK                    = { S = "MCP#catalog-sonarqube" }
    SK                    = { S = "METADATA" }
    GSI1PK                = { S = "ORG#default" }
    GSI1SK                = { S = "MCP#catalog-sonarqube" }
    mcp_id                = { S = "catalog-sonarqube" }
    name                  = { S = "SonarQube" }
    url                   = { S = "${local.mcp_base_url}/mcp/sonarqube" }
    internal_url          = { S = module.mcp_sonarqube[0].service_url }
    auth_type             = { S = "none" }
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

output "mcp_sonarqube_url" {
  description = "URL of the SonarQube MCP server (through ALB)"
  value       = var.mcp_sonarqube_enabled ? "${local.mcp_base_url}/mcp/sonarqube" : null
}

output "mcp_sonarqube_ecr_url" {
  description = "ECR repository URL for the SonarQube MCP container"
  value       = var.mcp_sonarqube_enabled ? aws_ecr_repository.mcp_sonarqube[0].repository_url : null
}
