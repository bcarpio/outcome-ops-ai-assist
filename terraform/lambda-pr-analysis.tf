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

    # AWS Marketplace permissions for Anthropic Claude models
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

output "process_pr_check_lambda_arn" {
  description = "ARN of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_arn
}

output "process_pr_check_lambda_name" {
  description = "Name of the process-pr-check Lambda function"
  value       = module.process_pr_check_lambda.lambda_function_name
}
