module "ingest_docs_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-ingest-docs"
  description   = "Ingest ADRs and READMEs into the knowledge base"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300
  memory_size   = 512

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/ingest-docs"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV       = var.environment
    APP_NAME  = var.app_name
    REPO_NAME = "outcome-ops-ai-assist"
  }

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
    bedrock_invoke = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:${var.aws_region}::foundation-model/amazon.titan-embed-text-v2:0"
      ]
    }
  }

  tags = {
    Purpose = "ingest-docs"
  }
}

# EventBridge rule to trigger ingest Lambda hourly
resource "aws_cloudwatch_event_rule" "ingest_docs_schedule" {
  name                = "${var.environment}-${var.app_name}-ingest-docs-schedule"
  description         = "Trigger ingest-docs Lambda every hour"
  schedule_expression = "rate(1 hour)"

  tags = {
    Purpose = "ingest-docs-schedule"
  }
}

resource "aws_cloudwatch_event_target" "ingest_docs_target" {
  rule      = aws_cloudwatch_event_rule.ingest_docs_schedule.name
  target_id = "IngestDocsLambda"
  arn       = module.ingest_docs_lambda.lambda_function_arn
}

# Allow EventBridge to invoke Lambda
resource "aws_lambda_permission" "allow_eventbridge_ingest_docs" {
  statement_id  = "AllowExecutionFromEventBridge"
  action        = "lambda:InvokeFunction"
  function_name = module.ingest_docs_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.ingest_docs_schedule.arn
}

# CloudWatch Log Group for Lambda is created by the Lambda module, but we can still reference it
# The lambda module creates one automatically, so we don't need a separate resource

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

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/generate-code-maps"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV                = var.environment
    APP_NAME           = var.app_name
    FORCE_FULL_PROCESS = "false" # Set to "true" for initial 0-day load
  }

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

    # Send messages to SQS code maps queue
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
  }

  tags = {
    Purpose = "generate-code-maps"
  }
}

# No EventBridge schedule - this Lambda is invoked manually or on-demand

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

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/process-batch-summary"

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
# Vector Query Lambda (Semantic Search)
# ============================================================================

module "vector_query_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-vector-query"
  description   = "Perform semantic search over knowledge base embeddings"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes for DynamoDB scan + embedding generation
  memory_size   = 512

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/vector-query"

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
    # Read from DynamoDB code maps table
    dynamodb_read = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:GetItem"
      ]
      resources = [
        module.code_maps_table.dynamodb_table_arn
      ]
    }

    # Read SSM parameters
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
  }

  tags = {
    Purpose = "vector-query"
  }
}

# Store Lambda ARN in SSM for query-kb orchestrator
resource "aws_ssm_parameter" "vector_query_lambda_arn" {
  name  = "/${var.environment}/${var.app_name}/lambda/vector-query-arn"
  type  = "String"
  value = module.vector_query_lambda.lambda_function_arn

  tags = {
    Purpose = "vector-query-config"
  }
}

output "vector_query_lambda_arn" {
  description = "ARN of the vector-query Lambda function"
  value       = module.vector_query_lambda.lambda_function_arn
}

output "vector_query_lambda_name" {
  description = "Name of the vector-query Lambda function"
  value       = module.vector_query_lambda.lambda_function_name
}

# ============================================================================
# Ask Claude Lambda (RAG Answer Generation)
# ============================================================================

module "ask_claude_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-ask-claude"
  description   = "Generate RAG answers using Claude 3.5 Sonnet via Bedrock"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 300 # 5 minutes for Claude API calls
  memory_size   = 512

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/ask-claude"

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
    # Bedrock Claude (for RAG generation)
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
  }

  tags = {
    Purpose = "ask-claude"
  }
}

# Store Lambda ARN in SSM for query-kb orchestrator
resource "aws_ssm_parameter" "ask_claude_lambda_arn" {
  name  = "/${var.environment}/${var.app_name}/lambda/ask-claude-arn"
  type  = "String"
  value = module.ask_claude_lambda.lambda_function_arn

  tags = {
    Purpose = "ask-claude-config"
  }
}

output "ask_claude_lambda_arn" {
  description = "ARN of the ask-claude Lambda function"
  value       = module.ask_claude_lambda.lambda_function_arn
}

output "ask_claude_lambda_name" {
  description = "Name of the ask-claude Lambda function"
  value       = module.ask_claude_lambda.lambda_function_name
}

# ============================================================================
# Query KB Lambda (RAG Orchestrator)
# ============================================================================

module "query_kb_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-query-kb"
  description   = "Orchestrate RAG pipeline (vector search + Claude generation)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 600 # 10 minutes for full RAG pipeline
  memory_size   = 512

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/query-kb"

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
    # Read SSM parameters (to get other Lambda ARNs)
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

    # Invoke vector-query and ask-claude Lambdas
    lambda_invoke = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.vector_query_lambda.lambda_function_arn,
        module.ask_claude_lambda.lambda_function_arn
      ]
    }
  }

  tags = {
    Purpose = "query-kb"
  }
}

output "query_kb_lambda_arn" {
  description = "ARN of the query-kb Lambda function"
  value       = module.query_kb_lambda.lambda_function_arn
}

output "query_kb_lambda_name" {
  description = "Name of the query-kb Lambda function"
  value       = module.query_kb_lambda.lambda_function_name
}

# ============================================================================
# Analyze PR Lambda (GitHub PR Analysis Orchestration)
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

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/analyze-pr"

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
  }

  tags = {
    Purpose = "analyze-pr"
  }
}

# No EventBridge schedule - this Lambda is invoked manually or via GitHub webhook

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

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/process-pr-check"

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

    # Bedrock Claude (for PR check analysis)
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
  }

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

# ============================================================================
# Lambda: Generate Code (GitHub Webhook Integration)
# ============================================================================

module "generate_code_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-generate-code"
  description   = "Process GitHub webhook to generate code from approved issues"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 900  # 15 minutes for plan generation and step execution
  memory_size   = 1024 # Higher memory for Bedrock API calls
  publish       = true

  # CloudWatch Logs retention
  cloudwatch_logs_retention_in_days = 7

  # Source code from local directory
  source_path = "${path.module}/../lambda/generate-code"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Environment variables
  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
  }

  # API Gateway trigger permission
  allowed_triggers = {
    APIGateway = {
      service    = "apigateway"
      source_arn = "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${module.outcome_ops_api.api_id}/*/*/*"
    }
  }

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Read SSM parameters (GitHub token, webhook secret, SQS queue URL)
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

    # SQS permissions for code generation queue
    sqs_send = {
      effect = "Allow"
      actions = [
        "sqs:SendMessage",
        "sqs:GetQueueUrl",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_generation_queue.queue_arn
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
        module.code_generation_queue.queue_arn
      ]
    }

    # Bedrock Claude (for plan generation and code generation)
    # Cross-region inference profiles route to available regions
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*"
      ]
    }

    # Invoke query-kb Lambda for knowledge base queries
    invoke_query_kb = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.query_kb_lambda.lambda_function_arn
      ]
    }
  }

  tags = {
    Purpose = "generate-code"
  }
}

# Event source mapping - Connect SQS code generation queue to Lambda
resource "aws_lambda_event_source_mapping" "code_generation_queue_to_lambda" {
  event_source_arn = module.code_generation_queue.queue_arn
  function_name    = module.generate_code_lambda.lambda_function_name
  batch_size       = 1 # Process one step at a time
  enabled          = true

  # Function response types for partial batch failures
  function_response_types = ["ReportBatchItemFailures"]
}

output "generate_code_lambda_arn" {
  description = "ARN of the generate-code Lambda function"
  value       = module.generate_code_lambda.lambda_function_arn
}

output "generate_code_lambda_name" {
  description = "Name of the generate-code Lambda function"
  value       = module.generate_code_lambda.lambda_function_name
}

output "process_pr_check_lambda_arn" {
  description = "ARN of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_arn
}

output "process_pr_check_lambda_name" {
  description = "Name of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_name
}
