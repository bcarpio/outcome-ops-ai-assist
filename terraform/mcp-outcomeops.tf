# ============================================================================
# OutcomeOps MCP Server
# Exposes the knowledge base to external MCP clients (Claude Code, VS Code, etc.)
# Uses the shared mcp-server module with API key authentication.
# ============================================================================

# -----------------------------------------------------------------------------
# ECR Repository for the custom container image
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "mcp_outcomeops" {
  count = var.mcp_server_enabled ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-mcp-outcomeops"
  image_tag_mutability = "MUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = {
    Purpose = "mcp-outcomeops"
  }
}

# -----------------------------------------------------------------------------
# MCP Server Module Instance
# -----------------------------------------------------------------------------

module "mcp_outcomeops" {
  source = "./modules/mcp-server"
  count  = var.mcp_server_enabled ? 1 : 0

  environment = var.environment
  app_name    = var.app_name
  aws_region  = var.aws_region

  server_name     = "outcomeops"
  container_image = "${aws_ecr_repository.mcp_outcomeops[0].repository_url}:latest"
  container_port  = 8080
  cpu             = 512
  memory          = 1024

  environment_variables = {
    PORT               = "8080"
    AWS_DEFAULT_REGION = var.aws_region
    DYNAMODB_TABLE     = module.workspaces_table[0].dynamodb_table_id
    S3_VECTORS_BUCKET  = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name
    S3_VECTORS_INDEX   = aws_s3vectors_index.knowledge_base.index_name
    KB_BUCKET          = module.knowledge_base_bucket.s3_bucket_id
    ENVIRONMENT        = var.environment
  }

  ecs_cluster_arn    = module.ui_ecs.cluster_arn
  vpc_id             = var.ui_vpc_id
  private_subnet_ids = var.ui_private_subnet_ids
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp[0].name

  # ALB routing -- bypasses OIDC, API key auth handled in application
  alb_listener_arn           = local.mcp_alb_listener_arn
  alb_listener_rule_priority = 3
  mcp_path_pattern           = "/mcp/v1"

  allowed_security_group_ids = [module.ui_alb_sg.security_group_id]
}

# -----------------------------------------------------------------------------
# IAM Policy for OutcomeOps MCP Server Task Role
# Grants access to DynamoDB, S3 Vectors, Bedrock, and S3 KB bucket.
# -----------------------------------------------------------------------------

resource "aws_iam_role_policy" "mcp_outcomeops" {
  count = var.mcp_server_enabled ? 1 : 0

  name = "${var.environment}-${var.app_name}-mcp-outcomeops"
  role = module.mcp_outcomeops[0].task_role_name

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
          "dynamodb:Scan"
        ]
        Resource = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      },
      {
        Sid    = "S3VectorsQuery"
        Effect = "Allow"
        Action = [
          "s3vectors:QueryVectors",
          "s3vectors:GetVectors"
        ]
        Resource = [
          "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
          "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
        ]
      },
      {
        Sid    = "S3KBRead"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:ListBucket"
        ]
        Resource = [
          module.knowledge_base_bucket.s3_bucket_arn,
          "${module.knowledge_base_bucket.s3_bucket_arn}/*"
        ]
      },
      {
        Sid    = "BedrockInvoke"
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel"
        ]
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*"
        ]
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "mcp_server_ecr_url" {
  description = "ECR repository URL for the OutcomeOps MCP server image"
  value       = var.mcp_server_enabled ? aws_ecr_repository.mcp_outcomeops[0].repository_url : null
}

output "mcp_server_url" {
  description = "URL of the OutcomeOps MCP server (for MCP client configuration)"
  value       = var.mcp_server_enabled ? "${local.mcp_base_url}/mcp/v1" : null
}
