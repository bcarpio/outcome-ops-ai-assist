# Lambda Functions

# Query Knowledge Base Lambda
module "query_kb_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-query-kb"
  description   = "Query knowledge base for context"
  handler       = "query_kb.lambda_handler"
  runtime       = "python3.12"
  timeout       = 60
  memory_size   = 256

  source_dir = "../lambdas/query_kb"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV                     = var.environment
    APP_NAME                = var.app_name
    KNOWLEDGE_BASE_ID       = aws_bedrock_knowledge_base.main.id
    KNOWLEDGE_BASE_DATA_SOURCE_ID = aws_bedrock_data_source.main.data_source_id
    LOG_LEVEL               = "INFO"
  }

  policy_statements = {
    bedrock_retrieve = {
      effect = "Allow"
      actions = [
        "bedrock:Retrieve",
        "bedrock:RetrieveAndGenerate"
      ]
      resources = [
        aws_bedrock_knowledge_base.main.arn
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Generate Response Lambda
module "generate_response_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-generate-response"
  description   = "Generate AI response using Bedrock"
  handler       = "generate_response.lambda_handler"
  runtime       = "python3.12"
  timeout       = 90
  memory_size   = 512

  source_dir = "../lambdas/generate_response"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV                     = var.environment
    APP_NAME                = var.app_name
    MODEL_ID                 = var.bedrock_model_id
    QUERY_KB_LAMBDA_ARN     = module.query_kb_lambda.function_arn
    LOG_LEVEL               = "INFO"
  }

  policy_statements = {
    bedrock_invoke = {
      effect = "Allow"
      actions = [
        "bedrock:InvokeModel"
      ]
      resources = [
        "arn:aws:bedrock:*::foundation-model/*"
      ]
    }
    lambda_invoke = {
      effect = "Allow"
      actions = [
        "lambda:InvokeFunction"
      ]
      resources = [
        module.query_kb_lambda.function_arn
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Admin Stats Lambda
module "admin_stats_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-admin-stats"
  description   = "Get admin statistics"
  handler       = "admin_stats.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  source_dir = "../lambdas/admin_stats"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV      = var.environment
    APP_NAME = var.app_name
    LOG_LEVEL = "INFO"
  }

  policy_statements = {
    dynamodb_scan = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query"
      ]
      resources = [
        aws_dynamodb_table.main.arn,
        "${aws_dynamodb_table.main.arn}/index/*"
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Admin Users Lambda
module "admin_users_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-admin-users"
  description   = "Getand manage users"
  handler       = "admin_users.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  source_dir = "../lambdas/admin_users"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV       = var.environment
    APP_NAME = var.app_name
    LOG_LEVEL = "INFO"
  }

  policy_statements = {
    dynamodb_scan = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ]
      resources = [
        aws_dynamodb_table.main.arn,
        "${aws_dynamodb_table.main.arn}/index/*"
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Moderation Characters Lambda
module "moderation_characters_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-moderation-characters"
  description   = "Moderate characters"
  handler       = "moderation_characters.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  source_dir = "../lambdas/moderation_characters"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV       = var.environment
    APP_NAME = var.app_name
    LOG_LEVEL = "INFO"
  }

  policy_statements = {
    dynamodb_scan = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query",
        "dynamodb:GetItem",
        "dynamodb:PutItem",
        "dynamodb:UpdateItem"
      ]
      resources = [
        aws_dynamodb_table.main.arn,
        "${aws_dynamodb_table.main.arn}/index/*"
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Admin List Coupons Lambda
module "admin_list_coupons_lambda" {
  source = "./modules/lambda"

  function_name = "${var.app_name}-admin-list-coupons"
  description   = "List coupons"
  handler       = "admin_list_coupons.lambda_handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 256

  source_dir = "../lambdas/admin_list_coupons"
  layer_arns = [aws_lambda_layer_version.common_layer.arn]

  environment_variables = {
    ENV       = var.environment
    APP_NAME = var.app_name
    LOG_LEVEL = "INFO"
  }

  policy_statements = {
    dynamodb_scan = {
      effect = "Allow"
      actions = [
        "dynamodb:Scan",
        "dynamodb:Query"
      ]
      resources = [
        aws_dynamodb_table.main.arn,
        "${aws_dynamodb_table.main.arn}/index/*"
      ]
    }
    ssm_parameters = {
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
        aws_kms_key.main.arn
      ]
    }
  }

  tags = var.tags
}

# Common Lambda Layer
resource "aws_lambda_layer_version" "common_layer" {
  filename            = "../layers/common_layer.zip"
  layer_name          = "${var.app_name}-common"
  compatible_runtimes = ["python3.12"]
  source_code_hash    = filebase64sha256("../layers/common_layer.zip")

  description = "Common utilities and dependencies"
}
