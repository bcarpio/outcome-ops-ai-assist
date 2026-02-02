# ============================================================================
# Chat UI - Fargate Spot + ALB
# Internal (default): Enterprise customers use VPC, Direct Connect, TGW, VPN
# Public: Secured by Azure AD OIDC for dev/demos
# ============================================================================

# -----------------------------------------------------------------------------
# Data sources
# -----------------------------------------------------------------------------

data "aws_vpc" "ui" {
  count = var.deploy_ui ? 1 : 0
  id    = var.ui_vpc_id
}

# -----------------------------------------------------------------------------
# Security Groups
# -----------------------------------------------------------------------------

module "ui_alb_sg" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "5.1.0"

  create = var.deploy_ui

  name        = "${var.environment}-${var.app_name}-ui-alb"
  description = "Security group for UI ALB"
  vpc_id      = var.ui_vpc_id

  # Internal ALB: VPC access only. Public ALB: anywhere (secured by OIDC)
  ingress_with_cidr_blocks = var.deploy_ui ? [
    {
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      description = var.ui_alb_internal ? "HTTP from VPC" : "HTTP from anywhere"
      cidr_blocks = var.ui_alb_internal ? data.aws_vpc.ui[0].cidr_block : "0.0.0.0/0"
    },
    {
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      description = var.ui_alb_internal ? "HTTPS from VPC" : "HTTPS from anywhere"
      cidr_blocks = var.ui_alb_internal ? data.aws_vpc.ui[0].cidr_block : "0.0.0.0/0"
    }
  ] : []

  egress_rules = ["all-all"]

  tags = {
    Purpose = "ui-alb"
  }
}

module "ui_fargate_sg" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "5.1.0"

  create = var.deploy_ui

  name        = "${var.environment}-${var.app_name}-ui-fargate"
  description = "Security group for UI Fargate tasks"
  vpc_id      = var.ui_vpc_id

  ingress_with_source_security_group_id = var.deploy_ui ? [
    {
      from_port                = 3000
      to_port                  = 3000
      protocol                 = "tcp"
      description              = "HTTP from ALB"
      source_security_group_id = module.ui_alb_sg.security_group_id
    }
  ] : []

  egress_rules = ["all-all"]

  tags = {
    Purpose = "ui-fargate"
  }
}

# -----------------------------------------------------------------------------
# Domain, ACM Certificate, and Route53 (when OIDC is configured)
# -----------------------------------------------------------------------------

locals {
  # FQDN: outcomeops.outcomeops.ai (prd) or outcomeops-dev.outcomeops.ai (dev)
  fqdn          = var.domain != "" ? (var.environment == "prd" ? "${var.ui_subdomain}.${var.domain}" : "${var.ui_subdomain}-${var.environment}.${var.domain}") : ""
  use_oidc      = var.deploy_ui && var.oidc_enabled && var.domain != ""
  oidc_ssm_path = "/${var.environment}/${var.app_name}/oidc/client-secret"
}

data "aws_route53_zone" "main" {
  count = local.use_oidc ? 1 : 0
  name  = "${var.domain}."
}

resource "aws_acm_certificate" "ui" {
  count             = local.use_oidc ? 1 : 0
  domain_name       = local.fqdn
  validation_method = "DNS"

  tags = {
    Name    = "${var.environment}-${var.app_name}-ui"
    Purpose = "ui-tls"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.use_oidc ? {
    for dvo in aws_acm_certificate.ui[0].domain_validation_options : dvo.domain_name => {
      name   = dvo.resource_record_name
      record = dvo.resource_record_value
      type   = dvo.resource_record_type
    }
  } : {}

  allow_overwrite = true
  name            = each.value.name
  records         = [each.value.record]
  ttl             = 60
  type            = each.value.type
  zone_id         = data.aws_route53_zone.main[0].zone_id
}

resource "aws_acm_certificate_validation" "ui" {
  count                   = local.use_oidc ? 1 : 0
  certificate_arn         = aws_acm_certificate.ui[0].arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

resource "aws_route53_record" "ui" {
  count   = local.use_oidc ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = local.fqdn
  type    = "A"

  alias {
    name                   = module.ui_alb.dns_name
    zone_id                = module.ui_alb.zone_id
    evaluate_target_health = true
  }
}

# -----------------------------------------------------------------------------
# OIDC Client Secret from SSM (read-only - must be created manually)
# -----------------------------------------------------------------------------

data "aws_ssm_parameter" "oidc_client_secret" {
  count = local.use_oidc ? 1 : 0
  name  = local.oidc_ssm_path
}

# -----------------------------------------------------------------------------
# Internal Application Load Balancer
# -----------------------------------------------------------------------------

module "ui_alb" {
  source  = "terraform-aws-modules/alb/aws"
  version = "9.4.0"

  create = var.deploy_ui

  name               = "${var.environment}-${var.app_name}-ui"
  load_balancer_type = "application"
  internal           = var.ui_alb_internal

  vpc_id  = var.ui_vpc_id
  subnets = var.ui_private_subnet_ids

  security_groups = var.deploy_ui ? [module.ui_alb_sg.security_group_id] : []

  # Disable access logs to save costs
  enable_deletion_protection = false

  # When OIDC is disabled, create HTTP listener via module.
  # When OIDC is enabled, listeners are created as separate resources below.
  listeners = local.use_oidc ? {} : {
    http = {
      port     = 80
      protocol = "HTTP"
      forward = {
        target_group_key = "ui"
      }
    }
  }

  target_groups = {
    ui = {
      name              = "${var.environment}-${var.app_name}-ui"
      protocol          = "HTTP"
      port              = 3000
      target_type       = "ip"
      create_attachment = false # ECS service handles attachment

      health_check = {
        enabled             = true
        healthy_threshold   = 2
        interval            = 30
        matcher             = "200"
        path                = "/health"
        port                = "traffic-port"
        protocol            = "HTTP"
        timeout             = 5
        unhealthy_threshold = 3
      }
    }
  }

  tags = {
    Purpose = "ui-internal-alb"
  }
}

# -----------------------------------------------------------------------------
# HTTP Redirect Listener (when OIDC enabled - redirects to HTTPS)
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "http_redirect" {
  count = local.use_oidc ? 1 : 0

  load_balancer_arn = module.ui_alb.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"

    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

# -----------------------------------------------------------------------------
# HTTPS Listener with OIDC Authentication (when enabled)
# -----------------------------------------------------------------------------

resource "aws_lb_listener" "https" {
  count = local.use_oidc ? 1 : 0

  load_balancer_arn = module.ui_alb.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.ui[0].certificate_arn

  default_action {
    type  = "authenticate-oidc"
    order = 1

    authenticate_oidc {
      authorization_endpoint = "https://login.microsoftonline.com/${var.oidc_tenant_id}/oauth2/v2.0/authorize"
      token_endpoint         = "https://login.microsoftonline.com/${var.oidc_tenant_id}/oauth2/v2.0/token"
      user_info_endpoint     = "https://graph.microsoft.com/oidc/userinfo"
      issuer                 = "https://login.microsoftonline.com/${var.oidc_tenant_id}/v2.0"
      client_id              = var.oidc_client_id
      client_secret          = data.aws_ssm_parameter.oidc_client_secret[0].value
      scope                  = "openid email profile"
    }
  }

  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = module.ui_alb.target_groups["ui"].arn
  }
}

# -----------------------------------------------------------------------------
# ECS Cluster + Service (Fargate Spot)
# -----------------------------------------------------------------------------

module "ui_ecs" {
  source  = "terraform-aws-modules/ecs/aws"
  version = "7.2.0"

  create = var.deploy_ui

  cluster_name = "${var.environment}-${var.app_name}-ui"

  # Fargate Spot for cost savings
  cluster_capacity_providers = ["FARGATE_SPOT"]

  default_capacity_provider_strategy = {
    FARGATE_SPOT = {
      weight = 100
      base   = 0
    }
  }

  # Disable Container Insights to save costs
  cluster_setting = [
    {
      name  = "containerInsights"
      value = "disabled"
    }
  ]

  services = {
    ui = {
      cpu    = 256 # 0.25 vCPU - minimum
      memory = 512 # 0.5 GB - minimum

      # Use Fargate Spot
      capacity_provider_strategy = {
        fargate_spot = {
          capacity_provider = "FARGATE_SPOT"
          weight            = 100
          base              = 0
        }
      }

      # Single task - keep costs minimal
      desired_count = 1

      # Allow Fargate Spot interruptions
      deployment_maximum_percent         = 200
      deployment_minimum_healthy_percent = 0

      # Container definition
      container_definitions = {
        ui = {
          essential              = true
          image                  = var.ui_container_image
          readonlyRootFilesystem = true

          portMappings = [
            {
              name          = "ui"
              containerPort = 3000
              hostPort      = 3000
              protocol      = "tcp"
            }
          ]

          environment = [
            {
              name  = "LAMBDA_FUNCTION_URL"
              value = var.deploy_ui ? aws_lambda_function_url.chat_streaming.function_url : ""
            },
            {
              name  = "AWS_REGION"
              value = var.aws_region
            }
          ]

          healthCheck = {
            command     = ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:3000/health || exit 1"]
            interval    = 30
            timeout     = 5
            retries     = 3
            startPeriod = 10
          }

          # CloudWatch Logs
          logConfiguration = {
            logDriver = "awslogs"
            options = {
              "awslogs-group"         = "/ecs/${var.environment}-${var.app_name}-ui"
              "awslogs-region"        = var.aws_region
              "awslogs-stream-prefix" = "ui"
              "awslogs-create-group"  = "true"
            }
          }
        }
      }

      # Task IAM role for Lambda Function URL invocation (SigV4 signed requests)
      tasks_iam_role_statements = [
        {
          effect = "Allow"
          actions = [
            "lambda:InvokeFunctionUrl"
          ]
          resources = [module.chat_streaming_lambda.lambda_function_arn]
        }
      ]

      # Network configuration
      subnet_ids         = var.ui_private_subnet_ids
      security_group_ids = var.deploy_ui ? [module.ui_fargate_sg.security_group_id] : []
      assign_public_ip   = true # Required for public subnets without NAT Gateway

      # Load balancer attachment
      load_balancer = {
        service = {
          target_group_arn = var.deploy_ui ? module.ui_alb.target_groups["ui"].arn : ""
          container_name   = "ui"
          container_port   = 3000
        }
      }
    }
  }

  tags = {
    Purpose = "ui-cluster"
  }
}

# -----------------------------------------------------------------------------
# Outputs
# -----------------------------------------------------------------------------

output "ui_url" {
  description = "URL to access the chat UI"
  value       = local.use_oidc ? "https://${local.fqdn}" : (var.deploy_ui ? "http://${module.ui_alb.dns_name}" : null)
}

output "ui_alb_dns_name" {
  description = "Internal ALB DNS name for chat UI (VPC access only)"
  value       = var.deploy_ui ? module.ui_alb.dns_name : null
}

output "ui_alb_arn" {
  description = "ARN of the internal ALB"
  value       = var.deploy_ui ? module.ui_alb.arn : null
}

output "ui_ecs_cluster_name" {
  description = "Name of the ECS cluster"
  value       = var.deploy_ui ? module.ui_ecs.cluster_name : null
}

output "ui_ecs_service_name" {
  description = "Name of the ECS service"
  value       = var.deploy_ui ? module.ui_ecs.services["ui"].name : null
}
