# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# PR Analysis Lambda Functions
# - analyze-pr: Analyze GitHub PRs and queue architecture check jobs
# - process-pr-check: Process PR check jobs from SQS queue
# ============================================================================

module "analyze_pr_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-analyze-pr"
  description   = "Analyze GitHub PRs and queue architecture check jobs"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes for GitHub API calls and job queueing
  memory_size   = 512

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory.
  source_path = [
    {
      path             = "${path.module}/../lambda/analyze-pr"
      pip_requirements = true
      patterns = [
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
        "!tests/.*",
      ]
    },
    # analyze-pr's ADO dispatch path (widened for provider awareness in
    # the AI PR Analyzer follow-up plan) consumes shared.pr_metadata,
    # shared.source_fetch, and shared.repo_metadata. Ship them here the
    # same way process-pr-check does.
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables.
  #
  # CODE_MAPS_TABLE holds analyze-pr's per-PR comment/summary state.
  environment_variables = merge({
    ENV             = var.environment
    APP_NAME        = var.app_name
    GITHUB_BASE_URL = var.github_base_url
    CODE_MAPS_TABLE = module.code_maps_table.dynamodb_table_id

    # Azure DevOps OAuth app credentials -- needed for the widened
    # provider-aware dispatch path (analyze-pr accepts
    # provider="azure_devops" requests + refreshes the customer's
    # OAuth access token before fetching PR metadata / posting the
    # initial "analysis started" comment). Empty on customer deploys
    # that haven't configured ADO; the ADO branch surfaces a clear
    # error in that case.
    #
    # Token refresh reads the encrypted refresh token from
    # WORKSPACES_TABLE (AZDO_TOKEN# rows), decrypts via the OAuth
    # tokens KMS key, and refreshes against the customer's Entra ID
    # tenant. Client secret comes from SSM at request time. All
    # gated on enable_workspaces so customer deploys without the
    # workspaces feature still build (the workspaces table + KMS
    # key only exist when enable_workspaces=true).
    WORKSPACES_TABLE                     = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_id : ""
    OAUTH_KMS_KEY_ARN                    = var.enable_workspaces ? aws_kms_key.oauth_tokens[0].arn : ""
    AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
    AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
    AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

    # GitLab MR-analysis dispatch. Same pattern as the ADO
    # block above: empty on deploys without GitLab; the gitlab branch
    # surfaces a clear error in that case. Token acquisition goes
    # through the cached single-flight path (ADR-050 v1.1) which reads
    # GITLAB_TOKEN# rows from WORKSPACES_TABLE and encrypts the cached
    # access token with the OAuth tokens KMS key (both wired above).
    GITLAB_BASE_URL                = var.gitlab_base_url
    GITLAB_CLIENT_ID               = var.gitlab_client_id
    GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path
    GITLAB_INSTANCES               = local.gitlab_instances_env

    # PR summary comment (COMMENT#summary) calls Bedrock directly via
    # shared.model_client at workload=pr-analyzer, tier=advanced. In prd the
    # Claude app-inference-profiles are disabled, so the model id comes from
    # the BEDROCK_ADVANCED_MODEL_ID legacy fallback; bedrock_pr_analyzer_env
    # below supplies the profile ARNs when app profiles are enabled. Both the
    # advanced and basic ids are the tfvar-driven, per-customer-swappable model
    # ids (advanced may be Sonnet or Opus); the Lambda never hardcodes a model.
    BEDROCK_ADVANCED_MODEL_ID = var.bedrock_advanced_model_id
    BEDROCK_BASIC_MODEL_ID    = var.bedrock_basic_model_id
    BEDROCK_DEFAULT_BACKEND   = var.bedrock_default_backend
    },
    # ADR-016 v1.2 -- analyze-pr's summary comment is the pr-analyzer workload.
    local.bedrock_pr_analyzer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # Bedrock models (the summary comment calls shared.model_client
    # directly). All foundation models + inference profiles for model
    # portability, mirroring process-pr-check.
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
      ]
    }

    # Read SSM parameters (GitHub token and SQS queue URL)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # Decrypt SSM parameters encrypted with AWS managed KMS key
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # Send messages to SQS PR checks queue
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.pr_checks_queue.queue_arn
      ]
    }

    # PR-comment upsert (analyze-pr's analysis-started /
    # structural-impact / no-checks-needed comments each carry a
    # companion DDB row keyed by (PR x comment-role); on subsequent
    # runs the handler PATCHes instead of POSTing so a customer that
    # pushes 5 times doesn't get 5 duplicate analysis-started
    # comments). Read + write on the code-maps table.
    dynamodb_code_maps_upsert = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn,
      ]
    }
    },
    # ADO dispatch path (widened analyze-pr) needs DDB read + KMS
    # decrypt to refresh the customer's OAuth token on request-
    # entry. Matches the same grants process-pr-check gets.
    var.enable_workspaces ? {
      dynamodb_workspaces_read = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
        ]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = local.oauth_kms_token_key_arns
      }
    } : {},
  )

  tags = {
    Purpose = "analyze-pr"
  }
}

output "analyze_pr_lambda_arn" {
  description = "ARN of the analyze-pr Lambda function"
  value       = module.analyze_pr_lambda.lambda_function_arn
}

output "analyze_pr_lambda_name" {
  description = "Name of the analyze-pr Lambda function"
  value       = module.analyze_pr_lambda.lambda_function_name
}

# Allow customer-defined CI role(s) to invoke analyze-pr directly -- the
# CI-triggered PR-analysis path (no public webhook; see var.ci_invoker_role_arns
# and .github/workflows/pr-analysis.yml). A resource-policy grant per listed
# role, so the CI role's own identity policy (often read-only) is untouched.
resource "aws_lambda_permission" "analyze_pr_ci_invoke" {
  for_each      = toset(var.ci_invoker_role_arns)
  statement_id  = "AllowCIInvoke-${substr(sha1(each.value), 0, 12)}"
  action        = "lambda:InvokeFunction"
  function_name = module.analyze_pr_lambda.lambda_function_name
  principal     = each.value
}

# ============================================================================
# Process PR Check Lambda (SQS Consumer)
# ============================================================================

module "process_pr_check_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-process-pr-check"
  description   = "Process PR check jobs from SQS queue (worker)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 600  # 10 minutes for Claude-based checks
  memory_size   = 1024 # Higher memory for Bedrock API calls

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module)
  source_path = [
    "${path.module}/../lambda/process-pr-check",
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = merge({
    ENV                  = var.environment
    APP_NAME             = var.app_name
    GITHUB_BASE_URL      = var.github_base_url
    AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
    AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)

    # Plane-1 breaking-change detection (ADR-054): shared.symbol_graph_reader
    # resolves a changed symbol's callers/importers from the symbol-graph table;
    # CODE_MAPS_TABLE feeds the per-(workspace,repo) pointer for staleness
    # reporting (best-effort). Query-only IAM is attached in iam_symbol_graph.tf
    # (process_pr_check_symbol_graph_reader). Mirrors analyze-pr's wiring.
    SYMBOL_GRAPH_TABLE                 = module.symbol_graph_table.dynamodb_table_id
    CODE_MAPS_TABLE                    = module.code_maps_table.dynamodb_table_id
    BEDROCK_ADVANCED_MODEL_ID          = var.bedrock_advanced_model_id
    BEDROCK_DEFAULT_BACKEND            = var.bedrock_default_backend
    BEDROCK_RESPONSES_REASONING_EFFORT = var.bedrock_responses_reasoning_effort
    BEDROCK_MANTLE_API_KEY_PARAM       = "/${var.environment}/${var.app_name}/bedrock/mantle-api-key"
    BEDROCK_BASIC_MODEL_ID             = var.bedrock_basic_model_id
    BEDROCK_ADVANCED_INPUT_PRICE       = tostring(var.bedrock_advanced_input_price)
    BEDROCK_ADVANCED_OUTPUT_PRICE      = tostring(var.bedrock_advanced_output_price)
    BEDROCK_BASIC_INPUT_PRICE          = tostring(var.bedrock_basic_input_price)
    BEDROCK_BASIC_OUTPUT_PRICE         = tostring(var.bedrock_basic_output_price)

    # ADO PR-comment posting reads the customer's OAuth
    # refresh token from WORKSPACES_TABLE (AZDO_TOKEN# rows),
    # decrypts via the OAuth tokens KMS key, and refreshes against
    # the customer's Entra ID tenant. tenant_id + client_id come
    # from tfvars; the client secret comes from SSM at request
    # time. All four default to "" so the build runs in deploys
    # that haven't enabled the ADO integration.
    WORKSPACES_TABLE                     = var.enable_workspaces ? module.workspaces_table[0].dynamodb_table_id : ""
    OAUTH_KMS_KEY_ARN                    = var.enable_workspaces ? aws_kms_key.oauth_tokens[0].arn : ""
    AZURE_DEVOPS_TENANT_ID               = var.azure_devops_tenant_id
    AZURE_DEVOPS_CLIENT_ID               = var.azure_devops_client_id
    AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = var.azure_devops_client_secret_ssm_param

    # GitLab MR-check dispatch. Token acquisition goes
    # through the cached single-flight path (ADR-050 v1.1) reading
    # GITLAB_TOKEN# rows from WORKSPACES_TABLE + the OAuth KMS key
    # (both wired above). Empty on deploys without GitLab.
    GITLAB_BASE_URL                = var.gitlab_base_url
    GITLAB_CLIENT_ID               = var.gitlab_client_id
    GITLAB_CLIENT_SECRET_SSM_PARAM = local.gitlab_secret_ssm_path
    GITLAB_INSTANCES               = local.gitlab_instances_env
    },
    # HA dual-write: emit replication events for the check-result row.
    local.ha_writer_env,
    # ADR-016 v1.2 -- process-pr-check is the pr-analyzer workload.
    local.bedrock_pr_analyzer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # Write to DynamoDB code maps table. DeleteItem is required so the
    # inline-comment reconciler (_sync_inline_findings -> _delete_inline_row)
    # can remove the INLINE# bookkeeping row when a finding is resolved; without
    # it the row orphans and a fixed-then-reintroduced finding at the same line
    # tries to patch an already-deleted provider discussion and never re-posts.
    dynamodb_write = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:DeleteItem",
        "dynamodb:Query"
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn
      ]
    }

    # Read SSM parameters (including encrypted GitHub token)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # Decrypt SSM parameters encrypted with AWS managed KMS key
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # Bedrock models (all foundation models and inference profiles for model portability)
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*"
      ]
    }

    # AWS Marketplace permissions for Bedrock models
    aws_marketplace = {
      effect = "Allow"
      actions = [
        "aws-marketplace:ViewSubscriptions",
        "aws-marketplace:Subscribe"
      ]
      resources = ["*"]
    }

    # Invoke query-kb Lambda (for ADR compliance and architectural duplication checks)
    lambda_invoke = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.query_kb_lambda.lambda_function_arn
      ]
    }

    # SQS permissions for event source mapping
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.pr_checks_queue.queue_arn
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

    # shared.symbol_graph_reader emits OutcomeOps/SymbolGraph::QueryLatency
    # from the pr-check path too -- BREAKING_CHANGES
    # plane-1 queries the symbol graph per check. PutMetricData has no
    # resource-level ARN, so scope by namespace (mirrors lambda-chat.tf /
    # mcp-outcomeops.tf). Without this the emit AccessDenies on every query
    # and the latency metric is silently never recorded.
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
    },
    # ADO PR-comment posting needs DDB read on AZDO_TOKEN#
    # rows + KMS decrypt on the OAuth tokens key. Gated on
    # enable_workspaces because the workspaces table + oauth_tokens
    # key only exist in that mode.
    var.enable_workspaces ? {
      dynamodb_workspaces_read = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query",
          # Scan needed for pr_check_scope resolver: finds workspaces
          # for a repo + lists is_global workspaces + (for
          # ARCHITECTURAL_DUPLICATION) lists every workspace_id.
          # Filtered scans; not a hot path (once per check).
          "dynamodb:Scan",
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
        ]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = local.oauth_kms_token_key_arns
      }
    } : {},
    # HA dual-write: events:PutEvents on the local replication bus.
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "process-pr-check"
  }
}

# Event source mapping - Connect SQS pr-checks-queue to Lambda
resource "aws_lambda_event_source_mapping" "pr_checks_queue_to_lambda" {
  event_source_arn = module.pr_checks_queue.queue_arn
  function_name    = module.process_pr_check_lambda.lambda_function_name
  batch_size       = 1 # Process one check job at a time
  enabled          = true

  # Function response types for partial batch failures
  function_response_types = ["ReportBatchItemFailures"]

  # cap PR-analyzer concurrency so a Monday-9am burst
  # of 200 PRs doesn't starve the interactive chat surface of Bedrock quota.
  # Absent this cap, process-pr-check spins up as many concurrent Lambdas as
  # SQS delivers messages, each holding a Sonnet inference slot.
  scaling_config {
    maximum_concurrency = var.pr_check_max_concurrency
  }
}

output "process_pr_check_lambda_arn" {
  description = "ARN of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_arn
}

output "process_pr_check_lambda_name" {
  description = "Name of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_name
}
