# ============================================================================
# API Gateway v2 (HTTP API)
# ============================================================================

module "outcome_ops_api" {
  source  = "terraform-aws-modules/apigateway-v2/aws"
  version = "5.2.1"

  name          = "${var.environment}-${var.app_name}-api"
  description   = "OutcomeOps AI Assist HTTP API Gateway"
  protocol_type = "HTTP"

  # No custom domain - use default API Gateway URL
  create_domain_name    = false
  create_certificate    = false
  create_domain_records = false

  # CORS configuration for webhooks
  cors_configuration = {
    allow_headers = ["*"]
    allow_methods = ["POST"]
    allow_origins = ["https://github.com"]
  }

  stage_default_route_settings = {
    detailed_metrics_enabled = false
  }

  tags = {
    Environment = var.environment
    App         = var.app_name
    Purpose     = "outcome-ops-api-gateway"
  }

  routes = {
    # GitHub webhook endpoint for code generation
    "POST /webhooks/github" = {
      integration = {
        uri                    = module.generate_code_lambda.lambda_function_invoke_arn
        integration_type       = "AWS_PROXY"
        integration_method     = "POST"
        payload_format_version = "2.0"
      }
    }
  }
}

output "api_gateway_endpoint" {
  description = "API Gateway endpoint URL"
  value       = module.outcome_ops_api.stage_invoke_url
}

output "github_webhook_url" {
  description = "GitHub webhook URL for code generation"
  value       = "${module.outcome_ops_api.stage_invoke_url}/webhooks/github"
}
