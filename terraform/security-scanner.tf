# Security Scanner Auth Endpoint
#
# Resources:
# - random_id "scanner_path" (Secret URL path for auth endpoint)
# - random_password "scanner_password" (Service account password)
# - random_password "scanner_jwt_secret" (JWT signing secret)
# - random_password "scanner_header_key" (Custom header for ALB OIDC bypass)
# - aws_ssm_parameter "scanner_path" (SSM for secret path)
# - aws_ssm_parameter "scanner_password" (SSM for password)
# - aws_ssm_parameter "scanner_jwt_secret" (SSM for JWT secret)
# - aws_dynamodb_table_item "scanner_user" (DynamoDB service account)
# - module "security_scanner_lambda" (Login form handler for ALB events)
# - aws_lb_target_group "scanner" (Lambda target group)
# - aws_lb_listener_rule "scanner" (ALB rule bypassing OIDC, priority 2)
# - aws_lb_listener_rule "scanner_header_bypass" (Header-based OIDC bypass, priority 4)
#
# Conditional on security_scanner_enabled and deploy_ui.
# Provides password-based auth for headless AWS Security Agent.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
