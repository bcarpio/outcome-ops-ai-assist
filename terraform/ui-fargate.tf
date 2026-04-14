# Chat UI - Fargate Spot + ALB
#
# Resources:
# - data "aws_vpc" "ui" (VPC data source for UI deployment)
# - module "ui_alb_sg" (Security group for ALB)
# - module "ui_fargate_sg" (Security group for Fargate tasks)
# - data "aws_route53_zone" "main" (Route53 zone for domain)
# - aws_acm_certificate "ui" (TLS certificate for OIDC)
# - aws_route53_record "cert_validation" (DNS validation records)
# - aws_route53_record "ui" (A record alias to ALB)
# - data "aws_ssm_parameter" "oidc_client_secret" (OIDC client secret from SSM)
# - module "ui_alb" (Application Load Balancer)
# - aws_lb_listener "http_redirect" (HTTP to HTTPS redirect when OIDC enabled)
# - aws_lb_listener "https" (HTTPS listener with Azure AD OIDC authentication)
# - module "ui_ecs" (ECS cluster + Fargate Spot service)
#
# Conditional on deploy_ui. Supports internal or public ALB.
# OIDC via Azure AD when domain and oidc_enabled are configured.
# Fargate Spot for cost savings, single task deployment.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
