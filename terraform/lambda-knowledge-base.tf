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
  timeout       = 900  # 15 minutes for processing large batches
  memory_size   = 1024 # Higher memory for Bedrock API calls

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory (includes shared module)
  source_path = [
    "${path.module}/../lambda/process-batch-summary",
    {
      path          = "${path.module}/../lambda/shared"
      prefix_in_zip = "shared"
    }
  ]

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV                     = var.environment
    APP_NAME                = var.app_name
    EVENT_BUS_NAME          = aws_cloudwatch_event_bus.automation.name
    ENABLE_BEDROCK_COOLDOWN = tostring(var.enable_bedrock_cooldown)
    AUDIT_TABLE             = module.audit_logs_table.dynamodb_table_id
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
  timeout       = 300  # 5 minutes for generating architectural summary
  memory_size   = 1024 # Higher memory for Bedrock API calls

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

  # Environment variables
  environment_variables = {
    ENV         = var.environment
    APP_NAME    = var.app_name
    AUDIT_TABLE = module.audit_logs_table.dynamodb_table_id
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
