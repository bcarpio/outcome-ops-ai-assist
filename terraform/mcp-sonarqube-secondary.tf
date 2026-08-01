# ============================================================================
# SonarQube MCP server — SECONDARY region mirror (HA parity)
# ----------------------------------------------------------------------------
# Mirror of module.mcp_sonarqube (mcp-sonarqube.tf). Same rationale + gating as
# mcp-snyk-secondary.tf: any catalog MCP server fails over with the platform
# when the customer runs HA. Gated on local.deploy_ui_secondary (HA on) &&
# var.mcp_sonarqube_enabled.
#
# Image replicates via aws_ecr_replication_configuration.ha; only the
# secondary image-tag SSM parameter is written by build-sonarqube-image.
# The catalog internal_url (sonarqube.${env}-mcp.local) is region-portable, so
# the replicated primary catalog item covers both regions.
# ============================================================================

data "aws_ssm_parameter" "mcp_sonarqube_image_tag_secondary" {
  count    = local.deploy_ui_secondary && var.mcp_sonarqube_enabled ? 1 : 0
  provider = aws.secondary
  name     = "/${var.environment}/${var.app_name}/mcp-sonarqube/image-tag"
}

module "mcp_sonarqube_secondary" {
  source = "./modules/mcp-server"
  count  = local.deploy_ui_secondary && var.mcp_sonarqube_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.secondary_region

  server_name = "sonarqube"
  # Same repo name as primary; us-east-2 ECR copy via cross-region replication.
  container_image = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.secondary_region}.amazonaws.com/${var.environment}-${var.app_name}-mcp-sonarqube:${nonsensitive(data.aws_ssm_parameter.mcp_sonarqube_image_tag_secondary[0].value)}"
  container_port  = 8080
  cpu             = var.mcp_sonarqube_cpu
  memory          = var.mcp_sonarqube_memory

  # Match primary: follows the fleet spot posture.
  ondemand_base   = var.fargate_enable_spot ? 0 : 1
  ondemand_weight = var.fargate_enable_spot ? 0 : 1
  spot_weight     = var.fargate_enable_spot ? 100 : 0

  environment_variables = merge(
    { SONARQUBE_URL = var.mcp_sonarqube_url != "" ? var.mcp_sonarqube_url : "https://sonarcloud.io" },
    var.mcp_sonarqube_org != "" ? { SONARQUBE_ORG = var.mcp_sonarqube_org } : {},
  )

  # Regional SSM SecureString — same path as primary, secondary region.
  # Synced west->east by scripts/ha-sync-secrets.
  secrets = {
    SONARQUBE_TOKEN = "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_sonarqube_token_ssm_path}"
  }

  ecs_cluster_arn    = try(module.ui_ecs_secondary[0].cluster_arn, "")
  vpc_id             = var.ui_vpc_id_secondary
  private_subnet_ids = var.ui_private_subnet_ids_secondary
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp_secondary[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp_secondary[0].name

  use_alb                    = local.deploy_ui_secondary
  alb_listener_arn           = local.mcp_alb_listener_arn_secondary
  alb_listener_rule_priority = 7
  mcp_path_pattern           = "/mcp/sonarqube"

  role_name_suffix = "-secondary"

  allowed_security_group_ids = merge(
    { ui_alb = module.ui_alb_sg_secondary.security_group_id },
    var.mcp_server_enabled ? { mcp_outcomeops = module.mcp_outcomeops_secondary[0].security_group_id } : {},
  )
}
