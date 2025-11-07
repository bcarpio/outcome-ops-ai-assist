# ============================================================================
# Hello World Lambda - INTENTIONALLY NOT USING STANDARD MODULE PATTERN
# This should trigger architectural duplication check
# ============================================================================

# IAM role for Lambda execution
resource "aws_iam_role" "hello_world_lambda_role" {
  name = "${var.environment}-${var.app_name}-hello-world-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Action = "sts:AssumeRole"
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
      }
    ]
  })

  tags = {
    Purpose = "hello-world-lambda"
  }
}

# Attach basic Lambda execution policy
resource "aws_iam_role_policy_attachment" "hello_world_lambda_basic" {
  role       = aws_iam_role.hello_world_lambda_role.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# CloudWatch Log Group
resource "aws_cloudwatch_log_group" "hello_world_lambda_logs" {
  name              = "/aws/lambda/${var.environment}-${var.app_name}-hello-world"
  retention_in_days = 7

  tags = {
    Purpose = "hello-world-lambda"
  }
}

# Lambda function - inline resource instead of using terraform-aws-modules
resource "aws_lambda_function" "hello_world" {
  function_name = "${var.environment}-${var.app_name}-hello-world"
  description   = "Simple hello world Lambda for testing"
  role          = aws_iam_role.hello_world_lambda_role.arn
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 128

  filename         = "${path.module}/../lambda/hello-world.zip"
  source_code_hash = filebase64sha256("${path.module}/../lambda/hello-world.zip")

  environment {
    variables = {
      ENV      = var.environment
      APP_NAME = var.app_name
    }
  }

  depends_on = [
    aws_cloudwatch_log_group.hello_world_lambda_logs,
    aws_iam_role_policy_attachment.hello_world_lambda_basic
  ]

  tags = {
    Purpose = "hello-world-lambda"
  }
}

output "hello_world_lambda_arn" {
  description = "ARN of the hello-world Lambda function"
  value       = aws_lambda_function.hello_world.arn
}

output "hello_world_lambda_name" {
  description = "Name of the hello-world Lambda function"
  value       = aws_lambda_function.hello_world.function_name
}
