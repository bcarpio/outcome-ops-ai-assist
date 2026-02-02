# ============================================================================
# Code Generation Lambda Functions
# - generate-code: Process GitHub webhook to generate code from approved issues
# - generate-code-dlq: Process failed code generation steps from DLQ
# - run-tests: Clone repo branches and run tests after code generation
# - handle-command: Process PR comment commands
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

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/generate-code"

  # Unique hash to prevent race condition with generate_code_dlq_lambda (same source_path)
  hash_extra = "generate-code-main"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Attach layers: terraform fmt, git (for terraform formatting), license validator
  layers = compact([
    module.terraform_tools_layer.lambda_layer_arn,
    module.runtime_tools_layer.lambda_layer_arn,
    var.outcomeops_license_layer_arn
  ])

  # Environment variables
  environment_variables = merge(
    {
      ENV                     = var.environment
      APP_NAME                = var.app_name
      EVENT_BUS_NAME          = aws_cloudwatch_event_bus.automation.name
      ENABLE_BEDROCK_COOLDOWN = tostring(var.enable_bedrock_cooldown)
      # Include /opt/bin in PATH for git from runtime_tools_layer
      PATH = "/opt/bin:/var/lang/bin:/usr/local/bin:/usr/bin:/bin"
      # Tell git where to find helper programs like git-remote-https
      GIT_EXEC_PATH = "/opt/libexec/git-core"
      # Shared libraries for git
      LD_LIBRARY_PATH = "/opt/lib:/var/lang/lib:/lib64:/usr/lib64"
    },
    var.outcomeops_license_layer_arn != "" ? {
      OUTCOMEOPS_LICENSE_PARAM      = var.outcomeops_license_ssm_param
      OUTCOMEOPS_LICENSE_SERVER_URL = var.outcomeops_license_server_url
      OUTCOMEOPS_USAGE_ENABLED      = "true"
    } : {}
  )

  # API Gateway trigger permission (only when GitHub Issue integration is enabled)
  allowed_triggers = var.enable_github_issue_integration ? {
    APIGateway = {
      service    = "apigateway"
      source_arn = "arn:aws:execute-api:${var.aws_region}:${data.aws_caller_identity.current.account_id}:${module.outcome_ops_api[0].api_id}/*/*/*"
    }
  } : {}

  # IAM permissions
  attach_policy_statements = true
  policy_statements = {
    # Read SSM parameters (GitHub token, webhook secret, SQS queue URL, license)
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
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/*"
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

    # Invoke ask-claude Lambda for Haiku summarization of KB results
    invoke_ask_claude = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.ask_claude_lambda.lambda_function_arn
      ]
    }

    # Invoke run-tests Lambda for fix-tests command
    invoke_run_tests = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.run_tests_lambda.lambda_function_arn
      ]
    }

    # Publish completion events to EventBridge automation bus
    eventbridge_publish = {
      effect = "Allow"
      actions = [
        "events:PutEvents"
      ]
      resources = [
        aws_cloudwatch_event_bus.automation.arn
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

# ============================================================================
# Generate Code DLQ Handler Lambda
# ============================================================================
# Processes failed code generation steps from the DLQ to:
# 1. Create a PR with whatever partial work was completed
# 2. Track usage metrics for billing purposes
# 3. Add a comment to the issue explaining the failure

module "generate_code_dlq_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-generate-code-dlq"
  description   = "Process failed code generation steps from DLQ"
  handler       = "dlq_handler.handler"
  runtime       = "python3.12"
  timeout       = 60  # Short timeout - just creates PR and fires metric
  memory_size   = 256 # Minimal memory needed
  publish       = true

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Use same source as generate-code (shares github_api, models, etc.)
  source_path = "${path.module}/../lambda/generate-code"

  # Unique hash to prevent race condition with generate_code_lambda (same source_path)
  hash_extra = "generate-code-dlq"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # Attach license layer for usage tracking
  layers = compact([
    var.outcomeops_license_layer_arn
  ])

  # Environment variables
  environment_variables = merge(
    {
      ENV      = var.environment
      APP_NAME = var.app_name
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
    # Read SSM parameters (GitHub token, license)
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

    # Decrypt SSM parameters
    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    # SQS permissions to read from DLQ
    sqs_receive = {
      effect = "Allow"
      actions = [
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:GetQueueAttributes"
      ]
      resources = [
        module.code_generation_dlq.queue_arn
      ]
    }
  }

  tags = {
    Purpose = "generate-code-dlq"
  }
}

# Event source mapping - Connect DLQ to DLQ handler Lambda
resource "aws_lambda_event_source_mapping" "code_generation_dlq_to_lambda" {
  event_source_arn = module.code_generation_dlq.queue_arn
  function_name    = module.generate_code_dlq_lambda.lambda_function_name
  batch_size       = 1 # Process one failed message at a time
  enabled          = true
}

output "generate_code_dlq_lambda_arn" {
  description = "ARN of the generate-code-dlq Lambda function"
  value       = module.generate_code_dlq_lambda.lambda_function_arn
}

output "generate_code_dlq_lambda_name" {
  description = "Name of the generate-code-dlq Lambda function"
  value       = module.generate_code_dlq_lambda.lambda_function_name
}

# ============================================================================
# Run Tests Lambda (Container-based)
# Multi-language test runner: Python, Java, TypeScript, Terraform
# Triggered by EventBridge for all code generation completions
# ============================================================================
#
# BOOTSTRAP PROCESS (dev example, use 'prd' for production):
# 1. Deploy ECR:
#    terraform workspace select dev
#    terraform apply -var-file=dev.tfvars -target=aws_ecr_repository.run_tests
#
# 2. Build container:
#    ./scripts/build-run-tests-image.sh --env dev
#
# 3. Full deploy:
#    terraform apply -var-file=dev.tfvars
#
# ============================================================================

# Common IAM policy statements for run-tests Lambda
locals {
  run_tests_policy_statements = {
    s3_put_results = {
      effect = "Allow"
      actions = [
        "s3:PutObject"
      ]
      resources = [
        "${module.knowledge_base_bucket.s3_bucket_arn}/*"
      ]
    }

    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }

    kms_decrypt = {
      effect = "Allow"
      actions = [
        "kms:Decrypt"
      ]
      resources = [
        "arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"
      ]
    }

    eventbridge_publish = {
      effect = "Allow"
      actions = [
        "events:PutEvents"
      ]
      resources = [
        aws_cloudwatch_event_bus.automation.arn
      ]
    }

    invoke_analyze_pr = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.analyze_pr_lambda.lambda_function_arn
      ]
    }

    invoke_query_kb = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.query_kb_lambda.lambda_function_arn
      ]
    }

    # Bedrock Claude (for auto-fixing import/syntax errors)
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/*"
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

    # ECR permissions to pull container image
    ecr_pull = {
      effect = "Allow"
      actions = [
        "ecr:GetDownloadUrlForLayer",
        "ecr:BatchGetImage"
      ]
      resources = [
        aws_ecr_repository.run_tests.arn
      ]
    }
  }
}

# Read the image tag from SSM (set by build-run-tests-image.sh)
data "aws_ssm_parameter" "run_tests_image_tag" {
  name = "/${var.environment}/${var.app_name}/run-tests/image-tag"
}

module "run_tests_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-run-tests"
  description   = "Run tests for code generation (Python, Java, TypeScript, Terraform)"
  timeout       = 900
  memory_size   = 2048
  publish       = true

  # Use container image instead of zip
  # Tag is stored in SSM by build-run-tests-image.sh script
  create_package = false
  package_type   = "Image"
  image_uri      = "${aws_ecr_repository.run_tests.repository_url}:${data.aws_ssm_parameter.run_tests_image_tag.value}"

  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  ephemeral_storage_size = 2048 # 2GB for large repos/dependencies
  architectures          = ["x86_64"]

  environment_variables = {
    ENV                 = var.environment
    APP_NAME            = var.app_name
    TEST_RESULTS_BUCKET = module.knowledge_base_bucket.s3_bucket_id
    TEST_RESULTS_PREFIX = "test-results"
    EVENT_BUS_NAME      = aws_cloudwatch_event_bus.automation.name
    # Git runtime configuration (must match Dockerfile ENV vars)
    PATH            = "/opt/git/bin:/usr/lib/jvm/java-21-amazon-corretto/bin:/usr/local/bin:/var/lang/bin:/usr/local/bin:/usr/bin/:/bin:/opt/bin"
    GIT_EXEC_PATH   = "/opt/git/libexec/git-core"
    LD_LIBRARY_PATH = "/var/lang/lib:/lib64:/usr/lib64:/var/runtime:/var/runtime/lib:/var/task:/var/task/lib:/opt/lib"
  }

  attach_policy_statements = true
  policy_statements        = local.run_tests_policy_statements

  tags = {
    Purpose = "run-tests"
  }
}

# Single EventBridge rule for all code generation completions
# The Lambda internally routes to the correct backend based on .outcomeops.yaml
resource "aws_cloudwatch_event_rule" "code_generation_completed" {
  name           = "${var.environment}-${var.app_name}-codegen-completed"
  description    = "Trigger test runner after code generation completes"
  event_bus_name = aws_cloudwatch_event_bus.automation.name

  event_pattern = jsonencode({
    "source" : ["outcomeops.generate-code"],
    "detail-type" : ["OutcomeOps.CodeGeneration.Completed"],
    "detail" : {
      "environment" : [var.environment],
      "appName" : [var.app_name]
    }
  })
}

resource "aws_cloudwatch_event_target" "run_tests_target" {
  rule           = aws_cloudwatch_event_rule.code_generation_completed.name
  event_bus_name = aws_cloudwatch_event_bus.automation.name
  target_id      = "RunTestsLambda"
  arn            = module.run_tests_lambda.lambda_function_arn
}

resource "aws_lambda_permission" "allow_eventbridge_run_tests" {
  statement_id  = "AllowExecutionFromEventBridgeRunTests"
  action        = "lambda:InvokeFunction"
  function_name = module.run_tests_lambda.lambda_function_name
  principal     = "events.amazonaws.com"
  source_arn    = aws_cloudwatch_event_rule.code_generation_completed.arn
}

output "run_tests_lambda_arn" {
  description = "ARN of the run-tests Lambda function"
  value       = module.run_tests_lambda.lambda_function_arn
}

output "run_tests_lambda_name" {
  description = "Name of the run-tests Lambda function"
  value       = module.run_tests_lambda.lambda_function_name
}

# ============================================================================
# Handle Command Lambda (PR Comment Commands)
# ============================================================================

module "handle_command_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = "${var.environment}-${var.app_name}-handle-command"
  description   = "Process PR comment commands (outcomeops: fix readme, etc.)"
  handler       = "handler.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60 # 1 minute for GitHub API calls
  memory_size   = 256

  # CloudWatch Logs retention and encryption
  cloudwatch_logs_retention_in_days = 7
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  # Source code from local directory
  source_path = "${path.module}/../lambda/handle-command"

  # Suppress verbose archive output
  quiet_archive_local_exec = true

  # OutcomeOps license validator layer (if configured)
  layers = var.outcomeops_license_layer_arn != "" ? [var.outcomeops_license_layer_arn] : []

  # Environment variables
  environment_variables = merge(
    {
      ENV      = var.environment
      APP_NAME = var.app_name
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
    # Read SSM parameters (GitHub token and license)
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

    # Send messages to SQS code generation queue (for async PR commands)
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

    # Bedrock Claude for test failure analysis
    bedrock_claude = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/anthropic.claude-*",
        "arn:aws:bedrock:*:*:inference-profile/*"
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
  }

  tags = {
    Purpose = "handle-command"
  }
}

output "handle_command_lambda_arn" {
  description = "ARN of the handle-command Lambda function"
  value       = module.handle_command_lambda.lambda_function_arn
}

output "handle_command_lambda_name" {
  description = "Name of the handle-command Lambda function"
  value       = module.handle_command_lambda.lambda_function_name
}
