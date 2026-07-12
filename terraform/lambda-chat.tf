# ============================================================================
# Chat Streaming Lambda
# Uses Lambda Web Adapter (LWA) for response streaming
# API Gateway HTTP API has 30s timeout - streaming requires Lambda Function URL
# ============================================================================

module "chat_streaming_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-chat-streaming"
  description   = "Streaming chat interface for OutcomeOps AI Assist"
  handler       = "run.sh"
  runtime       = "python3.12"
  # fable-scale chunk 05: timeout is tfvars-tunable. Was 900 (streaming max);
  # 300 covers tool-use worst case comfortably and unblocks Lambda
  # concurrency slots sooner under sustained load.
  timeout     = var.chat_timeout_seconds
  memory_size = 1024

  # fable-scale chunk 05: reserved_concurrent_executions guarantees the
  # chat surface a slice of the account concurrency pool so batch jobs
  # (PR checks, code-maps ingestion) can't starve interactive chat.
  # Default -1 (== unreserved) preserves sandbox behavior.
  reserved_concurrent_executions = var.chat_reserved_concurrency

  # Lambda Web Adapter layer for HTTP streaming + OutcomeOps license validator
  # (ADR-019: license layer is required on every customer-facing Lambda).
  # The platform-CA layer is also attached unconditionally so that under
  # BYO-internal-CA deploys (ui_acm_certificate_arn pointing at a private-PKI
  # cert), the requests calls to the MCP servers in lambda/chat/handler.py
  # actually trust the listener. Empty platform_ca_bundle_ssm_param_arns
  # ships pure certifi so this is a no-op for default-public-cert deploys.
  layers = [
    "arn:aws:lambda:${var.aws_region}:753240598075:layer:LambdaAdapterLayerX86:25",
    var.outcomeops_license_layer_arn,
    aws_lambda_layer_version.platform_ca.arn,
  ]

  # Source code with pip requirements (includes shared module)
  source_path = [
    {
      path             = "${path.module}/../lambda/chat"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!requirements-dev\\.txt",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables for LWA. Merged with the shared license-layer env
  # block (OUTCOMEOPS_LICENSE_PARAM / SERVER_URL / USAGE_ENABLED) from
  # local.outcomeops_license_env -- see locals.tf and ADR-019.
  environment_variables = merge(
    {
      ENV                                = var.environment
      APP_NAME                           = var.app_name
      AWS_LAMBDA_EXEC_WRAPPER            = "/opt/bootstrap"
      AWS_LWA_INVOKE_MODE                = "RESPONSE_STREAM"
      AWS_LWA_READINESS_CHECK_PATH       = "/health"
      PORT                               = "8000"
      AUDIT_TABLE                        = module.audit_logs_table.dynamodb_table_id
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
      # fable-scale chunk 10: chat message hygiene knobs.
      CHAT_MESSAGE_TTL_SECONDS       = tostring(var.chat_message_ttl_seconds)
      CHAT_MAX_MESSAGE_CONTENT_CHARS = tostring(var.chat_max_message_content_chars)
      BEDROCK_PROMPT_CACHE_ENABLED   = tostring(var.bedrock_prompt_cache_enabled)
      WORKSPACES_TABLE               = module.workspaces_table[0].dynamodb_table_id
      OAUTH_KMS_KEY_ARN              = aws_kms_key.oauth_tokens[0].arn

      # Symbol-graph router + context builder. IAM read access is
      # attached separately in iam_symbol_graph.tf; the env vars here
      # let the chat handler resolve the tables without per-cold-start
      # SSM lookups. CODE_MAPS_TABLE is used by symbol_graph_reader for
      # the per-repo staleness pointer.
      SYMBOL_GRAPH_TABLE = module.symbol_graph_table.dynamodb_table_id
      CODE_MAPS_TABLE    = module.code_maps_table.dynamodb_table_id

      # Platform-wide kill-switch (default ON). Operators can disable
      # graph retrieval without redeploy by flipping this in console /
      # SSM-backed config; the chat handler falls back to RAG-only
      # behavior.
      GRAPH_RETRIEVAL_ENABLED = "true"

      # Point requests / urllib3 / httpx / stdlib ssl + urllib.request at
      # the merged PEM shipped in the platform-CA layer so BYO-internal-CA
      # MCP listeners are trusted. Both env vars are needed because
      # `requests` honors REQUESTS_CA_BUNDLE and stdlib honors SSL_CERT_FILE.
      # /opt/ca-bundle.pem is the layer's mount path -- see
      # lambda-platform-ca-layer.tf.
      REQUESTS_CA_BUNDLE = "/opt/ca-bundle.pem"
      SSL_CERT_FILE      = "/opt/ca-bundle.pem"
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
    # AppConfig role-read env so shared.region_guard.guard_active() on the
    # chat mutation routes (chunk 05) actually evaluates the local
    # region's active/passive role. Without this, is_active_region()
    # takes the single-region fast-path (returns True) and passive-region
    # chats would write + replicate — the bidirectional behavior ADR-022
    # explicitly rejects. Post-failover this region becomes passive and
    # the gate must fire here too.
    local.ha_scheduler_env,
    # ADR-016 v1.2 -- chat workload profile ARNs (all 4 models used).
    # shared.model_client.resolve_profile_arn(workload="chat", tier=...)
    # reads these. Legacy BEDROCK_*_MODEL_ID env vars stay for the
    # WARN-fallback path per chunk 03.
    local.bedrock_chat_env,
  )

  # IAM permissions. Merged with the shared license-layer IAM block
  # (SSM read on the license SSM param + KMS decrypt for aws/ssm) from
  # local.outcomeops_license_iam -- see locals.tf and ADR-019.
  attach_policy_statements = true
  policy_statements = merge({
    # DynamoDB for conversations and messages
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
        "dynamodb:Query",
        "dynamodb:Scan"
      ]
      resources = [
        module.chat_table.dynamodb_table_arn,
        "${module.chat_table.dynamodb_table_arn}/index/*"
      ]
    }

    # shared.symbol_graph_reader emits OutcomeOps/SymbolGraph::QueryLatency
    # (chunk-08 latency alarm) from the chat path too. PutMetricData has no
    # resource ARN, so scope by namespace. Without this the emit AccessDenies.
    cloudwatch_symbol_graph_metrics = {
      effect    = "Allow"
      actions   = ["cloudwatch:PutMetricData"]
      resources = ["*"]
      conditions = [{
        test     = "StringEquals"
        variable = "cloudwatch:namespace"
        values   = ["OutcomeOps/SymbolGraph"]
      }]
    }

    # SSM for configuration (vector bucket, index names, etc.)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # KMS for decrypting SSM parameters
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # Bedrock for streaming chat (Claude) and embeddings (Titan)
    bedrock_invoke = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel",
        "bedrock:InvokeModelWithResponseStream",
        "bedrock:Converse",
        "bedrock:ConverseStream"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
      ]
    }

    # DynamoDB audit logging
    dynamodb_audit = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem"
      ]
      resources = [
        module.audit_logs_table.dynamodb_table_arn
      ]
    }

    # DynamoDB read on workspaces table (for MCP configs).
    # Scan added for ADR-037's ``resolve_chat_workspace_scope``:
    # when a global-workspace member queries from their global
    # workspace, the resolver walks every workspace metadata row
    # to build the bidirectional-broaden scope. Filtered scan; not
    # a hot path (once per turn on the broaden branch only).
    workspaces_table_read = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:Scan",
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
        "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
      ]
    }

    # KMS decrypt for OAuth key (MCP credential decryption)
    oauth_kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        aws_kms_key.oauth_tokens[0].arn
      ]
    }

    # Bedrock Rerank API for post-retrieval reranking
    bedrock_rerank = {
      effect = "Allow"
      actions = [
        "bedrock:Rerank"
      ]
      resources = ["*"]
    }

    # AWS Marketplace permissions for Bedrock models (required for Cohere Rerank)
    aws_marketplace = {
      effect = "Allow"
      actions = [
        "aws-marketplace:ViewSubscriptions",
        "aws-marketplace:Subscribe"
      ]
      resources = ["*"]
    }

    # Code-maps pointer read for symbol-graph staleness reporting.
    # The symbol_graph_reader queries the per-(workspace, repo) pointer
    # row to compute staleness_seconds so chat answers can disclose
    # graph freshness honestly. Read-only by design; the writer policy
    # (in iam_symbol_graph.tf and lambda-knowledge-base.tf) handles
    # writes.
    code_maps_pointer_read = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:Query"
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn,
        "${module.code_maps_table.dynamodb_table_arn}/index/*"
      ]
    }

    # S3 Vectors for KB search and conversation memory
    s3_vectors = {
      effect = "Allow"
      actions = [
        "s3vectors:QueryVectors",
        "s3vectors:GetVectors",
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam,
    # appconfig:GetLatestConfiguration for the region-role read above.
    local.ha_scheduler_iam,
  )

  tags = {
    Purpose = "chat-streaming"
  }
}

# Lambda Function URL with IAM auth - only Fargate task role can invoke
# Security: Requests must be SigV4 signed, no anonymous access
resource "aws_lambda_function_url" "chat_streaming" {
  function_name      = module.chat_streaming_lambda.lambda_function_name
  authorization_type = "AWS_IAM"

  # Enable response streaming mode
  invoke_mode = "RESPONSE_STREAM"

  cors {
    allow_origins     = ["*"]
    allow_methods     = ["POST", "GET", "DELETE", "PUT"]
    allow_headers     = ["content-type", "authorization", "x-amz-date", "x-amz-security-token", "x-user-email"]
    allow_credentials = false
    max_age           = 300
  }
}

# Store streaming URL in SSM for Fargate proxy
resource "aws_ssm_parameter" "chat_streaming_url" {
  name        = "/${var.environment}/${var.app_name}/api/chat-streaming-url"
  description = "Lambda Function URL for streaming chat (IAM auth required)"
  type        = "String"
  value       = aws_lambda_function_url.chat_streaming.function_url

  tags = {
    Purpose = "chat-streaming-url"
  }
}

output "chat_streaming_lambda_name" {
  description = "Name of the chat streaming Lambda function"
  value       = module.chat_streaming_lambda.lambda_function_name
}

output "chat_streaming_lambda_arn" {
  description = "ARN of the chat streaming Lambda function"
  value       = module.chat_streaming_lambda.lambda_function_arn
}
