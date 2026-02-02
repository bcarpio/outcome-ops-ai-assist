# ============================================================================
# Knowledge Base Lambda Functions
# - ingest-docs: Ingest ADRs and READMEs
# - generate-code-maps: Generate code maps and architectural summaries
# - process-batch-summary: Process code map batch summaries (SQS consumer)
# - process-repo-summary: Process repository architectural summaries
# ============================================================================

module "ingest_docs_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-ingest-docs"
  description   = "Ingest ADRs and READMEs into the knowledge base"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/ingest-docs"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # OutcomeOps license validator layer (if configured)
  layers = var.outcomeops_license_layer_arn != "" ? [var.outcomeops_license_layer_arn] : []

  # Environment variables
  environment_variables = merge(
    {
      ENV       = var.environment
      APP_NAME  = var.app_name
      REPO_NAME = "outcome-ops-ai-assist"
    },
    var.outcomeops_license_layer_arn != "" ? {
      OUTCOMEOPS_LICENSE_PARAM      = var.outcomeops_license_ssm_param
      OUTCOMEOPS_LICENSE_SERVER_URL = var.outcomeops_license_server_url
      OUTCOMEOPS_USAGE_ENABLED      = "true"
    } : {}
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Read from S3 knowledge base bucket
    s3_read = {
      effect = "Allow"
      actions = [
        "s3:ListBucket"
      ]
      resources = [
        module.knowledge_base_bucket.s3_bucket_arn
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

    # Read SSM parameters (including encrypted GitHub token, license)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = compact([
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*",
        var.outcomeops_license_layer_arn != "" ? "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.outcomeops_license_ssm_param}" : ""
      ])
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

    # Bedrock embeddings (Titan Embeddings v2)
    bedrock_invoke = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }

    eventbridge_put = {
      effect = "Allow"
      actions = [
        "events:PutEvents"
      ]
      resources = [
        aws_cloudwatch_event_bus.automation.arn
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

    # SQS permissions for queue-based processing
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.ingest_docs_queue.queue_arn
      ]
    }
  }

  tags = {
    Purpose = "ingest-docs"
  }
}

# Event source mapping - Connect SQS queue to ingest-docs Lambda
resource "aws_lambda_event_source_mapping" "ingest_docs_queue_to_lambda" {
  event_source_arn = module.ingest_docs_queue.queue_arn
  function_name    = module.ingest_docs_lambda.lambda_function_arn
  batch_size       = 1 # Process one repo at a time

  # Scaling configuration for FIFO queues
  scaling_config {
    maximum_concurrency = 5 # Limit concurrent executions to avoid GitHub rate limits
  }
}

# EventBridge rule to trigger dispatcher hourly (queues repos for processing)
resource "aws_cloudwatch_event_rule" "ingest_docs_schedule" {
  name                = "${var.environment}-${var.app_name}-ingest-docs-schedule"
  description         = "Trigger ingest-docs dispatcher every hour to queue repos"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "ingest-docs-schedule"
  }
}

resource "aws_cloudwatch_event_target" "ingest_docs_target" {
  rule      = aws_cloudwatch_event_rule.ingest_docs_schedule.name
  target_id = "IngestDocsDispatcher"
  arn       = module.ingest_docs_dispatcher_lambda.lambda_function_arn
}

# Allow EventBridge to invoke dispatcher Lambda
resource "aws_lambda_permission" "allow_eventbridge_ingest_docs" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.ingest_docs_dispatcher_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_docs_schedule.arn
}

# -----------------------------------------------------------------------------
# Dispatcher Lambda - Reads repo list and queues messages for processing
# -----------------------------------------------------------------------------

module "ingest_docs_dispatcher_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-ingest-docs-dispatcher"
  description   = "Queue repos for document ingestion processing"
  handler       = "handler.dispatcher_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from same directory as ingest-docs
  source_path = "${path.module}/../lambda/ingest-docs"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV                   = var.environment
    APP_NAME              = var.app_name
    INGEST_DOCS_QUEUE_URL = module.ingest_docs_queue.queue_url
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Read SSM parameters (repo allowlist)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    # Send messages to ingest-docs queue
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.ingest_docs_queue.queue_arn
      ]
    }
  }

  tags = {
    Purpose = "ingest-docs-dispatcher"
  }
}

output "ingest_docs_dispatcher_lambda_arn" {
  description = "ARN of the ingest-docs dispatcher Lambda function"
  value       = module.ingest_docs_dispatcher_lambda.lambda_function_arn
}

output "ingest_docs_lambda_arn" {
  description = "ARN of the ingest-docs Lambda function"
  value       = module.ingest_docs_lambda.lambda_function_arn
}

output "ingest_docs_lambda_name" {
  description = "Name of the ingest-docs Lambda function"
  value       = module.ingest_docs_lambda.lambda_function_name
}

# ============================================================================
# Generate Code Maps Lambda
# ============================================================================

module "generate_code_maps_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-generate-code-maps"
  description   = "Generate code maps and architectural summaries from repository structure"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 900  # 15 minutes for processing multiple repos
  memory_size   = 1024 # Higher memory for Bedrock API calls

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/generate-code-maps"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # OutcomeOps license validator layer (if configured)
  layers = var.outcomeops_license_layer_arn != "" ? [var.outcomeops_license_layer_arn] : []

  # Environment variables
  environment_variables = merge(
    {
      ENV                     = var.environment
      APP_NAME                = var.app_name
      FORCE_FULL_PROCESS      = "false" # Set to "true" for initial 0-day load
      ACTIVITY_WINDOW_MINUTES = "61"    # Time window for incremental mode (61 = hourly + buffer)
    },
    var.outcomeops_license_layer_arn != "" ? {
      OUTCOMEOPS_LICENSE_PARAM      = var.outcomeops_license_ssm_param
      OUTCOMEOPS_LICENSE_SERVER_URL = var.outcomeops_license_server_url
      OUTCOMEOPS_USAGE_ENABLED      = "true"
    } : {}
  )

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Read from S3 knowledge base bucket
    s3_read = {
      effect = "Allow"
      actions = [
        "s3:ListBucket"
      ]
      resources = [
        module.knowledge_base_bucket.s3_bucket_arn
      ]
    }

    # Write to S3 knowledge base bucket (code maps)
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

    # Write to DynamoDB code maps table (includes DeleteItem for stale cleanup)
    dynamodb_write = {
      effect = "Allow"
      actions = [
        "dynamodb:PutItem",
        "dynamodb:UpdateItem",
        "dynamodb:GetItem",
        "dynamodb:Query",
        "dynamodb:DeleteItem"
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn
      ]
    }

    # Send messages to SQS code maps queue and repo summaries queue
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_maps_queue.queue_arn,
        module.repo_summaries_queue.queue_arn
      ]
    }

    # Read SSM parameters (including encrypted GitHub token, license)
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = compact([
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*",
        var.outcomeops_license_layer_arn != "" ? "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${var.outcomeops_license_ssm_param}" : ""
      ])
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

    # Bedrock embeddings (Titan Embeddings v2)
    bedrock_titan = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }

    # Bedrock Claude (for architectural summaries)
    # Cross-region inference profiles route to available regions (us-east-1, us-west-2, etc)
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }

    # AWS Marketplace permissions for Anthropic Claude models
    aws_marketplace = {
      effect = "Allow"
      actions = [
        "aws-marketplace:ViewSubscriptions",
        "aws-marketplace:Subscribe"
      ]
      resources = ["*"]
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
  }

  tags = {
    Purpose = "generate-code-maps"
  }
}

# EventBridge rule to trigger code maps generation hourly (incremental mode)
resource "aws_cloudwatch_event_rule" "generate_code_maps_schedule" {
  name                = "${var.environment}-${var.app_name}-generate-code-maps-schedule"
  description         = "Trigger generate-code-maps Lambda every hour for incremental updates"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "generate-code-maps-schedule"
  }
}

resource "aws_cloudwatch_event_target" "generate_code_maps_target" {
  rule      = aws_cloudwatch_event_rule.generate_code_maps_schedule.name
  target_id = "GenerateCodeMapsLambda"
  arn       = module.generate_code_maps_lambda.lambda_function_arn

  # Pass empty payload - Lambda will use default incremental mode (FORCE_FULL_PROCESS=false)
  input = jsonencode({
    source = "eventbridge-scheduled"
  })
}

# Allow EventBridge to invoke Lambda
resource "aws_lambda_permission" "allow_eventbridge_generate_code_maps" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.generate_code_maps_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.generate_code_maps_schedule.arn
}

output "generate_code_maps_lambda_arn" {
  description = "ARN of the generate-code-maps Lambda function"
  value       = module.generate_code_maps_lambda.lambda_function_arn
}

output "generate_code_maps_lambda_name" {
  description = "Name of the generate-code-maps Lambda function"
  value       = module.generate_code_maps_lambda.lambda_function_name
}

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
  timeout       = 900  # 15 minutes for processing large batches
  memory_size   = 1024 # Higher memory for Bedrock API calls

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/process-batch-summary"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV                     = var.environment
    APP_NAME                = var.app_name
    EVENT_BUS_NAME          = aws_cloudwatch_event_bus.automation.name
    ENABLE_BEDROCK_COOLDOWN = tostring(var.enable_bedrock_cooldown)
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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

    # Bedrock embeddings (Titan Embeddings v2)
    bedrock_titan = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }

    # Bedrock Claude (for batch summaries)
    # Cross-region inference profiles route to available regions (us-east-1, us-west-2, etc)
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }

    # AWS Marketplace permissions for Anthropic Claude models
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
  }

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
  timeout       = 300  # 5 minutes for generating architectural summary
  memory_size   = 1024 # Higher memory for Bedrock API calls

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/process-repo-summary"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
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

    # Send messages to code maps queue
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_maps_queue.queue_arn
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

    # Bedrock embeddings (Titan Embeddings v2)
    bedrock_titan = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }

    # Bedrock Claude (for architectural summaries)
    # Cross-region inference profiles route to available regions
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*::foundation-model/us.anthropic.claude-sonnet-4-5-20250929-v1:0",
        "arn:aws:bedrock:*:${data.aws_caller_identity.current.account_id}:inference-profile/us.anthropic.claude-sonnet-4-5-20250929-v1:0"
      ]
    }

    # AWS Marketplace permissions for Anthropic Claude models
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
  }

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
}

output "process_repo_summary_lambda_arn" {
  description = "ARN of the process-repo-summary Lambda function"
  value       = module.process_repo_summary_lambda.lambda_function_arn
}

output "process_repo_summary_lambda_name" {
  description = "Name of the process-repo-summary Lambda function"
  value       = module.process_repo_summary_lambda.lambda_function_name
}
