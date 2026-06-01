# ============================================================================
# Secondary-region writer Lambdas (HA chunk 9e)
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

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                = var.environment
      APP_NAME           = var.app_name
      WORKSPACES_TABLE   = module.workspaces_table_secondary[0].dynamodb_table_id
      KB_BUCKET          = module.knowledge_base_bucket_secondary.s3_bucket_id
      OAUTH_KMS_KEY_ARN  = aws_kms_replica_key.oauth_tokens_secondary[0].arn
      AUDIT_TABLE        = module.audit_logs_table_secondary[0].dynamodb_table_id
      PENDING_WORK_TABLE = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
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
    local.outcomeops_license_iam,
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

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                               = var.environment
      APP_NAME                          = var.app_name
      KB_BUCKET                         = module.knowledge_base_bucket_secondary.s3_bucket_id
      AUDIT_TABLE                       = module.audit_logs_table_secondary[0].dynamodb_table_id
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
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
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
        effect    = "Allow"
        actions   = ["bedrock:InvokeModel"]
        resources = ["arn:aws:bedrock:*::foundation-model/*"]
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
    local.outcomeops_license_iam,
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
      # delete_symbol_graph cleans the secondary region's facts + pointer
      # when repo-delete messages are dispatched here (delete-bug 02).
      SYMBOL_GRAPH_TABLE = module.symbol_graph_table_secondary[0].dynamodb_table_id
      CODE_MAPS_TABLE    = module.code_maps_table_secondary[0].dynamodb_table_id
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
  batch_size       = 10

  # Per-record partial-batch failures so a failed delete retries / DLQs
  # instead of being silently dropped (delete-bug 4b).
  function_response_types = ["ReportBatchItemFailures"]
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

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                               = var.environment
      APP_NAME                          = var.app_name
      EVENT_BUS_NAME                    = aws_cloudwatch_event_bus.replication_secondary[0].name
      ENABLE_BEDROCK_COOLDOWN           = tostring(var.enable_bedrock_cooldown)
      AUDIT_TABLE                       = module.audit_logs_table_secondary[0].dynamodb_table_id
      WORKSPACES_TABLE                  = module.workspaces_table_secondary[0].dynamodb_table_id
      BEDROCK_ADVANCED_MODEL_ID         = var.bedrock_advanced_model_id
      BEDROCK_EMBEDDING_MODEL_ID        = var.bedrock_embedding_model_id
      BEDROCK_ADVANCED_INPUT_PRICE      = tostring(var.bedrock_advanced_input_price)
      BEDROCK_ADVANCED_OUTPUT_PRICE     = tostring(var.bedrock_advanced_output_price)
      BEDROCK_BASIC_INPUT_PRICE         = tostring(var.bedrock_basic_input_price)
      BEDROCK_BASIC_OUTPUT_PRICE        = tostring(var.bedrock_basic_output_price)
      BEDROCK_EMBEDDING_INPUT_PRICE     = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE    = tostring(var.bedrock_embedding_output_price)
      PII_REDACTION_ENABLED             = tostring(var.pii_redaction_enabled)
      COMPREHEND_PII_PRICE_PER_1M_CHARS = tostring(var.comprehend_pii_price_per_1m_chars)
      PII_REDACTION_SKIP_TYPES          = join(",", var.pii_redaction_skip_types)
      SYMBOL_GRAPH_TABLE                = module.symbol_graph_table_secondary[0].dynamodb_table_id
      CODE_MAPS_TABLE                   = module.code_maps_table_secondary[0].dynamodb_table_id
      PENDING_WORK_TABLE                = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE                = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
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
        ]
      }
      s3vectors = {
        effect    = "Allow"
        actions   = ["s3vectors:PutVectors"]
        resources = ["arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"]
      }
      sqs_receive = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
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
    },
    local.outcomeops_license_iam,
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

  layers = [var.outcomeops_license_layer_arn_secondary]

  environment_variables = merge(
    {
      ENV                               = var.environment
      APP_NAME                          = var.app_name
      AUDIT_TABLE                       = module.audit_logs_table_secondary[0].dynamodb_table_id
      WORKSPACES_TABLE                  = module.workspaces_table_secondary[0].dynamodb_table_id
      DELETE_QUEUE                      = aws_sqs_queue.delete_queue_secondary[0].url
      BEDROCK_ADVANCED_MODEL_ID         = var.bedrock_advanced_model_id
      BEDROCK_EMBEDDING_MODEL_ID        = var.bedrock_embedding_model_id
      BEDROCK_ADVANCED_INPUT_PRICE      = tostring(var.bedrock_advanced_input_price)
      BEDROCK_ADVANCED_OUTPUT_PRICE     = tostring(var.bedrock_advanced_output_price)
      BEDROCK_BASIC_INPUT_PRICE         = tostring(var.bedrock_basic_input_price)
      BEDROCK_BASIC_OUTPUT_PRICE        = tostring(var.bedrock_basic_output_price)
      BEDROCK_EMBEDDING_INPUT_PRICE     = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE    = tostring(var.bedrock_embedding_output_price)
      PII_REDACTION_ENABLED             = tostring(var.pii_redaction_enabled)
      COMPREHEND_PII_PRICE_PER_1M_CHARS = tostring(var.comprehend_pii_price_per_1m_chars)
      PII_REDACTION_SKIP_TYPES          = join(",", var.pii_redaction_skip_types)
      PENDING_WORK_TABLE                = module.code_map_pending_work_table_secondary[0].dynamodb_table_id
      FAILED_FILES_TABLE                = module.code_map_failed_files_table_secondary[0].dynamodb_table_id
      CODE_MAPS_QUEUE_URL               = module.code_maps_queue_secondary.queue_url
      KB_BUCKET                         = module.knowledge_base_bucket_secondary.s3_bucket_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env_secondary,
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
        ]
      }
      s3_kb = {
        effect  = "Allow"
        actions = ["s3:GetObject", "s3:PutObject"]
        resources = [
          "${module.knowledge_base_bucket_secondary.s3_bucket_arn}/code-maps/*",
        ]
      }
      s3vectors = {
        effect    = "Allow"
        actions   = ["s3vectors:PutVectors"]
        resources = ["arn:aws:s3vectors:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"]
      }
      sqs_receive_repo = {
        effect    = "Allow"
        actions   = ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"]
        resources = [module.repo_summaries_queue_secondary.queue_arn]
      }
      sqs_send_code_maps = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [module.code_maps_queue_secondary.queue_arn]
      }
      sqs_send_delete = {
        effect    = "Allow"
        actions   = ["sqs:SendMessage"]
        resources = [aws_sqs_queue.delete_queue_secondary[0].arn]
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
    },
    local.outcomeops_license_iam,
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
}
