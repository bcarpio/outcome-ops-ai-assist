# Lambda Function Configuration
# This module deploys the Code Maps Lambda function with appropriate IAM permissions

module "code_maps_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "7.14.0"

  function_name = "${var.environment}-${var.app_name}-code-maps"
  description  = "Lambda function to manage code maps for MyFantasy application"
  handler      = "index.handler"
  runtime      = "python3.12"
  source_path   = "../src/lambdas/code_maps"

  # Performance configuration
  timeout     = 30
  memory_size = 512

  # Environment variables
  environment_variables = {
    ENV              = var.environment
    APP_NAME         = var.app_name
    CODE_MAPS_TABLE  = module.dynamodb.table_name
  }

  # IAM policy attachments
  attach_policy_statements = true
  policy_statements = {
    # DynamoDB permissions for scan and query operations
    dynamodb = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",        # Required for full table scans with filters
        "dynamodb:Query",       # Required for GSI queries
        "dynamodb:GetItem"      # Required for single item retrieval
      ]
      resources = [
        module.dynamodb.table_arn,
        "${module.dynamodb.table_arn}/index/*"  # Include GSI access
      ]
    }

    # SSM Parameter Store permissions for reading configuration
    ssm = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter"  # Read-only access to parameters
      ]
      resources = [
        "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/*"
      ]
    }
  }

  # Tags for resource management
  tags = merge(
    var.common_tags,
    {
      Name      = "${var.environment}-${var.app_name}-code-maps"
      Component = "lambda"
      Function  = "code-maps"
    }
  )
}

# Data source for current AWS account ID
data "aws_caller_identity" "current" {}

# Outputs for use by other modules
output "lambda_function_arn" {
  description = "ARN of the Code Maps Lambda function"
  value       = module.code_maps_lambda.lambda_function_arn
}

output "lambda_function_name" {
  description = "Name of the Code Maps Lambda function"
  value       = module.code_maps_lambda.lambda_function_name
}

output "lambda_function_invoke_arn" {
  description = "Invoke ARN of the Code Maps Lambda function"
  value       = module.code_maps_lambda.lambda_function_invoke_arn
}

output "lambda_role_arn" {
  description = "ARN of the IAM role used by the Lambda function"
  value       = module.code_maps_lambda.lambda_role_arn
}
