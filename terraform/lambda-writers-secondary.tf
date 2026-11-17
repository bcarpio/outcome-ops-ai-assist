# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# Secondary-region writer Lambdas ()
# ----------------------------------------------------------------------------
# Mirror of the 5 ingest-pipeline Lambdas under aws.secondary:
#   file-ingestion       (SQS-triggered by file_ingestion_secondary)
#   process-documents    (S3-event-triggered by knowledge_base_bucket_secondary)
#   delete-worker        (SQS-triggered by delete_queue_secondary)
#   process-batch-summary (SQS-triggered by code_maps_queue_secondary)
#   process-repo-summary (SQS-triggered by repo_summaries_queue_secondary)
#
# All idle until secondary becomes active and SQS messages / S3 events
# land there.
#
# All modules gated on var.enable_workspaces && var.secondary_region != "".
# ============================================================================

# ----------------------------------------------------------------------------
# file-ingestion (secondary)
# ----------------------------------------------------------------------------

module "file_ingestion_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-file-ingestion"
  role_name     = "${var.environment}-${var.app_name}-file-ingestion-secondary"
  description   = "Central file ingestion (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/file-ingestion", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!requirements-dev\\.txt", "!.*\\.pyc", "!\\.pytest_cache/.*"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true


  environment_variables = merge(
    {
      ENV                  = var.environment
      APP_NAME             = var.app_name
      WORKSPACES_TABLE     = module.workspaces_table_secondary[0].dynamodb_table_id
      KB_BUCKET            = module.knowledge_base_bucket_secondary.s3_bucket_id
      OAUTH_KMS_KEY_ARN    = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      AUDIT_TABLE          = module.audit_logs_table_secondary[0].dynamodb_table_id
      AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
      PENDING_WORK_TABLE   = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE   = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
    },
    # HA parity with primary lambda-file-ingestion.tf: shared/oauth modules
    # read their env at module init on import, so servicenow_attachment /
    # salesforce_attachment processing on failover crashes if these are
    # missing here. Primary carries both; secondary was silently lacking
    # both until 2026-07-08 attachment-parity work surfaced it.
    local.servicenow_common_env,
    local.salesforce_common_env,
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      s3_kb = {
        effect    = "Allow"
        actions   = ["s3:PutObject", "s3:PutObjectTagging"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*"]
      }
      dynamodb = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      sha_truth = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        resources = local.sha_truth_table_arns_secondary
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/*"]
      }
      kms_oauth = {
        effect    = "Allow"
        actions   = ["kms:Encrypt", "kms:Decrypt", "kms:GenerateDataKey*"]
        resources = [aws_kms_replica_key.oauth_tokens_secondary[0].arn]
      }
      kms_ssm = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.file_ingestion_secondary[0].arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
    },
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "file-ingestion" }
}

resource "aws_lambda_event_source_mapping" "file_ingestion_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  event_source_arn = aws_sqs_queue.file_ingestion_secondary[0].arn
  function_name    = module.file_ingestion_lambda_secondary[0].lambda_function_name

  batch_size = 10

  # partial-batch-failure. See lambda-file-ingestion.tf primary
  # ESM for rationale (ingestion writes are idempotent; failed items must
  # redeliver + DLQ instead of silent 200/delete).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 10
  }
}

# ----------------------------------------------------------------------------
# process-documents (secondary) — S3-event-triggered by KB bucket secondary
# ----------------------------------------------------------------------------

module "process_documents_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-process-documents"
  role_name     = "${var.environment}-${var.app_name}-process-documents-secondary"
  description   = "Document embedding and vector storage (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 1024

  source_path = [
    { path = "${path.module}/../lambda/process-documents", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true


  environment_variables = merge(
    {
      ENV                               = var.environment
      APP_NAME                          = var.app_name
      KB_BUCKET                         = module.knowledge_base_bucket_secondary.s3_bucket_id
      AUDIT_TABLE                       = module.audit_logs_table_secondary[0].dynamodb_table_id
      AUDIT_RETENTION_DAYS              = tostring(var.audit_retention_days)
      WORKSPACES_TABLE                  = module.workspaces_table_secondary[0].dynamodb_table_id
      BEDROCK_EMBEDDING_MODEL_ID        = var.bedrock_embedding_model_id
      BEDROCK_EMBEDDING_INPUT_PRICE     = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE    = tostring(var.bedrock_embedding_output_price)
      PII_REDACTION_ENABLED             = tostring(var.pii_redaction_enabled)
      COMPREHEND_PII_PRICE_PER_1M_CHARS = tostring(var.comprehend_pii_price_per_1m_chars)
      PII_REDACTION_SKIP_TYPES          = join(",", var.pii_redaction_skip_types)
      PENDING_WORK_TABLE                = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE                = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
    },
    local.ha_writer_env_secondary,
    # ADR-016 v1.2 -- chat workload (embed), secondary parity.
    local.bedrock_chat_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      s3_read = {
        effect    = "Allow"
        actions   = ["s3:GetObject"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*"]
      }
      sha_truth = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        resources = local.sha_truth_table_arns_secondary
      }
      workspaces_sha_advance = {
        effect    = "Allow"
        actions   = ["dynamodb:UpdateItem"]
        resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/s3vectors/*"]
      }
      bedrock = {
        effect  = "Allow"
        actions = ["bedrock:InvokeModel"]
        # ADR-016 section 5: application inference profiles require BOTH
        # the profile ARN and the underlying foundation-model ARN. Matches
        # primary lambda-file-ingestion.tf; HA parity.
        resources = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      }
      s3vectors = {
        effect    = "Allow"
        actions   = ["s3vectors:PutVectors", "s3vectors:DeleteVectors"]
        resources = ["arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"]
      }
      textract_ocr = {
        effect    = "Allow"
        actions   = ["textract:StartDocumentTextDetection", "textract:GetDocumentTextDetection"]
        resources = ["*"]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
      workspaces_read = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem"]
        resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
      }
      comprehend_pii = {
        effect    = "Allow"
        actions   = ["comprehend:DetectPiiEntities"]
        resources = ["*"]
      }
    },
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "process-documents" }
}

# S3 event notification: knowledge_base_bucket_secondary triggers
# process-documents_secondary on PutObject events.
resource "aws_lambda_permission" "process_documents_s3_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = module.process_documents_lambda_secondary[0].lambda_function_name
  principal     = "s3.amazonaws.com"
  source_arn    = module.knowledge_base_bucket_secondary.s3_bucket_arn
}

resource "aws_s3_bucket_notification" "knowledge_base_notification_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  bucket = module.knowledge_base_bucket_secondary.s3_bucket_id

  lambda_function {
    lambda_function_arn = module.process_documents_lambda_secondary[0].lambda_function_arn
    events              = ["s3:ObjectCreated:*"]
  }

  depends_on = [aws_lambda_permission.process_documents_s3_secondary]
}

# ----------------------------------------------------------------------------
# delete-worker (secondary) — SQS-triggered by delete_queue_secondary
# ----------------------------------------------------------------------------

module "delete_worker_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-delete-worker"
  role_name     = "${var.environment}-${var.app_name}-delete-worker-secondary"
  description   = "S3 + S3 Vectors delete worker (secondary region)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  source_path = [
    { path = "${path.module}/../lambda/delete-worker", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV       = var.environment
      APP_NAME  = var.app_name
      KB_BUCKET = module.knowledge_base_bucket_secondary.s3_bucket_id
      # delete_symbol_graph cleans every per-repo store in the secondary
      # region: symbol-graph facts + pointer, code-maps ingest state rows,
      # and pending-work fan-out counters (delete-bug 02).
      SYMBOL_GRAPH_TABLE = module.symbol_graph_table_secondary[0].dynamodb_table_id
      CODE_MAPS_TABLE    = module.code_maps_table_secondary[0].dynamodb_table_id
      PENDING_WORK_TABLE = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
    },
    local.ha_writer_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_symbol_graph = {
        effect  = "Allow"
        actions = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:DeleteItem", "dynamodb:BatchWriteItem"]
        resources = [
          module.symbol_graph_table_secondary[0].dynamodb_table_arn,
          module.code_maps_table_secondary[0].dynamodb_table_arn,
          module.code_map_pending_work_table_secondary[0].dynamodb_table_arn,
        ]
      }
      s3_kb = {
        effect  = "Allow"
        actions = ["s3:GetObject", "s3:DeleteObject", "s3:DeleteObjects", "s3:ListBucket"]
        resources = [
          module.knowledge_base_bucket_secondary.s3_bucket_arn,
          "${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/s3vectors/*"]
      }
      s3vectors = {
        effect    = "Allow"
        actions   = ["s3vectors:ListVectors", "s3vectors:DeleteVectors"]
        resources = ["arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [aws_sqs_queue.delete_queue_secondary[0].arn]
      }
    },
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "delete-worker" }
}

resource "aws_lambda_event_source_mapping" "delete_worker_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  event_source_arn = aws_sqs_queue.delete_queue_secondary[0].arn
  function_name    = module.delete_worker_lambda_secondary[0].lambda_function_name

  batch_size                         = 10
  maximum_batching_window_in_seconds = 5 # (HA parity): mirror primary

  # Per-record partial-batch failures so a failed delete retries / DLQs
  # instead of being silently dropped (delete-bug 4b).
  function_response_types = ["ReportBatchItemFailures"]

  # (HA parity): mirror primary maximum_concurrency = 5.
  scaling_config {
    maximum_concurrency = 5
  }
}

# ----------------------------------------------------------------------------
# process-batch-summary (secondary) — SQS-triggered by code_maps_queue secondary
# ----------------------------------------------------------------------------

module "process_batch_summary_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-process-batch-summary"
  role_name     = "${var.environment}-${var.app_name}-process-batch-summary-secondary"
  description   = "Per-batch architectural summary + symbol-graph regen (secondary region)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 900
  memory_size   = 2048

  source_path = [
    { path = "${path.module}/../lambda/process-batch-summary", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true

  environment_variables = merge(
    {
      ENV             = var.environment
      APP_NAME        = var.app_name
      GITHUB_BASE_URL = var.github_base_url
      # (HA cleanup): EVENT_BUS_NAME removed (dead env). See
      # primary lambda-knowledge-base.tf.
      ENABLE_BEDROCK_COOLDOWN            = tostring(var.enable_bedrock_cooldown)
      AUDIT_TABLE                        = module.audit_logs_table_secondary[0].dynamodb_table_id
      AUDIT_RETENTION_DAYS               = tostring(var.audit_retention_days)
      WORKSPACES_TABLE                   = module.workspaces_table_secondary[0].dynamodb_table_id
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
      SYMBOL_GRAPH_TABLE                 = module.symbol_graph_table_secondary[0].dynamodb_table_id
      CODE_MAPS_TABLE                    = module.code_maps_table_secondary[0].dynamodb_table_id
      PENDING_WORK_TABLE                 = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE                 = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
      # Splitter-pipeline mirror (chunks 02-07 + tuning). CODE_MAPS_QUEUE_URL
      # is FUNCTIONAL, not tuning: without it the merge fan-in cannot
      # requeue-with-delay ("CODE_MAPS_QUEUE_URL not configured") and every
      # sharded merge gives up on its first attempt post-failover.
      CODE_MAPS_QUEUE_URL               = module.code_maps_queue_secondary.queue_url
      BATCH_SUMMARY_MSG_COOLDOWN_MS     = tostring(var.batch_summary_msg_cooldown_ms)
      BATCH_SUMMARY_MERGE_ATTEMPTS      = tostring(var.batch_summary_merge_attempts)
      BATCH_SUMMARY_MERGE_DELAY_SECONDS = tostring(var.batch_summary_merge_delay_s)
      CODE_MAP_TARGET_SHARD_TOKENS      = tostring(var.code_map_target_shard_tokens)
      CODE_MAP_MAX_PROMPT_TOKENS        = tostring(var.code_map_max_prompt_tokens)
      # (HA parity, mirror): fetch_file_content(provider=
      # azure_devops) reads from the S3 mirror at workspaces/{ws}/
      # azure_devops/{slug}/{path} that azure-devops-sync wrote in this
      # region. Without KB_BUCKET + s3_kb_read below, ADO content fetch
      # fails in the secondary post-failover.
      KB_BUCKET = module.knowledge_base_bucket_secondary.s3_bucket_id
    },
    local.ha_writer_env_secondary,
    # ADR-016 v1.2 -- code-maps workload (batch summaries), secondary parity.
    local.bedrock_code_maps_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_write = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query"]
        resources = [module.code_maps_table_secondary[0].dynamodb_table_arn]
      }
      sha_truth = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        resources = local.sha_truth_table_arns_secondary
      }
      # (HA parity, mirror): read the ADO S3 mirror
      # so fetch_file_content(provider=azure_devops) resolves without a
      # live ADO API call. Scoped to workspaces/* per primary.
      s3_kb_read = {
        effect    = "Allow"
        actions   = ["s3:GetObject"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*"]
      }
      # Mirrors primary's workspaces_read. read_workspace_mode() in the
      # PII path GetItems the workspace row to look up privacy_mode; this
      # grant was missing on the secondary and surfaced as a noisy
      # AccessDeniedException warning under failover (2026-06-01).
      # ADR-056: Scan mirrors the primary -- is_global + search_allowed_workspaces
      # scope enumeration for cross-repo resolution.
      workspaces_read = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:Query", "dynamodb:Scan"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      workspaces_sha_advance = {
        effect    = "Allow"
        actions   = ["dynamodb:UpdateItem"]
        resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
      }
      symbol_graph_write = {
        effect  = "Allow"
        actions = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:Query", "dynamodb:BatchWriteItem", "dynamodb:DeleteItem"]
        resources = [
          module.symbol_graph_table_secondary[0].dynamodb_table_arn,
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-kind-index",
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-prefix-index",
          "${module.symbol_graph_table_secondary[0].dynamodb_table_arn}/index/target-name-index",
        ]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
      }
      bedrock = {
        effect  = "Allow"
        actions = ["bedrock:InvokeModel"]
        resources = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      }
      s3_vectors_write = {
        effect = "Allow"
        # (HA parity): mirror primary. DeleteVectors was missing
        # -- orphan-vector cleanup broken post-failover. Also widened
        # resources to include both the bucket ARN and bucket/* per
        # primary, because bucket-level actions need the naked bucket ARN.
        # GetVectors is FUNCTIONAL parity: the merge fan-in fetches shard
        # summaries by key before unifying them.
        actions = ["s3vectors:PutVectors", "s3vectors:DeleteVectors", "s3vectors:GetVectors"]
        resources = [
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*",
        ]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [module.code_maps_queue_secondary.queue_arn]
      }
      # Merge fan-in requeue-with-delay sends the merge message back to
      # the same queue it consumes (primary parity: sqs_send_merge_requeue).
      sqs_send_merge_requeue = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [module.code_maps_queue_secondary.queue_arn]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
      comprehend_pii = {
        effect    = "Allow"
        actions   = ["comprehend:DetectPiiEntities"]
        resources = ["*"]
      }
      kms_decrypt = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      aws_marketplace = {
        effect    = "Allow"
        actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
        resources = ["*"]
      }
    },
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "process-batch-summary" }
}

resource "aws_lambda_event_source_mapping" "code_maps_queue_to_lambda_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  event_source_arn        = module.code_maps_queue_secondary.queue_arn
  function_name           = module.process_batch_summary_lambda_secondary[0].lambda_function_name
  batch_size              = 1
  enabled                 = true
  function_response_types = ["ReportBatchItemFailures"]

  # (HA parity): mirror primary maximum_concurrency = 20 so a
  # post-failover backlog doesn't drown the region with unlimited fan-out
  # (Bedrock throttling + DDB write-capacity blowout).
  scaling_config {
    maximum_concurrency = 20
  }
}

# ----------------------------------------------------------------------------
# process-repo-summary (secondary) — SQS-triggered by repo_summaries_queue secondary
# ----------------------------------------------------------------------------

module "process_repo_summary_lambda_secondary" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  providers = {
    aws = aws.secondary
  }

  function_name = "${var.environment}-${var.app_name}-process-repo-summary"
  role_name     = "${var.environment}-${var.app_name}-process-repo-summary-secondary"
  description   = "Per-repo architectural summary + code-unit fan-out (secondary region)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 600
  memory_size   = 2048

  source_path = [
    { path = "${path.module}/../lambda/process-repo-summary", pip_requirements = true, patterns = ["!tests/.*", "!__pycache__/.*", "!\\.venv/.*", "!.*\\.pyc"] },
    { path = "${path.module}/../lambda/shared", prefix_in_zip = "shared" },
  ]

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn
  quiet_archive_local_exec          = true


  environment_variables = merge(
    {
      ENV                                = var.environment
      APP_NAME                           = var.app_name
      GITHUB_BASE_URL                    = var.github_base_url
      AUDIT_TABLE                        = module.audit_logs_table_secondary[0].dynamodb_table_id
      AUDIT_RETENTION_DAYS               = tostring(var.audit_retention_days)
      WORKSPACES_TABLE                   = module.workspaces_table_secondary[0].dynamodb_table_id
      DELETE_QUEUE                       = aws_sqs_queue.delete_queue_secondary[0].url
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
      PENDING_WORK_TABLE                 = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      # Splitter sizing mirror -- shard-count decisions must match the
      # primary or expected-key sets drift across a failover boundary.
      CODE_MAP_TARGET_SHARD_TOKENS = tostring(var.code_map_target_shard_tokens)
      CODE_MAP_MAX_PROMPT_TOKENS   = tostring(var.code_map_max_prompt_tokens)
      FAILED_FILES_TABLE           = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
      # (HA cleanup): CODE_MAPS_QUEUE_URL and KB_BUCKET removed.
      # Handler discovers both via SSM (/env/app/sqs/code-maps-queue-url and
      # /env/app/s3/knowledge-base-bucket) -- these envs had no consumer
      # and existed only on the secondary. Primary side never had them.
    },
    local.ha_writer_env_secondary,
    # ADR-016 v1.2 -- code-maps workload (repo summaries), secondary parity.
    local.bedrock_code_maps_env_secondary,
  )

  attach_policy_statements = true
  policy_statements = merge(
    {
      dynamodb_write = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem", "dynamodb:UpdateItem", "dynamodb:GetItem", "dynamodb:Query"]
        resources = [module.code_maps_table_secondary[0].dynamodb_table_arn]
      }
      sha_truth = {
        effect    = "Allow"
        actions   = ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem"]
        resources = local.sha_truth_table_arns_secondary
      }
      workspaces_read = {
        effect  = "Allow"
        actions = ["dynamodb:GetItem", "dynamodb:Query"]
        resources = [
          module.workspaces_table_secondary[0].dynamodb_table_arn,
          "${module.workspaces_table_secondary[0].dynamodb_table_arn}/index/*",
        ]
      }
      workspaces_sha_advance = {
        effect    = "Allow"
        actions   = ["dynamodb:UpdateItem"]
        resources = [module.workspaces_table_secondary[0].dynamodb_table_arn]
      }
      ssm_read = {
        effect    = "Allow"
        actions   = ["ssm:GetParameter", "ssm:GetParameters"]
        resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"]
      }
      bedrock = {
        effect  = "Allow"
        actions = ["bedrock:InvokeModel"]
        resources = [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
          "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
        ]
      }
      s3_write = {
        effect    = "Allow"
        actions   = ["s3:PutObject", "s3:GetObject"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/*"]
      }
      s3_kb_read = {
        effect    = "Allow"
        actions   = ["s3:GetObject"]
        resources = ["${module.knowledge_base_bucket_secondary.s3_bucket_arn}/workspaces/*"]
      }
      s3_vectors_write = {
        effect = "Allow"
        # (HA parity): mirror primary. DeleteVectors was missing
        # -- orphan-vector cleanup broken post-failover. Also widened
        # resources to include the naked bucket ARN.
        actions = ["s3vectors:PutVectors", "s3vectors:DeleteVectors"]
        resources = [
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors",
          "arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*",
        ]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [module.repo_summaries_queue_secondary.queue_arn]
      }
      sqs_send = {
        effect  = "Allow"
        actions = ["sqs:SendMessage", "sqs:GetQueueAttributes"]
        resources = [
          module.code_maps_queue_secondary.queue_arn,
          aws_sqs_queue.delete_queue_secondary[0].arn,
        ]
      }
      dynamodb_audit = {
        effect    = "Allow"
        actions   = ["dynamodb:PutItem"]
        resources = [module.audit_logs_table_secondary[0].dynamodb_table_arn]
      }
      comprehend_pii = {
        effect    = "Allow"
        actions   = ["comprehend:DetectPiiEntities"]
        resources = ["*"]
      }
      kms_decrypt = {
        effect    = "Allow"
        actions   = ["kms:Decrypt"]
        resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
      }
      aws_marketplace = {
        effect    = "Allow"
        actions   = ["aws-marketplace:ViewSubscriptions", "aws-marketplace:Subscribe"]
        resources = ["*"]
      }
    },
    local.ha_writer_events_iam_secondary,
  )

  tags = { Purpose = "process-repo-summary" }
}

resource "aws_lambda_event_source_mapping" "repo_summaries_queue_to_lambda_secondary" {
  count = var.enable_workspaces && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  event_source_arn        = module.repo_summaries_queue_secondary.queue_arn
  function_name           = module.process_repo_summary_lambda_secondary[0].lambda_function_name
  batch_size              = 1
  enabled                 = true
  function_response_types = ["ReportBatchItemFailures"]

  # (HA parity): mirror primary maximum_concurrency = 20.
  scaling_config {
    maximum_concurrency = 20
  }
}
