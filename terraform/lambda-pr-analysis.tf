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
  #
  # analyze-pr's structural-impact pass needs the symbol-graph
  # extractor at runtime so canonical symbol IDs produced from
  # post-diff file content line up exactly with the graph. The
  # extractor lives in lambda/process-batch-summary/extractor because
  # that's where the writer runs; here we ship it as a prefixed
  # subdirectory so the lazy import in diff_symbol_resolver.py finds it
  # without a sys.path shim in the deployed zip. Local tests keep the
  # sys.path fallback for convenience.
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
    {
      path          = "${path.module}/../lambda/process-batch-summary/extractor"
      prefix_in_zip = "extractor"
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
  # Wire the symbol-graph reader's two anchor tables; the SSM params
  # exist alongside the DDB module but we read directly from the module
  # output rather than re-resolving SSM at cold-start.
  # GRAPH_RETRIEVAL_ENABLED is the platform-wide kill-switch (defaults
  # to "true" via the handler) so an operator can flip the flag without
  # redeploy via SSM-backed env override. PR_WORKSPACE_ID is NOT wired
  # here -- analyze-pr resolves repo -> workspace at runtime; plumbed
  # through handler.py via the env var only as a forward-compat seam.
  environment_variables = {
    ENV                     = var.environment
    APP_NAME                = var.app_name
    SYMBOL_GRAPH_TABLE      = module.symbol_graph_table.dynamodb_table_id
    CODE_MAPS_TABLE         = module.code_maps_table.dynamodb_table_id
    GRAPH_RETRIEVAL_ENABLED = "true"

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
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
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
        resources = [aws_kms_key.oauth_tokens[0].arn]
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
    ENV                                = var.environment
    APP_NAME                           = var.app_name
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

    # chunk 07: ADO PR-comment posting reads the customer's OAuth
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
    },
    # HA dual-write: emit replication events for the check-result row.
    local.ha_writer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # Write to DynamoDB code maps table
    dynamodb_write = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
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
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*"
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
    },
    # chunk 07: ADO PR-comment posting needs DDB read on AZDO_TOKEN#
    # rows + KMS decrypt on the OAuth tokens key. Gated on
    # enable_workspaces because the workspaces table + oauth_tokens
    # key only exist in that mode.
    var.enable_workspaces ? {
      dynamodb_workspaces_read = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
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
        resources = [aws_kms_key.oauth_tokens[0].arn]
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
}

output "process_pr_check_lambda_arn" {
  description = "ARN of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_arn
}

output "process_pr_check_lambda_name" {
  description = "Name of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_name
}
