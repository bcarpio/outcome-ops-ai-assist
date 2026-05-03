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

  # Source code with pip requirements
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
    }
  ]

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  environment_variables = {
    ENV               = var.environment
    APP_NAME          = var.app_name
    WORKSPACES_TABLE  = module.workspaces_table[0].dynamodb_table_id
    KB_BUCKET         = module.knowledge_base_bucket.s3_bucket_id
    OAUTH_KMS_KEY_ARN = aws_kms_key.oauth_tokens[0].arn
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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
      resources = [
        aws_kms_key.oauth_tokens[0].arn
      ]
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
  }

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

  environment_variables = {
    ENV                            = var.environment
    APP_NAME                       = var.app_name
    KB_BUCKET                      = module.knowledge_base_bucket.s3_bucket_id
    AUDIT_TABLE                    = module.audit_logs_table.dynamodb_table_id
    BEDROCK_EMBEDDING_MODEL_ID     = var.bedrock_embedding_model_id
    BEDROCK_EMBEDDING_INPUT_PRICE  = tostring(var.bedrock_embedding_input_price)
    BEDROCK_EMBEDDING_OUTPUT_PRICE = tostring(var.bedrock_embedding_output_price)
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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
      resources = [
        "arn:aws:bedrock:*::foundation-model/*"
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
  }

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

  environment_variables = {
    ENV       = var.environment
    APP_NAME  = var.app_name
    KB_BUCKET = module.knowledge_base_bucket.s3_bucket_id
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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
  }

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
