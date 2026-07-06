# ============================================================================
# Secondary-region chat + workspace-management Lambdas (HA chunk 9d)
# ----------------------------------------------------------------------------
# Mirror of the two custom-built core Lambdas under aws.secondary. Each
# gets its own Function URL with IAM auth and an SSM parameter holding
# the URL so the secondary UI Fargate (chunk 9h) can resolve it from
# local-region SSM.
#
# Env vars + IAM scope to SECONDARY-region resources only:
#   workspaces_table_secondary, audit_logs_table_secondary,
#   chat_table_secondary, analytics_table_secondary,
#   code_maps_table_secondary, symbol_graph_table_secondary,
#   knowledge_base_bucket_secondary,
#   delete_queue_secondary, oauth_tokens_secondary KMS,
#   cloudwatch_logs_secondary KMS, replication_secondary event bus,
#   ha_secondary AppConfig, platform_ca layer in secondary.
#
# Passive-mode behavior:
# - chat: NO guard. Chat is a user-facing read+write that should work in
#   either region. Chat memory writes go through dual_write (chunk 06a)
#   for cross-region replication.
# - workspace-management: 14 mutation routes gated by
#   shared.region_guard.guard_active (this commit). Reads unaffected.
#
# All modules gated on var.enable_workspaces && var.secondary_region != "".
# Single-region deploys produce no resources here.
# ============================================================================

# ----------------------------------------------------------------------------
# Chat streaming Lambda (secondary)
# ----------------------------------------------------------------------------

module "chat_streaming_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-chat-streaming"
  role_name     = "${var.environment}-${var.app_name}-chat-streaming-secondary"
  description   = "Streaming chat interface (secondary region)"
  handler       = "run.sh"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 1024

  # LWA layer: AWS publishes the same layer in every region under account
  # 753240598075. ARN format swaps region; same layer version.
  # Plus license + platform-CA layers in secondary.
  layers = [
    "arn:aws:lambda:${var.secondary_region}:753240598075:layer:LambdaAdapterLayerX86:25",
    var.outcomeops_license_layer_arn_secondary,
    aws_lambda_layer_version.platform_ca_secondary[0].arn,
  ]

  source_path = [
    {
      path             = "${path.module}/../lambda/chat"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV                                = var.environment
      APP_NAME                           = var.app_name
      AWS_LAMBDA_EXEC_WRAPPER            = "/opt/bootstrap"
      AWS_LWA_INVOKE_MODE                = "RESPONSE_STREAM"
      AWS_LWA_READINESS_CHECK_PATH       = "/health"
      PORT                               = "8000"
      AUDIT_TABLE                        = module.audit_logs_table_secondary[0].dynamodb_table_id
      BEDROCK_ADVANCED_MODEL_ID          = var.bedrock_advanced_model_id
      BEDROCK_DEFAULT_BACKEND            = var.bedrock_default_backend
      BEDROCK_RESPONSES_REASONING_EFFORT = var.bedrock_responses_reasoning_effort
      BEDROCK_MANTLE_API_KEY_PARAM       = "/${var.environment}/${var.app_name}/bedrock/mantle-api-key"
      BEDROCK_BASIC_MODEL_ID             = var.bedrock_basic_model_id
      BEDROCK_ADVANCED_INPUT_PRICE       = tostring(var.bedrock_advanced_input_price)
      BEDROCK_ADVANCED_OUTPUT_PRICE      = tostring(var.bedrock_advanced_output_price)
      BEDROCK_BASIC_INPUT_PRICE          = tostring(var.bedrock_basic_input_price)
      BEDROCK_BASIC_OUTPUT_PRICE         = tostring(var.bedrock_basic_output_price)
      BEDROCK_EMBEDDING_MODEL_ID         = var.bedrock_embedding_model_id
      BEDROCK_EMBEDDING_INPUT_PRICE      = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE     = tostring(var.bedrock_embedding_output_price)
      BEDROCK_RERANK_MODEL_ID            = var.bedrock_rerank_model_id
      BEDROCK_RERANK_PRICE_PER_1K        = tostring(var.bedrock_rerank_price_per_1k)
      RERANK_ENABLED                     = tostring(var.bedrock_rerank_enabled)
      RERANK_MAX_FETCH                   = tostring(var.rerank_max_fetch)
      RERANK_MIN_SCORE                   = tostring(var.rerank_min_score)
      RAG_MAX_DOC_CHARS                  = tostring(var.rag_max_doc_chars)
      WORKSPACES_TABLE                   = module.workspaces_table_secondary[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN                  = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      SYMBOL_GRAPH_TABLE                 = module.symbol_graph_table_secondary[0].dynamodb_table_id
      CODE_MAPS_TABLE                    = module.code_maps_table_secondary[0].dynamodb_table_id
      GRAPH_RETRIEVAL_ENABLED            = "true"
      REQUESTS_CA_BUNDLE                 = "/opt/ca-bundle.pem"
      SSL_CERT_FILE                      = "/opt/ca-bundle.pem"
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
    # AppConfig role-read env so region_guard.guard_active() on the chat
    # mutation routes evaluates THIS region's role. Without it,
    # is_active_region() short-circuits to True and passive-region chats
    # write + replicate to primary (the bidirectional behavior ADR-022
    # rejects). The secondary is passive in steady state, so this is the
    # gate that must fire today.
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge({
    dynamodb = {
      effect  = "Allow"
      actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem", "dynamodb:Query", "dynamodb:Scan"]
      resources = [
        module.chat_table_secondary[0].dynamodb_table_arn,
        "${module.chat_table_secondary[0].dynamodb_table_arn}/index/*",
      ]
    }
    ssm_read = {
      effect    = "Allow"
      actions   = ["ssm:GetParameter", "ssm:GetParameters"]
      resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
    }
    kms_decrypt = {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
    }
    bedrock_invoke = {
      effect  = "Allow"
      actions = ["bedrock:InvokeModel", "bedrock:InvokeModelWithResponseStream", "bedrock:Converse", "bedrock:ConverseStream"]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
      ]
    }
    dynamodb_audit = {
      effect    = "Allow"
      actions   = ["dynamodb:PutItem"]
      resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
    }
    workspaces_table_read = {
      effect = "Allow"
      # chunk 09 (HA parity): dynamodb:Scan was missing on secondary. The
      # global-workspace broaden branch of resolve_chat_workspace_scope
      # (ADR-037) uses Scan to enumerate is_global workspaces; without
      # this grant chat returns AccessDenied for any global-scope query
      # post-failover.
      actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
      resources = [
        module.workspaces_table_secondary[0].dynamodb_table_arn,
        "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
      ]
    }
    oauth_kms_decrypt = {
      effect    = "Allow"
      actions   = ["kms:Decrypt"]
      resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
    }
    bedrock_rerank = {
      effect    = "Allow"
      actions   = ["bedrock:Rerank"]
      resources = ["*"]
    }
    aws_marketplace = {
      effect    = "Allow"
      actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
      resources = ["*"]
    }
    # Symbol-graph reader access — equivalent to primary's
    # iam_symbol_graph.tf attachment but inlined here since the policy
    # is scoped to secondary-region ARNs.
    symbol_graph_read = {
      effect  = "Allow"
      actions = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:BatchGetItem"]
      resources = [
        module.symbol_graph_table_secondary[0].dynamodb_table_arn,
        "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-kind-index",
        "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-prefix-index",
        "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-name-index",
      ]
    }
    code_maps_pointer_read = {
      effect  = "Allow"
      actions = ["dynamodb:GetItem", "dynamodb:Query"]
      resources = [
        module.code_maps_table_secondary[0].dynamodb_table_arn,
        "${module.code_maps_table_secondary[0].dynamodb_table_arn}/index/*",
      ]
    }
    s3_vectors = {
      effect  = "Allow"
      actions = ["s3vectors:QueryVectors", "s3vectors:GetVectors", "s3vectors:PutVectors", "s3vectors:DeleteVectors"]
      resources = [
        "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*",
      ]
    }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam_secondary,
    # appconfig:GetLatestConfiguration for the region-role read above.
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "chat-streaming" }
}

resource "aws_lambda_function_url" "chat_streaming_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.chat_streaming_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"
  invoke_mode        = "RESPONSE_STREAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST", "GET", "DELETE", "PUT"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "chat_streaming_url_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/chat-streaming-url"
  description = "Lambda Function URL for streaming chat in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.chat_streaming_secondary[0].function_url

  tags = { Purpose = "chat-streaming-url" }
}

# ----------------------------------------------------------------------------
# Workspace management Lambda (secondary)
# ----------------------------------------------------------------------------

module "workspace_management_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-workspace-management"
  role_name     = "${var.environment}-${var.app_name}-workspace-management-secondary"
  description   = "Workspace management (secondary region)"
  handler       = "run.sh"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 512

  # NO license layer for workspace-management (matches primary).
  layers = [
    "arn:aws:lambda:${var.secondary_region}:753240598075:layer:LambdaAdapterLayerX86:25",
    aws_lambda_layer_version.platform_ca_secondary[0].arn,
  ]

  source_path = [
    {
      path             = "${path.module}/../lambda/workspace-management"
      pip_requirements = true
      patterns         = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV                          = var.environment
      APP_NAME                     = var.app_name
      AWS_LAMBDA_EXEC_WRAPPER      = "/opt/bootstrap"
      AWS_LWA_READINESS_CHECK_PATH = "/health"
      PORT                         = "8000"
      KB_BUCKET                    = module.knowledge_base_bucket_secondary.s3_bucket_id
      DELETE_QUEUE_URL             = aws_sqs_queue.delete_queue_secondary[0].url
      OAUTH_KMS_KEY_ARN            = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      AUDIT_TABLE                  = module.audit_logs_table_secondary[0].dynamodb_table_id
      ANALYTICS_TABLE              = module.analytics_table_secondary[0].dynamodb_table_id
      PII_REDACTION_ENABLED        = tostring(var.pii_redaction_enabled)
      REQUESTS_CA_BUNDLE           = "/opt/ca-bundle.pem"
      SSL_CERT_FILE                = "/opt/ca-bundle.pem"

      INTEGRATION_AZURE_DEVOPS_ENABLED        = tostring(var.enable_azure_devops_integration)
      INTEGRATION_AZURE_DEVOPS_BOARDS_ENABLED = tostring(var.enable_azure_devops_boards_integration)
      INTEGRATION_GITHUB_ENABLED              = tostring(var.enable_github_integration)
      INTEGRATION_GITHUB_ISSUES_ENABLED       = tostring(var.enable_github_issue_integration)
      INTEGRATION_CONFLUENCE_ENABLED          = tostring(var.enable_confluence_integration)
      INTEGRATION_JIRA_ENABLED                = tostring(var.enable_jira_integration)
      INTEGRATION_OUTLOOK_ENABLED             = tostring(var.enable_outlook_integration)
      INTEGRATION_TEAMS_ENABLED               = tostring(var.enable_teams_integration)
      INTEGRATION_SHAREPOINT_ENABLED          = tostring(var.enable_sharepoint_integration)
      INTEGRATION_ONEDRIVE_ENABLED            = tostring(var.enable_onedrive_integration)
      INTEGRATION_ONENOTE_ENABLED             = tostring(var.enable_onenote_integration)
      INTEGRATION_DATABASE_ENABLED            = tostring(var.enable_database_integration)
    },
    local.ha_writer_env_secondary,
    local.ha_scheduler_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan", "dynamodb:BatchWriteItem"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      dynamodb_audit = {
        effect  = "Allow"
        actions = ["dynamodb:PutItem", "dynamodb:Query", "dynamodb:GetItem", "dynamodb:Scan"]
        resources = [
          module.audit_logs_table_secondary[0].dynamodb_table_arn,
          "${module.audit_logs_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      dynamodb_analytics = {
        effect    = "Allow"
        actions   = ["dynamodb:Query", "dynamodb:GetItem"]
        resources = [module.analytics_table_secondary[0].dynamodb_table_arn]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
      }
      kms_decrypt = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      s3_documents = {
        effect  = "Allow"
        actions = ["s3:PutObject", "s3:GetObject", "s3:DeleteObject", "s3:ListBucket"]
        resources = [
          module.knowledge_base_bucket_secondary.s3_bucket_arn,
          "${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*",
        ]
      }
      oauth_kms = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      sqs_send = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.delete_queue_secondary[0].arn]
      }
    },
    local.ha_writer_events_iam_secondary,
    local.ha_scheduler_iam_secondary,
  )

  tags = { Purpose = "workspace-management" }
}

resource "aws_lambda_function_url" "workspace_management_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  function_name      = module.workspace_management_lambda_secondary[0].lambda_function_name
  authorization_type = "AWS_IAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST", "GET", "PATCH", "DELETE"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

resource "aws_ssm_parameter" "workspace_management_url_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name        = "/${var.environment}/${var.app_name}/api/workspace-management-url"
  description = "Lambda Function URL for workspace management in secondary region (IAM auth)"
  type        = "String"
  value       = aws_lambda_function_url.workspace_management_secondary[0].function_url

  tags = { Purpose = "workspace-management-url" }
}

# ============================================================================
# Outputs — Function URLs consumed by the secondary UI Fargate (chunk 9h)
# ============================================================================

output "chat_streaming_url_secondary" {
  description = "Lambda Function URL for streaming chat in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.chat_streaming_secondary[0].function_url : null
}

output "workspace_management_url_secondary" {
  description = "Lambda Function URL for workspace management in secondary region (null when HA disabled)"
  value       = (var.enable_workspaces && var.secondary_region != "") ? aws_lambda_function_url.workspace_management_secondary[0].function_url : null
}
