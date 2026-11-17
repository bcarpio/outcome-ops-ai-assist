# ============================================================================
# File Ingestion Pipeline
# Central pipeline for processing documents from all sources
# ============================================================================

# ============================================================================
# SQS Queues
# ============================================================================

# File Ingestion Queue (FIFO for ordering and deduplication)
resource "aws_sqs_queue" "file_ingestion" {
  count = var.enable_workspaces ? 1 : 0

  name                        = "${var.environment}-${var.app_name}-file-ingestion.fifo"
  fifo_queue                  = true
  content_based_deduplication = false
  deduplication_scope         = "messageGroup"
  fifo_throughput_limit       = "perMessageGroupId"

  visibility_timeout_seconds = 300   # 5 minutes
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.file_ingestion_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "file-ingestion-queue"
  }
}

resource "aws_sqs_queue" "file_ingestion_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-file-ingestion-dlq.fifo"
  fifo_queue                = true
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "file-ingestion-dlq"
  }
}

# Delete Queue (standard queue for delete operations)
resource "aws_sqs_queue" "delete_queue" {
  count = var.enable_workspaces ? 1 : 0

  name                       = "${var.environment}-${var.app_name}-delete"
  visibility_timeout_seconds = 300   # 5 minutes
  message_retention_seconds  = 86400 # 1 day
  receive_wait_time_seconds  = 20    # Long polling

  # Dead letter queue
  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.delete_dlq[0].arn
    maxReceiveCount     = 3
  })

  # Encryption at rest
  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "delete-queue"
  }
}

resource "aws_sqs_queue" "delete_dlq" {
  count = var.enable_workspaces ? 1 : 0

  name                      = "${var.environment}-${var.app_name}-delete-dlq"
  message_retention_seconds = 1209600 # 14 days

  sqs_managed_sse_enabled = var.enable_cmk_encryption ? null : true
  kms_master_key_id       = local.data_cmk_arn

  tags = {
    Purpose = "delete-dlq"
  }
}

# ============================================================================
# File Ingestion Lambda
# Downloads content from sources and uploads to S3
# ============================================================================

module "file_ingestion_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-file-ingestion"
  description   = "Central file ingestion for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes
  memory_size   = 512

  # Source code with pip requirements; bundle shared/ for audit_writer
  source_path = [
    {
      path             = "${path.module}/../lambda/file-ingestion"
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

  # ADR-019: outcomeops_license layer for fail-closed license validation
  # plus per-source repo-limit enforcement at the upstream ingest boundary.
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV               = var.environment
      APP_NAME          = var.app_name
      WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
      KB_BUCKET         = module.knowledge_base_bucket.s3_bucket_id
      OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn
      # AUDIT_TABLE for log_license_fail_open() regression detector (ADR-019)
      AUDIT_TABLE          = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS = tostring(var.audit_retention_days)
      # plan-git-truth chunk 01: SHA-truth ledger tables. Wired but not
      # yet imported; chunk 02 lights up the docs-pipeline consumer.
      PENDING_WORK_TABLE = module.code_map_pending_work_table.dynamodb_table_id
      FAILED_FILES_TABLE = module.code_map_failed_files_table.dynamodb_table_id
    },
    # ServiceNow OAuth env: needed here because process_servicenow_article
    # and process_servicenow_attachment call refresh_access_token via the
    # shared/oauth/servicenow module, which reads its instance URL +
    # client_id + client-secret SSM path from env at module init.
    local.servicenow_common_env,
    # Salesforce OAuth env: process_salesforce_record doesn't currently
    # call refresh_access_token (sync worker embeds pre-rendered markdown
    # in the SQS body), but shared/oauth/salesforce reads its env at
    # module init on every import. Thread the vars through so a future
    # switch to fetch-back-per-record doesn't crash on missing config.
    local.salesforce_common_env,
    local.outcomeops_license_env,
    local.ha_writer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge(
    {
      # S3 for knowledge base uploads
      s3_kb = {
        effect = "Allow"
        actions = [
          "s3:PutObject",
          "s3:PutObjectTagging"
        ]
        resources = [
          "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
        ]
      }

      # DynamoDB for connection lookup and token updates
      dynamodb = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
          "dynamodb:Query"
        ]
        resources = [
          module.workspaces_table[0].dynamodb_table_arn,
          "${module.workspaces_table[0].dynamodb_table_arn}/index/*"
        ]
      }

      # plan-git-truth chunk 01: SHA-truth ledger.
      sha_truth = {
        effect = "Allow"
        actions = [
          "dynamodb:GetItem",
          "dynamodb:PutItem",
          "dynamodb:UpdateItem",
        ]
        resources = local.sha_truth_table_arns
      }

      # SSM for OAuth configuration
      ssm_read = {
        effect = "Allow"
        actions = [
          "ssm:GetParameter",
          "ssm:GetParameters"
        ]
        resources = [
          "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/oauth/*"
        ]
      }

      # KMS for token encryption/decryption (rolling refresh tokens)
      kms_oauth = {
        effect = "Allow"
        actions = [
          "kms:Encrypt",
          "kms:Decrypt",
          "kms:GenerateDataKey*"
        ]
        resources = local.oauth_kms_token_key_arns
      }

      # KMS for SSM parameter decryption
      kms_ssm = {
        effect = "Allow"
        actions = [
          "kms:Decrypt"
        ]
        resources = [
          "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
        ]
      }

      # SQS for receiving messages
      sqs_receive = {
        effect = "Allow"
        actions = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        resources = [
          aws_sqs_queue.file_ingestion[0].arn
        ]
      }

      # DynamoDB audit logging for LICENSE_FAIL_OPEN regression-detector rows
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
    local.outcomeops_license_iam,
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "file-ingestion"
  }
}

# SQS trigger for file ingestion Lambda
resource "aws_lambda_event_source_mapping" "file_ingestion" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.file_ingestion[0].arn
  function_name    = module.file_ingestion_lambda[0].lambda_function_name

  batch_size = 10 # Process multiple files concurrently

  # Chunk 02: partial-batch-failure. Ingestion writes are idempotent
  # (S3 put + DDB upsert on stable key), so redelivery of failed items
  # is safe. Without this the batch returns 200 on any failure and every
  # message is deleted -- permanent KB gap.
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 10 # Limit concurrent invocations
  }
}

# ============================================================================
# Process Documents Lambda
# Triggered by S3 events, generates embeddings
# ============================================================================

module "process_documents_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-process-documents"
  description   = "Document embedding and vector storage for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes
  memory_size   = 1024

  # Source code with pip requirements (includes shared module)
  source_path = [
    {
      path             = "${path.module}/../lambda/process-documents"
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

  # ADR-019: outcomeops_license layer for fail-closed license validation
  # plus track_repo_active() at the completion (vector-write) boundary.
  layers = [
    var.outcomeops_license_layer_arn,
  ]

  environment_variables = merge(
    {
      ENV                               = var.environment
      APP_NAME                          = var.app_name
      KB_BUCKET                         = module.knowledge_base_bucket.s3_bucket_id
      AUDIT_TABLE                       = module.audit_logs_table.dynamodb_table_id
      AUDIT_RETENTION_DAYS              = tostring(var.audit_retention_days)
      WORKSPACES_TABLE                  = module.workspaces_table[0].dynamodb_table_id
      BEDROCK_EMBEDDING_MODEL_ID        = var.bedrock_embedding_model_id
      BEDROCK_EMBEDDING_INPUT_PRICE     = tostring(var.bedrock_embedding_input_price)
      BEDROCK_EMBEDDING_OUTPUT_PRICE    = tostring(var.bedrock_embedding_output_price)
      PII_REDACTION_ENABLED             = tostring(var.pii_redaction_enabled)
      COMPREHEND_PII_PRICE_PER_1M_CHARS = tostring(var.comprehend_pii_price_per_1m_chars)
      PII_REDACTION_SKIP_TYPES          = join(",", var.pii_redaction_skip_types)
      # plan-git-truth chunk 02b/4: process-documents closes the docs
      # ledger after vectorization. _close_docs_unit reads these tables.
      PENDING_WORK_TABLE = module.code_map_pending_work_table.dynamodb_table_id
      FAILED_FILES_TABLE = module.code_map_failed_files_table.dynamodb_table_id
    },
    local.outcomeops_license_env,
    local.ha_writer_env,
    # ADR-016 v1.2 -- process-documents (called via file-ingestion) embeds
    # every ingested doc via Titan. Attribute under chat workload since
    # every retrieval consumer (chat, ask-claude, query-kb, mcp-server)
    # reads the resulting vectors.
    local.bedrock_chat_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # S3 for reading documents
    s3_read = {
      effect = "Allow"
      actions = [
        "s3:GetObject"
      ]
      resources = [
        "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
      ]
    }

    # plan-git-truth chunk 02b/4: SHA-truth ledger (pending-work +
    # failed-files) + workspaces-row advance.
    sha_truth = {
      effect = "Allow"
      actions = [
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
      ]
      resources = local.sha_truth_table_arns
    }

    workspaces_sha_advance = {
      effect = "Allow"
      actions = [
        "dynamodb:UpdateItem",
      ]
      resources = [
        module.workspaces_table[0].dynamodb_table_arn,
      ]
    }

    # SSM for vector config
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/s3vectors/*"
      ]
    }

    # Bedrock for embeddings (all foundation models for model portability)
    bedrock = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      # foundation-model grant is required regardless (ADR-016 section 5:
      # the underlying model must be grantable even when the profile ARN
      # is passed as modelId). inference-profile/* covers application
      # inference profiles created in bedrock-inference-profiles.tf so
      # process-documents can attribute Titan-embed spend under the
      # chat workload once chunk 03/04 land.
      resources = [
        "arn:aws:bedrock:*::foundation-model/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/*",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:application-inference-profile/*",
      ]
    }

    # S3 Vectors for storing embeddings
    s3vectors = {
      effect = "Allow"
      actions = [
        "s3vectors:PutVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }

    # Textract OCR for scanned PDFs (fallback when PyPDF2 finds no text)
    textract_ocr = {
      effect = "Allow"
      actions = [
        "textract:StartDocumentTextDetection",
        "textract:GetDocumentTextDetection"
      ]
      resources = ["*"] # Textract does not support resource-level permissions
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
    Purpose = "process-documents"
  }
}

# S3 event notification for process-documents Lambda
resource "aws_lambda_permission" "process_documents_s3" {
  count = var.enable_workspaces ? 1 : 0

  statement_id  = "AllowS3Invoke"
  action        = "lambda:InvokeFunction"
  function_name = module.process_documents_lambda[0].lambda_function_name
  principal     = "s3.amazonaws.com"
  source_arn    = module.knowledge_base_bucket.s3_bucket_arn
}

resource "aws_s3_bucket_notification" "knowledge_base_notification" {
  count = var.enable_workspaces ? 1 : 0

  bucket = module.knowledge_base_bucket.s3_bucket_id

  lambda_function {
    lambda_function_arn = module.process_documents_lambda[0].lambda_function_arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "workspaces/"
  }

  depends_on = [aws_lambda_permission.process_documents_s3]
}

# ============================================================================
# Delete Worker Lambda
# Handles document and vector deletion
# ============================================================================

module "delete_worker_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  count = var.enable_workspaces ? 1 : 0

  function_name = "${var.environment}-${var.app_name}-delete-worker"
  description   = "Document deletion and orphan cleanup for OutcomeOps AI Assist"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes
  memory_size   = 512

  # Source code with pip requirements
  source_path = [
    {
      path             = "${path.module}/../lambda/delete-worker"
      pip_requirements = true
      patterns = [
        "!tests/.*",
        "!__pycache__/.*",
        "!\\.venv/.*",
        "!requirements-dev\\.txt",
        "!.*\\.pyc",
        "!\\.pytest_cache/.*",
      ]
    }
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  environment_variables = merge(
    {
      ENV       = var.environment
      APP_NAME  = var.app_name
      KB_BUCKET = module.knowledge_base_bucket.s3_bucket_id
      # delete_symbol_graph cleans every per-repo store the SHA-truth ledger
      # writes: symbol-graph facts + pointer, code-maps ingest state rows,
      # and pending-work fan-out counters.
      SYMBOL_GRAPH_TABLE = module.symbol_graph_table.dynamodb_table_id
      CODE_MAPS_TABLE    = module.code_maps_table.dynamodb_table_id
      PENDING_WORK_TABLE = module.code_map_pending_work_table.dynamodb_table_id
    },
    local.ha_writer_env,
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = merge({
    # Symbol-graph + code-maps + pending-work cleanup on repo removal.
    dynamodb_symbol_graph = {
      effect = "Allow"
      actions = [
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:DeleteItem",
        "dynamodb:BatchWriteItem",
      ]
      resources = [
        module.symbol_graph_table.dynamodb_table_arn,
        module.code_maps_table.dynamodb_table_arn,
        module.code_map_pending_work_table.dynamodb_table_arn,
      ]
    }

    # S3 for listing and deleting documents
    s3_kb = {
      effect = "Allow"
      actions = [
        "s3:GetObject",
        "s3:DeleteObject",
        "s3:DeleteObjects",
        "s3:ListBucket"
      ]
      resources = [
        module.knowledge_base_bucket.s3_bucket_arn,
        "${module.knowledge_base_bucket.s3_bucket_arn}/workspaces/*"
      ]
    }

    # SSM for vector config
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/s3vectors/*"
      ]
    }

    # S3 Vectors for listing and deleting embeddings
    s3vectors = {
      effect = "Allow"
      actions = [
        "s3vectors:ListVectors",
        "s3vectors:DeleteVectors"
      ]
      resources = [
        "arn:aws:s3vectors:${var.aws_region}:${data.aws_caller_identity.current.account_id}:bucket/${var.environment}-${var.app_name}-vectors/*"
      ]
    }

    # SQS for receiving messages
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        aws_sqs_queue.delete_queue[0].arn
      ]
    }
    },
    local.ha_writer_events_iam,
  )

  tags = {
    Purpose = "delete-worker"
  }
}

# SQS trigger for delete worker Lambda
resource "aws_lambda_event_source_mapping" "delete_worker" {
  count = var.enable_workspaces ? 1 : 0

  event_source_arn = aws_sqs_queue.delete_queue[0].arn
  function_name    = module.delete_worker_lambda[0].lambda_function_name

  batch_size                         = 10 # Process multiple deletes concurrently
  maximum_batching_window_in_seconds = 5  # Wait up to 5s to batch

  # Per-record partial-batch failures so a failed delete retries / DLQs
  # instead of being silently dropped (delete-bug 4b).
  function_response_types = ["ReportBatchItemFailures"]

  scaling_config {
    maximum_concurrency = 5 # Limit concurrent invocations
  }
}

# ============================================================================
# Outputs
# ============================================================================

output "file_ingestion_queue_url" {
  description = "URL of the file ingestion SQS queue"
  value       = var.enable_workspaces ? aws_sqs_queue.file_ingestion[0].url : null
}

output "file_ingestion_queue_arn" {
  description = "ARN of the file ingestion SQS queue"
  value       = var.enable_workspaces ? aws_sqs_queue.file_ingestion[0].arn : null
}

output "delete_queue_url" {
  description = "URL of the delete SQS queue"
  value       = var.enable_workspaces ? aws_sqs_queue.delete_queue[0].url : null
}

output "delete_queue_arn" {
  description = "ARN of the delete SQS queue"
  value       = var.enable_workspaces ? aws_sqs_queue.delete_queue[0].arn : null
}

output "file_ingestion_lambda_name" {
  description = "Name of the file ingestion Lambda function"
  value       = var.enable_workspaces ? module.file_ingestion_lambda[0].lambda_function_name : null
}

output "process_documents_lambda_name" {
  description = "Name of the process documents Lambda function"
  value       = var.enable_workspaces ? module.process_documents_lambda[0].lambda_function_name : null
}

output "delete_worker_lambda_name" {
  description = "Name of the delete worker Lambda function"
  value       = var.enable_workspaces ? module.delete_worker_lambda[0].lambda_function_name : null
}
