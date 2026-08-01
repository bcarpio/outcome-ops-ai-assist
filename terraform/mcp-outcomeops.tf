# ============================================================================
# OutcomeOps MCP Server
# Exposes the knowledge base to external MCP clients (Claude Code, VS Code, etc.)
# Uses the shared mcp-server module with API key authentication.
# ============================================================================

# -----------------------------------------------------------------------------
# ECR Repository for the custom container image
# -----------------------------------------------------------------------------

# Immutable image tag written by the build-mcp-server-image make target. The ECR
# repo is IMMUTABLE, so the task def pulls this unique tag rather than :latest.
data "aws_ssm_parameter" "mcp_image_tag" {
  count = var.mcp_server_enabled ? 1 : 0
  name  = "/${var.environment}/${var.app_name}/mcp-outcomeops/image-tag"
}

resource "aws_ecr_repository" "mcp_outcomeops" {
  count = var.mcp_server_enabled ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-mcp-outcomeops"
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
  container_image = "${aws_ecr_repository.mcp_outcomeops[0].repository_url}:${nonsensitive(data.aws_ssm_parameter.mcp_image_tag[0].value)}"
  container_port  = 8080

  # fable-scale chunk 01: sizing + autoscaling driven by tfvars. Autoscaling
  # here presumes DDB-backed sessions (fable-scale chunk 02); with in-memory
  # sessions a second task can't share auth/session state — flip
  # mcp_server_enable_autoscaling = false as a rollback until chunk 02 ships.
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
  alb_arn_suffix                       = var.deploy_ui ? module.ui_alb.arn_suffix : ""

  environment_variables = merge({
    PORT               = "8080"
    AWS_DEFAULT_REGION = var.aws_region
    GITHUB_BASE_URL    = var.github_base_url
    DYNAMODB_TABLE     = module.workspaces_table[0].dynamodb_table_id
    S3_VECTORS_BUCKET  = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name
    S3_VECTORS_INDEX   = aws_s3vectors_index.knowledge_base.index_name
    KB_BUCKET          = module.knowledge_base_bucket.s3_bucket_id
    ENVIRONMENT        = var.environment
    MCP_BASE_URL       = local.mcp_base_url
    # chunk 11b (ADR-049): Azure AD OAuth (JWT) auth for the MCP server.
    # AZURE_AD_TENANT_ID + MCP_JWT_AUDIENCE enable the JWT path;
    # MCP_STATIC_KEYS_ENABLED gates the legacy static-key path (default true;
    # set false in the enterprise profile once all clients use OAuth).
    AZURE_AD_TENANT_ID      = var.azure_ad_tenant_id
    MCP_JWT_AUDIENCE        = var.mcp_jwt_audience
    MCP_STATIC_KEYS_ENABLED = tostring(var.mcp_static_keys_enabled)
    # fable-scale chunk 02: DDB-backed session store. MCP_SESSION_STORE
    # picks the impl (memory | ddb); "ddb" is required for horizontal
    # scaling (chunk 01 autoscaling). Fallback to "memory" reverts to the
    # single-task in-memory dict (test-mode + operator rollback).
    MCP_SESSION_STORE  = "ddb"
    MCP_SESSIONS_TABLE = module.mcp_sessions_table[0].dynamodb_table_id
    # plan/audit-completeness chunk 02 + 08: MCP server emits
    # ACTION_API_KEY_USED + ACTION_MCP_TOOL_INVOKED audit rows.
    AUDIT_TABLE = module.audit_logs_table.dynamodb_table_id
    # Symbol-graph query tools (find_callers, find_importers,
    # find_implementers, find_definitions; see docs/mcp-server.md).
    # The reader uses SYMBOL_GRAPH_TABLE for the Fact stream +
    # CODE_MAPS_TABLE for the per-(workspace, repo) pointer that drives
    # staleness reporting.
    SYMBOL_GRAPH_TABLE = module.symbol_graph_table.dynamodb_table_id
    CODE_MAPS_TABLE    = module.code_maps_table.dynamodb_table_id
    # Retrieval bounds (shared with chat's query-kb path). The MCP server
    # historically returned top_k=10 verbatim with no rerank and no cap
    # (the 60KB-for-one-question failure); these wire the consolidated
    # oversample+rerank+cap pipeline. Defaults in code match these.
    BEDROCK_RERANK_MODEL_ID = var.bedrock_rerank_model_id
    RERANK_ENABLED          = tostring(var.bedrock_rerank_enabled)
    RERANK_MAX_FETCH        = tostring(var.rerank_max_fetch)
    # ADR-030 v1.1: read_source is platform-agnostic. When the
    # workspace's connection is Azure DevOps, the tool refreshes an
    # Entra ID OAuth access token via shared.entra_id_oauth. The
    # tenant + client id + client-secret param name flow through
    # env; the client secret itself is read from SSM at request time
    # (cached at the task lifetime).
    AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
    AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
    AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param
    OAUTH_KMS_KEY_ARN                    = var.enable_workspaces ? aws_kms_key.oauth_tokens[0].arn : ""
    },
    # HA dual-write: the MCP server routes its api-key last_used_at
    # write through shared.dual_write; these env vars make it emit a
    # replication event. Empty in single-region mode.
    local.ha_writer_env,
    # ADR-016 v1.2 -- MCP tool retrieval (embed + rerank; no LLM synthesis
    # on our side -- client does that). Attribute under chat workload
    # since MCP tool queries and chat UI queries hit the same vector
    # index. Split into an mcp workload later if Cost Explorer shows
    # retrieval spend is a meaningful share of chat.
    local.bedrock_chat_env,
  )

  ecs_cluster_arn    = module.ui_ecs.cluster_arn
  vpc_id             = var.ui_vpc_id
  private_subnet_ids = var.ui_private_subnet_ids
  assign_public_ip   = var.ui_fargate_assign_public_ip

  service_discovery_namespace_id   = aws_service_discovery_private_dns_namespace.mcp[0].id
  service_discovery_namespace_name = aws_service_discovery_private_dns_namespace.mcp[0].name

  # ALB routing -- bypasses OIDC, API key auth handled in application
  use_alb                    = var.deploy_ui
  alb_listener_arn           = local.mcp_alb_listener_arn
  alb_listener_rule_priority = 3
  mcp_path_pattern           = "/mcp/v1"

  allowed_security_group_ids = {
    ui_alb = module.ui_alb_sg.security_group_id
  }
}

# State migration: the mcp-server module switched aws_security_group_rule.mcp_ingress
# from `count`-indexed to `for_each`-keyed. Without this, terraform plans a
# destroy + recreate for the existing rule.
moved {
  from = module.mcp_outcomeops[0].aws_security_group_rule.mcp_ingress[0]
  to   = module.mcp_outcomeops[0].aws_security_group_rule.mcp_ingress["ui_alb"]
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
        # fable-scale chunk 02: session store (PK-only, TTL-driven).
        Sid    = "MCPSessionsAccess"
        Effect = "Allow"
        Action = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:DeleteItem",
        ]
        Resource = [
          module.mcp_sessions_table[0].dynamodb_table_arn,
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
        # foundation-model grant stays -- ADR-016 section 5: application
        # inference profiles require BOTH the profile ARN and the
        # underlying model ARN. inference-profile/* covers the profiles
        # in bedrock-inference-profiles.tf (Titan embed under chat
        # workload for MCP retrieval).
        Resource = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      },
      {
        # Post-retrieval reranking (Cohere Rerank via the Bedrock Rerank
        # API), consolidated with chat's query-kb path. Resource must be
        # "*" — same grant query-kb uses (terraform/lambda-query.tf). The
        # reranker degrades to cosine order where unavailable (us-east-2),
        # so this is a quality grant, not a hard dependency.
        Sid    = "BedrockRerank"
        Effect = "Allow"
        Action = [
          "bedrock:Rerank"
        ]
        Resource = ["*"]
      },
      {
        Sid    = "SSMReadMcpTokens"
        Effect = "Allow"
        Action = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        Resource = concat(
          [
            "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/mcp/*",
            # read_source mints GitHub App installation tokens at request
            # time; needs the app id + private key (SecureString -- KMS
            # grant covered by the default-ssm-alias decrypt allowance).
            "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/app-id",
            "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/github/private-key"
          ],
          # ADR-030 v1.1: ADO branch of read_source needs the Entra ID
          # OAuth client secret from SSM to refresh access tokens. The
          # param name is customer-configured; empty tfvars means the
          # ADO code path returns a clean not-configured error rather
          # than crash on init. Only add the ARN when set.
          var.azure_devops_client_secret_ssm_param != "" ? [
            "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.azure_devops_client_secret_ssm_param}"
          ] : [],
        )
      },
      # KMS decrypt on the default aws/ssm alias so the SSM
      # SecureString GetParameter (ADO client secret) succeeds. Same
      # pattern as the analyze-pr Lambda; scoped to the region's
      # default SSM alias.
      {
        Sid    = "KMSDecryptSSMDefaultAlias"
        Effect = "Allow"
        Action = [
          "kms:Decrypt"
        ]
        Resource = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      },
      {
        # plan/audit-completeness: emit ACTION_API_KEY_USED +
        # ACTION_MCP_TOOL_INVOKED audit rows.
        Sid    = "DynamoDBAuditWrite"
        Effect = "Allow"
        Action = [
          "dynamodb:PutItem"
        ]
        Resource = [
          module.audit_logs_table.dynamodb_table_arn
        ]
      },
      {
        # shared.symbol_graph_reader emits OutcomeOps/SymbolGraph::QueryLatency
        # (chunk-08 latency alarm). PutMetricData has no resource-level ARN, so
        # scope it by namespace condition. Without this the emit fails
        # AccessDenied every query (non-fatal, but noisy + blinds the alarm).
        Sid    = "CloudWatchSymbolGraphMetrics"
        Effect = "Allow"
        Action = [
          "cloudwatch:PutMetricData"
        ]
        Resource = ["*"]
        Condition = {
          StringEquals = {
            "cloudwatch:namespace" = "OutcomeOps/SymbolGraph"
          }
        }
      }
    ]
  })
}

# ADR-030 v1.1: KMS decrypt on the OAuth-tokens CMK so the ADO branch
# of read_source can unwrap the refresh_token stored encrypted in DDB
# (shared.entra_id_oauth). Kept as a separate policy so it only exists
# when the OAuth-tokens key exists (var.enable_workspaces=true) — a
# missing OAUTH_KMS_KEY_ARN env var in the task then makes read_source's
# ADO path fail cleanly rather than crash on init.
resource "aws_iam_role_policy" "mcp_outcomeops_ado_kms" {
  count = var.mcp_server_enabled && var.enable_workspaces ? 1 : 0

  name = "${var.environment}-${var.app_name}-mcp-outcomeops-ado-kms"
  role = module.mcp_outcomeops[0].task_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Decrypt to read stored refresh tokens; Encrypt to cache the
        # short-lived access token + persist the rotated refresh token via
        # get_valid_access_token (ADR-050 v1.1). The prior read_source path
        # never persisted rotations because Encrypt was absent and
        # save_tokens swallowed the failure -- quietly poisoning the
        # connection for the next consumer.
        Sid    = "KMSOAuthTokens"
        Effect = "Allow"
        Action = ["kms:Decrypt", "kms:Encrypt"]
        # Both regional ARNs under HA: tokens refreshed while the secondary
        # was active are encrypted under the replica ARN and must stay
        # decryptable here after failback.
        Resource = local.oauth_kms_token_key_arns
      }
    ]
  })
}

# HA dual-write: grant the MCP task role events:PutEvents on the local
# replication bus so its dual_write.ddb_update_item (api-key last_used_at)
# emits a replication event. Gated on HA mode; in single-region the env
# var is empty and dual_write._emit short-circuits anyway.
resource "aws_iam_role_policy" "mcp_outcomeops_ha_events" {
  count = var.mcp_server_enabled && var.secondary_region != "" ? 1 : 0

  name = "${var.environment}-${var.app_name}-mcp-outcomeops-ha-events"
  role = module.mcp_outcomeops[0].task_role_name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "HAEventsPutEvents"
        Effect   = "Allow"
        Action   = ["events:PutEvents"]
        Resource = [aws_cloudwatch_event_bus.replication.arn]
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

# -----------------------------------------------------------------------------
# Secondary-region ECR repository for the OutcomeOps MCP server image
# (HA mode only). Bootstrapped via `make build-mcp-server-image` with the
# multi-region path turned on by the presence of secondary_region in tfvars.
# -----------------------------------------------------------------------------

resource "aws_ecr_repository" "mcp_outcomeops_secondary" {
  count = var.mcp_server_enabled && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name                 = "${var.environment}-${var.app_name}-mcp-outcomeops"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn_secondary
  }

  tags = { Purpose = "mcp-outcomeops" }
}

output "mcp_server_ecr_url_secondary" {
  description = "Secondary-region ECR repository URL for the OutcomeOps MCP server image (null when HA disabled)"
  value       = (var.mcp_server_enabled && var.secondary_region != "") ? aws_ecr_repository.mcp_outcomeops_secondary[0].repository_url : null
}
