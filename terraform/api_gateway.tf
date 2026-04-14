# API Gateway
#
# Resources:
# - module "outcome_ops_api" (API Gateway v2 HTTP API for webhooks)
# - output "api_gateway_endpoint" (API Gateway endpoint URL)
# - output "github_webhook_url" (GitHub webhook URL for code generation)
#
# Conditional on enable_github_issue_integration.
# Routes GitHub webhook POST requests to the generate-code Lambda.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
