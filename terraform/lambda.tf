# ----------------------------------------------------------------------------
# Lambda Functions
# ----------------------------------------------------------------------------

# List Recent Documents Handler
module "list_recent_docs_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "7.2.1"

  function_name = "${var.app_name}-${var.environment}-list-recent-docs"
  description  = "List recently uploaded documents from DynamoDB"
  handler      = "list_recent_docs.lambda_handler"
  runtime      = "python3.12"
  timeout      = 30
  memory_size  = 128

  source_path = [
    {
      path = "${path.module}/../src/lambda/list_recent_docs"
      patterns = [
        "!.*",
        "!.pyc",
        "!__pycache__/.*",
        "!.pytest_cache/.*",
        "!tests/.*",
        "!.git/*"
      ]
    },
    {
      path = "${path.module}/../src/lambda/common"
      prefix_in_zip = "common"
      patterns = [
        "!.*",
        "!.pyc",
        "!__pycache__/.*",
        "!.pytest_cache/.*",
        "!tests/.*",
        "!.git/*"
      ]
    }
  ]

  environment_variables = {
    ENVIRONMENT = var.environment
    APP_NAME     = var.app_name
    LOG_LEVEL    = var.log_level
  }

  attach_policy_statements = true
  policy_statements = {
    # DynamoDB permissions for scan and query operations
    dynamodb_read = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:GetItem"
      ]
      resources = [
        aws_dynamodb_table.documents.arn,
        "${aws_dynamodb_table.documents.arn}/index/*"
      ]
    }

    # SSM Parameter Store permissions for table name lookup
    ssm_read = {
      effect = "Allow"
      actions = [
        "ssm:GetParameter",
        "ssm:GetParameters"
      ]
      resources = [
        "arn:aws:ssm:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/dynamodb/*"
      ]
    }

    # CloudWatch Logs permissions
    logs = {
      effect = "Allow"
      actions = [
        "logs:CreateLogGroup",
        "logs:CreateLogStream",
        "logs:PutLogEvents"
      ]
      resources = [
        "arn:aws:logs:${data.aws_region.current.name}:${data.aws_caller_identity.current.account_id}:log-group:/aws/lambda/${var.app_name}-${var.environment}-list-recent-docs:*"
      ]
    }
  }

  cloudwatch_logs_retention_in_days = var.log_retention_days

  tags = merge(
    var.tags,
    {
      Name      = "${var.app_name}-${var.environment}-list-recent-docs"
      Function = "list-recent-docs"
    }
  )
}

# API Gateway Integration
resource "aws_apigatewayv2_integration" "list_recent_docs" {
  api_id             = aws_apigatewayv2_api.main.id
  integration_type   = "AWS_PROXY"
  integration_uri    = module.list_recent_docs_lambda.lambda_function_invoke_arn
  integration_method = "POST"
  payload_format_version = "2.0"
}

resource "aws_apigatewayv2_route" "list_recent_docs" {
  api_id     = aws_apigatewayv2_api.main.id
  route_key = "GET /api/v1/documents/recent"
  target    = "integrations/${aws_apigatewayv2_integration.list_recent_docs.id}"

  authorization_type = "JWT"
  authorizer_id      = aws_apigatewayv2_authorizer.cognito.id
}

resource "aws_lambda_permission" "list_recent_docs_apigw" {
  statement_id  = "AllowAPIGatewayInvoke"
  action        = "lambda:InvokeFunction"
  function_name = module.list_recent_docs_lambda.lambda_function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/GET/api/v1/documents/recent"
}
