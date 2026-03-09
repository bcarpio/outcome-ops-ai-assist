# ============================================================================
# MCP Server Catalog
# Deploys MCP server containers (Fargate Spot) with Cloud Map service discovery.
# Terraform deploys the infrastructure; org admins control access via the UI.
# ============================================================================

locals {
  any_mcp_enabled = var.mcp_sonarqube_enabled
}

# -----------------------------------------------------------------------------
# Cloud Map Private DNS Namespace
# Shared by all MCP servers. Only created when any MCP server is enabled.
# -----------------------------------------------------------------------------

resource "aws_service_discovery_private_dns_namespace" "mcp" {
  count = local.any_mcp_enabled ? 1 : 0

  name = "mcp.local"
  vpc  = var.ui_vpc_id

  tags = {
    Purpose = "mcp-service-discovery"
  }
}

# -----------------------------------------------------------------------------
# Chat Lambda Security Group (for VPC access to MCP servers)
# Created when any MCP server is enabled. Passed to MCP modules as allowed caller.
# -----------------------------------------------------------------------------

resource "aws_security_group" "chat_lambda_mcp" {
  count = local.any_mcp_enabled ? 1 : 0

  name        = "${var.environment}-${var.app_name}-chat-lambda-mcp"
  description = "Chat Lambda - outbound to MCP servers and AWS services"
  vpc_id      = var.ui_vpc_id

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
    description = "Allow all outbound (Bedrock, DynamoDB, S3, KMS, MCP)"
  }

  tags = {
    Name    = "${var.environment}-${var.app_name}-chat-lambda-mcp"
    Purpose = "chat-lambda-mcp"
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
      STORAGE_PATH = "/tmp/sonarqube-mcp"
    },
    var.mcp_sonarqube_url != "" ? { SONARQUBE_URL = var.mcp_sonarqube_url } : {},
    var.mcp_sonarqube_org != "" ? { SONARQUBE_ORG = var.mcp_sonarqube_org } : {},
  )

  secrets = var.mcp_sonarqube_token_ssm_arn != "" ? {
    SONARQUBE_TOKEN = var.mcp_sonarqube_token_ssm_arn
  } : {}

  ecs_cluster_arn = module.ui_ecs.cluster_arn
  vpc_id          = var.ui_vpc_id
  private_subnet_ids = var.ui_private_subnet_ids
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp[0].name

  allowed_security_group_ids = [aws_security_group.chat_lambda_mcp[0].id]
}

# -----------------------------------------------------------------------------
# Auto-Register MCP Servers in DynamoDB
# Creates catalog records so servers appear in the MCP management UI.
# NOT auto-global — org admins assign to workspaces via the UI.
# -----------------------------------------------------------------------------

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
    url                   = { S = module.mcp_sonarqube[0].service_url }
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

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "mcp_sonarqube_url" {
  description = "Internal URL of the SonarQube MCP server"
  value       = var.mcp_sonarqube_enabled ? module.mcp_sonarqube[0].service_url : null
}
