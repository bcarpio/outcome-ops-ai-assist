# ============================================================================
# Knowledge Base Lambda Functions
# - process-batch-summary: Process code map batch summaries (SQS consumer)
# - process-repo-summary: Process repository architectural summaries
# ============================================================================

# ============================================================================
# Process Batch Summary Lambda (SQS Consumer)
# ============================================================================

module "process_batch_summary_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-process-batch-summary"
  description   = "Process code map batch summaries from SQS queue"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 900 # 15 minutes for processing large batches
  # chunk 09 (HA alignment): raised from 1024 to 2048 to match the
  # secondary. The secondary already ran at 2048; primary at 1024 was
  # the divergence, and symbol-graph extraction on large batches was
  # near-OOM under coverage instrumentation.
  memory_size = 2048

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module).
  # lambda/process-batch-summary/requirements.txt pins tree-sitter +
  # per-language grammars used by the symbol-graph extractor; the
  # extractor ImportErrors without them. pip_requirements = true tells
  # the terraform-aws-modules/lambda module to pip-install those into
  # the deploy zip during `terraform apply`. patterns exclude test code
  # and venv detritus.
  source_path = [
    {
      path             = "${path.module}/../lambda/process-batch-summary"
      pip_requirements = true
      patterns = [
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
        "!tests/.*",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = merge({
    ENV      = var.environment
    APP_NAME = var.app_name
    # chunk 09 (HA cleanup): EVENT_BUS_NAME removed. shared.dual_write
    # only reads REPLICATION_EVENT_BUS_NAME; EVENT_BUS_NAME had no
    # consumer and its value diverged primary vs secondary (automation
    # bus here, replication bus there) -- silent semantic mismatch.
    ENABLE_BEDROCK_COOLDOWN            = tostring(var.enable_bedrock_cooldown)
    AUDIT_TABLE                        = module.audit_logs_table.dynamodb_table_id
    WORKSPACES_TABLE                   = module.workspaces_table[0].dynamodb_table_id
    BEDROCK_ADVANCED_MODEL_ID          = var.bedrock_advanced_model_id
    BEDROCK_DEFAULT_BACKEND            = var.bedrock_default_backend
    BEDROCK_RESPONSES_REASONING_EFFORT = var.bedrock_responses_reasoning_effort
    BEDROCK_MANTLE_API_KEY_PARAM       = "/${var.environment}/${var.app_name}/bedrock/mantle-api-key"
    BEDROCK_EMBEDDING_MODEL_ID         = var.bedrock_embedding_model_id
    BEDROCK_ADVANCED_INPUT_PRICE       = tostring(var.bedrock_advanced_input_price)
    BEDROCK_ADVANCED_OUTPUT_PRICE      = tostring(var.bedrock_advanced_output_price)
    BEDROCK_BASIC_INPUT_PRICE          = tostring(var.bedrock_basic_input_price)
    BEDROCK_BASIC_OUTPUT_PRICE         = tostring(var.bedrock_basic_output_price)
    BEDROCK_EMBEDDING_INPUT_PRICE      = tostring(var.bedrock_embedding_input_price)
    BEDROCK_EMBEDDING_OUTPUT_PRICE     = tostring(var.bedrock_embedding_output_price)
    PII_REDACTION_ENABLED              = tostring(var.pii_redaction_enabled)
    COMPREHEND_PII_PRICE_PER_1M_CHARS  = tostring(var.comprehend_pii_price_per_1m_chars)
    PII_REDACTION_SKIP_TYPES           = join(",", var.pii_redaction_skip_types)
    # Plan-gkg chunks 02 + 03 — symbol-graph regen + pointer-swap path.
    SYMBOL_GRAPH_TABLE = module.symbol_graph_table.dynamodb_table_id
    CODE_MAPS_TABLE    = module.code_maps_table.dynamodb_table_id
    # plan-git-truth chunk 01: SHA-truth ledger tables. process-batch-summary
    # owns the gkg ledger (dispatcher + consumer in one Lambda). Wired but
    # not yet imported; chunk 02 lights it up.
    PENDING_WORK_TABLE = module.code_map_pending_work_table.dynamodb_table_id
    FAILED_FILES_TABLE = module.code_map_failed_files_table.dynamodb_table_id
    # chunk 08a: when fetch_file_content is called with
    # provider=azure_devops it reads from the S3 mirror at
    # workspaces/{ws}/azure_devops/{slug}/{path} that
    # azure-devops-sync wrote (chunk 05a). KB_BUCKET is the same
    # bucket azure-devops-sync writes to.
    KB_BUCKET = module.knowledge_base_bucket.s3_bucket_id
    # Chunk 05: merge-message requeue-with-delay target. When a merge
    # message arrives before all shard summaries have landed, the
    # handler enqueues itself back onto this queue with a delay so
    # SQS re-delivers once shards are done. Same queue this Lambda
    # already consumes from (event source mapping below).
    CODE_MAPS_QUEUE_URL = module.code_maps_queue.queue_url
    # Chunk 06: token-sizing knobs shared with process-repo-summary.
    # Change without redeploy by rotating the tfvar; new value picks
    # up on next Lambda cold start.
    CODE_MAP_TARGET_SHARD_TOKENS = tostring(var.code_map_target_shard_tokens)
    CODE_MAP_MAX_PROMPT_TOKENS   = tostring(var.code_map_max_prompt_tokens)
    },
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

    # plan-git-truth chunk 01: SHA-truth ledger (pending-work + failed-files).
    sha_truth = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
      ]
      resources = local.sha_truth_table_arns
    }

    # plan-git-truth chunk 02: try_advance_sha writes to the workspaces row
    # (last_{pipeline}_sha + last_{pipeline}_sha_ts). Live discovery on
    # 2026-05-16: AccessDeniedException blocked the SHA advance even though
    # both ledger stages closed correctly. github-sync already had this
    # via its own dynamodb policy block; process-batch-summary needs it
    # added separately.
    workspaces_sha_advance = {
      effect = "Allow"
      actions = [
        "dynamodb:UpdateItem",
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
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
    bedrock = {
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

    # SQS permissions for event source mapping
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_maps_queue.queue_arn
      ]
    }

    # Chunk 05: merge-message requeue-with-delay writes back to the
    # same code-maps queue via sqs:SendMessage. Merge messages that
    # arrive before all shard summaries have landed get re-enqueued
    # with DelaySeconds so SQS re-delivers when the shards are ready.
    sqs_send_merge_requeue = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
      ]
      resources = [
        module.code_maps_queue.queue_arn
      ]
    }

    # S3 Vectors write permissions for storing embeddings
    s3_vectors_write = {
      effect = "Allow"
      actions = [
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
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

    # chunk 08a: read the S3 mirror written by azure-devops-sync so
    # the provider=azure_devops branch of fetch_file_content can
    # serve content without a live ADO API call. Scoped to
    # workspaces/* because that prefix is the only legitimate read
    # target. Both process-batch-summary and process-repo-summary
    # get this grant — process-batch-summary uses it today;
    # process-repo-summary will use it when .outcomeops.yaml lookup
    # learns to read from the S3 mirror for non-github providers.
    s3_kb_read = {
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"]
    }

    # Read workspace METADATA rows for privacy_mode lookups (PII redaction)
    workspaces_read = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn
      ]
    }

    # Comprehend Detect-PII for ingest-time PII redaction (plan/ chunk 07).
    # AWS does not support resource-level scoping for this action; "*" is
    # the only valid value per the Comprehend service auth documentation.
    comprehend_pii = {
      effect = "Allow"
      actions = [
        "comprehend:DetectPiiEntities"
      ]
      resources = ["*"]
    }
    },
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "process-batch-summary"
  }
}

# Event source mapping - Connect SQS queue to Lambda
resource "aws_lambda_event_source_mapping" "code_maps_queue_to_lambda" {
  event_source_arn = module.code_maps_queue.queue_arn
  function_name    = module.process_batch_summary_lambda.lambda_function_name
  batch_size       = 1 # Process one message at a time (each message is already a batch)
  enabled          = true

  # Function response types for partial batch failures
  function_response_types = ["ReportBatchItemFailures"]

  # Allow higher concurrency for faster processing
  # Each code unit batch uses unique MessageGroupId, so they can process in parallel
  scaling_config {
    maximum_concurrency = 20 # Process up to 20 code unit batches in parallel
  }
}

output "process_batch_summary_lambda_arn" {
  description = "ARN of the process-batch-summary Lambda function"
  value       = module.process_batch_summary_lambda.lambda_function_arn
}

output "process_batch_summary_lambda_name" {
  description = "Name of the process-batch-summary Lambda function"
  value       = module.process_batch_summary_lambda.lambda_function_name
}

# ============================================================================
# Process Repository Summary Lambda
# ============================================================================

module "process_repo_summary_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-process-repo-summary"
  description   = "Process repository architectural summaries from SQS queue"
  handler       = "handler.handler"
  runtime       = "python3.12"
  # chunk 09 (HA alignment): bumped 300->600 and 1024->2048 to match
  # the secondary. Large repos were pushing the 5-minute wall + memory
  # ceiling; secondary values have been safe in prd and are the
  # correct-sized ones.
  timeout     = 600
  memory_size = 2048

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code (backends/ now lives under process-repo-summary) + shared module
  source_path = [
    {
      path             = "${path.module}/../lambda/process-repo-summary"
      pip_requirements = true
      patterns = [
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!.*\\.pyc",
      ]
    },
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Attach license validator layer (ADR-019)
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  # Environment variables
  environment_variables = merge(
    {
      ENV                                = var.environment
      APP_NAME                           = var.app_name
      AUDIT_TABLE                        = module.audit_logs_table.dynamodb_table_id
      WORKSPACES_TABLE                   = module.workspaces_table[0].dynamodb_table_id
      DELETE_QUEUE                       = aws_sqs_queue.delete_queue[0].url
      BEDROCK_ADVANCED_MODEL_ID          = var.bedrock_advanced_model_id
      BEDROCK_DEFAULT_BACKEND            = var.bedrock_default_backend
      BEDROCK_RESPONSES_REASONING_EFFORT = var.bedrock_responses_reasoning_effort
      BEDROCK_MANTLE_API_KEY_PARAM       = "/${var.environment}/${var.app_name}/bedrock/mantle-api-key"
      BEDROCK_EMBEDDING_MODEL_ID         = var.bedrock_embedding_model_id
      BEDROCK_ADVANCED_INPUT_PRICE       = tostring(var.bedrock_advanced_input_price)
      BEDROCK_ADVANCED_OUTPUT_PRICE      = tostring(var.bedrock_advanced_output_price)
      BEDROCK_BASIC_INPUT_PRICE          = tostring(var.bedrock_basic_input_price)
      BEDROCK_BASIC_OUTPUT_PRICE         = tostring(var.bedrock_basic_output_price)
      BEDROCK_EMBEDDING_INPUT_PRICE      = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE     = tostring(var.bedrock_embedding_output_price)
      PII_REDACTION_ENABLED              = tostring(var.pii_redaction_enabled)
      COMPREHEND_PII_PRICE_PER_1M_CHARS  = tostring(var.comprehend_pii_price_per_1m_chars)
      PII_REDACTION_SKIP_TYPES           = join(",", var.pii_redaction_skip_types)
      # plan-git-truth chunk 01: SHA-truth ledger tables. process-repo-summary
      # is the code_map dispatcher (fans the single repo-summaries message out
      # to N per-code-unit code-maps messages) and writes the per-unit ledger.
      # Wired but not yet imported; chunk 02 lights it up.
      PENDING_WORK_TABLE = module.code_map_pending_work_table.dynamodb_table_id
      FAILED_FILES_TABLE = module.code_map_failed_files_table.dynamodb_table_id
      # Chunk 06: same token-sizing knobs as process-batch-summary so
      # the upstream splitter and downstream consumer stay aligned on
      # "does this fit in one Bedrock call".
      CODE_MAP_TARGET_SHARD_TOKENS = tostring(var.code_map_target_shard_tokens)
      CODE_MAP_MAX_PROMPT_TOKENS   = tostring(var.code_map_max_prompt_tokens)
    },
    local.outcomeops_license_env,
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

    # plan-git-truth chunk 01: SHA-truth ledger (pending-work + failed-files).
    sha_truth = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
      ]
      resources = local.sha_truth_table_arns
    }

    # plan-git-truth chunk 02: try_advance_sha writes to the workspaces
    # row. process-repo-summary fires this from inside
    # _close_code_map_dispatch_ledger.
    workspaces_sha_advance = {
      effect = "Allow"
      actions = [
        "dynamodb:UpdateItem",
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
      ]
    }

    # Write to S3 knowledge base bucket
    s3_write = {
      effect = "Allow"
      actions = [
        "s3:PutObject",
        "s3:GetObject"
      ]
      resources = [
        "${module.knowledge_base_bucket.s3_bucket_arn}/*"
      ]
    }

    # Send messages to code maps queue + delete queue (for orphan cleanup)
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_maps_queue.queue_arn,
        aws_sqs_queue.delete_queue[0].arn
      ]
    }

    # Read SSM parameters (including encrypted GitHub token)
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
    bedrock = {
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

    # SQS permissions for event source mapping
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.repo_summaries_queue.queue_arn
      ]
    }

    # S3 Vectors write permissions for storing embeddings
    s3_vectors_write = {
      effect = "Allow"
      actions = [
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
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

    # chunk 08a: read the S3 mirror written by azure-devops-sync so
    # the provider=azure_devops branch of fetch_file_content can
    # serve content without a live ADO API call. Scoped to
    # workspaces/* because that prefix is the only legitimate read
    # target. Both process-batch-summary and process-repo-summary
    # get this grant — process-batch-summary uses it today;
    # process-repo-summary will use it when .outcomeops.yaml lookup
    # learns to read from the S3 mirror for non-github providers.
    s3_kb_read = {
      effect    = "Allow"
      actions   = ["s3:GetObject"]
      resources = ["${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"]
    }

    # Read workspace METADATA rows for privacy_mode lookups (PII redaction)
    workspaces_read = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem"
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn
      ]
    }

    # Comprehend Detect-PII for ingest-time PII redaction (plan/ chunk 07).
    # AWS does not support resource-level scoping for this action; "*" is
    # the only valid value per the Comprehend service auth documentation.
    comprehend_pii = {
      effect = "Allow"
      actions = [
        "comprehend:DetectPiiEntities"
      ]
      resources = ["*"]
    }
    },
    local.outcomeops_license_iam,
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "process-repo-summary"
  }
}

# Event source mapping - Connect SQS repo-summaries queue to Lambda
resource "aws_lambda_event_source_mapping" "repo_summaries_queue_to_lambda" {
  event_source_arn = module.repo_summaries_queue.queue_arn
  function_name    = module.process_repo_summary_lambda.lambda_function_name
  batch_size       = 1 # Process one repo at a time (natural rate limiting)
  enabled          = true

  # Function response types for partial batch failures
  function_response_types = ["ReportBatchItemFailures"]

  # Allow higher concurrency for faster processing
  # Each repo has unique MessageGroupId, so they can process in parallel
  scaling_config {
    maximum_concurrency = 20 # Process up to 20 repos in parallel
  }
}

output "process_repo_summary_lambda_arn" {
  description = "ARN of the process-repo-summary Lambda function"
  value       = module.process_repo_summary_lambda.lambda_function_arn
}

output "process_repo_summary_lambda_name" {
  description = "Name of the process-repo-summary Lambda function"
  value       = module.process_repo_summary_lambda.lambda_function_name
}
