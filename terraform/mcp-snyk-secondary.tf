# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Snyk MCP server — SECONDARY region mirror (HA parity)
# ----------------------------------------------------------------------------
# Mirror of module.mcp_snyk (mcp-snyk.tf). Any catalog MCP server must fail
# over with the rest of the platform when the customer runs HA, so this is
# gated on the SAME switches as the OutcomeOps MCP secondary:
#   local.deploy_ui_secondary (HA on) && var.mcp_snyk_enabled.
#
# The container image replicates to the secondary region automatically via
# aws_ecr_replication_configuration.ha (PREFIX_MATCH on ${env}-${app}-), so
# there is no secondary ECR repo to declare and no secondary push in the build
# — only the secondary-region image-tag SSM parameter, which build-snyk-image
# writes when secondary_region is set.
#
# No secondary catalog DDB item is needed: the primary aws_dynamodb_table_item
# .mcp_snyk replicates via dual-write, and its internal_url
# (snyk.${env}-mcp.local) is region-portable — VPC-scoped Cloud Map resolves it
# to the local region's task in each region.
# ============================================================================

# Secondary-region copy of the image tag (build-snyk-image writes it there when
# secondary_region is set). Consumed only by the secondary task below.
data "aws_ssm_parameter" "mcp_snyk_image_tag_secondary" {
  count    = local.deploy_ui_secondary && var.mcp_snyk_enabled ? 1 : 0
  provider = aws.secondary
  name     = "/${var.environment}/${var.app_name}/mcp-snyk/image-tag"
}

module "mcp_snyk_secondary" {
  source = "./modules/mcp-server"
  count  = local.deploy_ui_secondary && var.mcp_snyk_enabled ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.secondary_region

  server_name = "snyk"
  # Same repo name as primary; the us-east-2 ECR copy is populated by the
  # cross-region replication config (aws_ecr_replication_configuration.ha).
  container_image = "${data.aws_caller_identity.current.account_id}.dkr.ecr.${var.secondary_region}.amazonaws.com/${var.environment}-${var.app_name}-mcp-snyk:${nonsensitive(data.aws_ssm_parameter.mcp_snyk_image_tag_secondary[0].value)}"
  container_port  = 8080
  cpu             = var.mcp_snyk_cpu
  memory          = var.mcp_snyk_memory

  # Match primary: 100% spot unless the master switch disables it.
  ondemand_base   = var.fargate_enable_spot ? 0 : 1
  ondemand_weight = var.fargate_enable_spot ? 0 : 1
  spot_weight     = var.fargate_enable_spot ? 100 : 0

  environment_variables = merge(
    {},
    var.mcp_snyk_org != "" ? { SNYK_CFG_ORG = var.mcp_snyk_org } : {},
  )

  # Regional SSM SecureString — same path as primary, resolved in the secondary
  # region. Synced west->east by scripts/ha-sync-secrets (SSM is regional).
  secrets = {
    SNYK_TOKEN = "arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter${local.mcp_snyk_token_ssm_path}"
  }

  ecs_cluster_arn    = try(module.ui_ecs_secondary[0].cluster_arn, "")
  vpc_id             = var.ui_vpc_id_secondary
  private_subnet_ids = var.ui_private_subnet_ids_secondary
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp_secondary[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp_secondary[0].name

  use_alb                    = local.deploy_ui_secondary
  alb_listener_arn           = local.mcp_alb_listener_arn_secondary
  alb_listener_rule_priority = 6
  mcp_path_pattern           = "/mcp/snyk"

  role_name_suffix = "-secondary"

  allowed_security_group_ids = merge(
    { ui_alb = module.ui_alb_sg_secondary.security_group_id },
    var.mcp_server_enabled ? { mcp_outcomeops = module.mcp_outcomeops_secondary[0].security_group_id } : {},
  )
}

output "mcp_snyk_url_secondary" {
  description = "URL of the Snyk MCP server in secondary region (null when HA / Snyk disabled)"
  value       = (local.deploy_ui_secondary && var.mcp_snyk_enabled) ? "${local.mcp_base_url_secondary}/mcp/snyk" : null
}
