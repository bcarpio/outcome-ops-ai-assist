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
  # fqdn is defined in locals.tf
  use_oidc      = var.deploy_ui && var.oidc_enabled && var.domain != ""
  oidc_ssm_path = "/${var.environment}/${var.app_name}/oidc/client-secret"

  # Whether terraform should manage Route53 (zone lookup, DVO records,
  # A-ALIAS). Off when DNS is managed externally.
  manage_route53 = local.use_oidc && var.manage_dns_in_route53

  # Whether terraform should request a new ACM cert (vs. using the
  # customer-provided ARN). Only meaningful when we also manage DNS,
  # because DNS validation requires writing CNAMEs into Route53.
  issue_acm_cert = local.manage_route53 && var.ui_acm_certificate_arn == ""

  # The cert ARN the listener will actually attach. Either the one we
  # just issued + validated, or the BYO ARN passed in.
  ui_cert_arn = local.issue_acm_cert ? aws_acm_certificate_validation.ui[0].certificate_arn : var.ui_acm_certificate_arn
}

data "aws_route53_zone" "main" {
  count = local.manage_route53 ? 1 : 0
  name  = "${var.domain}."
}

resource "aws_acm_certificate" "ui" {
  count             = local.issue_acm_cert ? 1 : 0
  domain_name       = local.fqdn
  validation_method = "DNS"

  tags = {
    Name    = "${var.environment}-${var.app_name}-ui"
    Purpose = "ui-tls"
  }

  lifecycle {
    create_before_destroy = true
    precondition {
      condition     = var.manage_dns_in_route53 || var.ui_acm_certificate_arn != ""
      error_message = "manage_dns_in_route53 = false requires ui_acm_certificate_arn to be set - terraform cannot DNS-validate a public ACM cert without a Route53 zone in this account."
    }
  }
}

resource "aws_route53_record" "cert_validation" {
  for_each = local.issue_acm_cert ? {
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
  count                   = local.issue_acm_cert ? 1 : 0
  certificate_arn         = aws_acm_certificate.ui[0].arn
  validation_record_fqdns = [for record in aws_route53_record.cert_validation : record.fqdn]
}

# ============================================================================
# Secondary-region ACM certificate (HA chunk 9a)
# ----------------------------------------------------------------------------
# ACM is regional; the secondary ALB (chunk 9b) needs its own cert in
# us-east-2. The cert covers BOTH the regional hostname
# (outcomeops2.${domain}) AND the canonical hostname
# (outcomeops.${domain}) — because after failover, users hit the
# canonical URL which resolves via DNS to the secondary ALB, and that
# ALB's listener needs a cert that matches the SNI hostname the
# browser sends (which is the canonical, not the regional).
#
# DNS validation records live in the primary's Route53 zone (Route53
# is global; the zone is in our primary account).
# ============================================================================

resource "aws_acm_certificate" "ui_secondary" {
  count = local.issue_acm_cert && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  domain_name = "${var.ui_subdomain}2.${var.domain}"
  subject_alternative_names = [
    local.fqdn, # canonical, so SNI from outcomeops.${domain} matches after failover
  ]
  validation_method = "DNS"

  tags = {
    Name    = "${var.environment}-${var.app_name}-ui-secondary"
    Purpose = "ui-tls-secondary"
  }

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "cert_validation_secondary" {
  for_each = local.issue_acm_cert && var.secondary_region != "" ? {
    for dvo in aws_acm_certificate.ui_secondary[0].domain_validation_options : dvo.domain_name => {
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

resource "aws_acm_certificate_validation" "ui_secondary" {
  count = local.issue_acm_cert && var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  certificate_arn         = aws_acm_certificate.ui_secondary[0].arn
  validation_record_fqdns = [for r in aws_route53_record.cert_validation_secondary : r.fqdn]
}

locals {
  # ARN of the secondary-region cert the secondary ALB listener (chunk 9b)
  # consumes. Empty when HA is disabled OR when BYO cert (no auto-issue).
  # When BYO, operator provides a secondary cert via a separate tfvar
  # (deferred until a customer actually asks for BYO + HA).
  ui_cert_arn_secondary = (
    local.issue_acm_cert && var.secondary_region != ""
    ? aws_acm_certificate_validation.ui_secondary[0].certificate_arn
    : ""
  )
}

resource "aws_route53_record" "ui" {
  count   = local.manage_route53 ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = local.fqdn
  type    = "A"

  alias {
    name                   = module.ui_alb.dns_name
    zone_id                = module.ui_alb.zone_id
    evaluate_target_health = true
  }

  lifecycle {
    # HA chunk 9a: operator-driven failover flips this record's alias
    # target between the two regional ALBs via the AWS console. Without
    # ignore_changes a subsequent terraform apply would revert the
    # operator's flip. The canonical hostname stays terraform-managed
    # at create time (initial alias points at the primary ALB);
    # mutations on the alias block are operator-only post-creation.
    ignore_changes = [alias]
  }
}

# HA chunk 9a: regional hostname for primary. Operator escape hatch
# for deep-linking to us-west-2 specifically (e.g. to verify the
# primary ALB is healthy during a failover drill). The canonical
# `outcomeops.${domain}` is what end users hit; this is admin-only.
resource "aws_route53_record" "ui_primary" {
  count   = local.manage_route53 && var.secondary_region != "" ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = "${var.ui_subdomain}1.${var.domain}"
  type    = "A"

  alias {
    name                   = module.ui_alb.dns_name
    zone_id                = module.ui_alb.zone_id
    evaluate_target_health = true
  }
}

# Secondary regional hostname is declared in 9b once
# module.ui_alb_secondary exists.

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

  # Allow headers with underscores (e.g., SONARQUBE_TOKEN for MCP servers)
  drop_invalid_header_fields = false

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
  certificate_arn   = local.ui_cert_arn

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

# ============================================================================
# Secondary-region UI ALB + listeners (HA chunk 9b)
# ----------------------------------------------------------------------------
# Mirror of the primary ALB/SG/listener block above, under the
# aws.secondary provider. Gated on var.secondary_region != "" AND
# var.ui_vpc_id_secondary != "" so 9a's outputs (ECR replication,
# Route53 scaffolding) can apply before the operator has finished
# building the secondary VPC.
#
# Target groups attach in 9h (UI Fargate ECS service) / 9i (MCP +
# scanner Fargates). Until then the ALB listener responds with a 503
# default to any traffic that lands on outcomeops2.${domain}.
# ============================================================================

locals {
  # Gate the secondary ALB on the operator having actually provisioned
  # the secondary VPC. Until then 9b's resources stay dormant so the
  # 9a apply (ECR replication / Route53 / ACM) succeeds standalone.
  deploy_ui_secondary = (
    var.deploy_ui
    && var.secondary_region != ""
    && var.ui_vpc_id_secondary != ""
  )
}

data "aws_vpc" "ui_secondary" {
  count = local.deploy_ui_secondary ? 1 : 0

  provider = aws.secondary

  id = var.ui_vpc_id_secondary
}

# OIDC client secret in secondary. Read-only data source matches the
# primary pattern (data "aws_ssm_parameter" "oidc_client_secret"):
# operator creates the SecureString in us-east-2 manually BEFORE
# applying 9b. `make ha-list-secrets` (chunk 9l) will check both
# regions. If the parameter doesn't exist, terraform plan fails with
# a clear error and the operator runs:
#   aws ssm put-parameter --region us-east-2 --type SecureString \
#     --name /${env}/${app}/oidc/client-secret --value "<value>"
data "aws_ssm_parameter" "oidc_client_secret_secondary" {
  count = local.deploy_ui_secondary && local.use_oidc ? 1 : 0

  provider = aws.secondary

  name = local.oidc_ssm_path
}

module "ui_alb_sg_secondary" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "5.1.0"

  create = local.deploy_ui_secondary

  providers = {
    aws = aws.secondary
  }

  name        = "${var.environment}-${var.app_name}-ui-alb"
  description = "Security group for UI ALB (secondary region)"
  vpc_id      = var.ui_vpc_id_secondary

  ingress_with_cidr_blocks = local.deploy_ui_secondary ? [
    {
      from_port   = 80
      to_port     = 80
      protocol    = "tcp"
      description = var.ui_alb_internal ? "HTTP from VPC" : "HTTP from anywhere"
      cidr_blocks = var.ui_alb_internal ? data.aws_vpc.ui_secondary[0].cidr_block : "0.0.0.0/0"
    },
    {
      from_port   = 443
      to_port     = 443
      protocol    = "tcp"
      description = var.ui_alb_internal ? "HTTPS from VPC" : "HTTPS from anywhere"
      cidr_blocks = var.ui_alb_internal ? data.aws_vpc.ui_secondary[0].cidr_block : "0.0.0.0/0"
    }
  ] : []

  egress_rules = ["all-all"]

  tags = {
    Purpose = "ui-alb"
  }
}

module "ui_fargate_sg_secondary" {
  source  = "terraform-aws-modules/security-group/aws"
  version = "5.1.0"

  create = local.deploy_ui_secondary

  providers = {
    aws = aws.secondary
  }

  name        = "${var.environment}-${var.app_name}-ui-fargate"
  description = "Security group for UI Fargate tasks (secondary region)"
  vpc_id      = var.ui_vpc_id_secondary

  ingress_with_source_security_group_id = local.deploy_ui_secondary ? [
    {
      from_port                = 3000
      to_port                  = 3000
      protocol                 = "tcp"
      description              = "HTTP from ALB"
      source_security_group_id = module.ui_alb_sg_secondary.security_group_id
    }
  ] : []

  egress_rules = ["all-all"]

  tags = {
    Purpose = "ui-fargate"
  }
}

module "ui_alb_secondary" {
  source  = "terraform-aws-modules/alb/aws"
  version = "9.4.0"

  create = local.deploy_ui_secondary

  providers = {
    aws = aws.secondary
  }

  name               = "${var.environment}-${var.app_name}-ui"
  load_balancer_type = "application"
  internal           = var.ui_alb_internal

  vpc_id  = var.ui_vpc_id_secondary
  subnets = var.ui_private_subnet_ids_secondary

  security_groups = local.deploy_ui_secondary ? [module.ui_alb_sg_secondary.security_group_id] : []

  drop_invalid_header_fields = false
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
      create_attachment = false # ECS service handles attachment in chunk 9h

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

resource "aws_lb_listener" "http_redirect_secondary" {
  count = local.deploy_ui_secondary && local.use_oidc ? 1 : 0

  provider = aws.secondary

  load_balancer_arn = module.ui_alb_secondary.arn
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

resource "aws_lb_listener" "https_secondary" {
  count = local.deploy_ui_secondary && local.use_oidc ? 1 : 0

  provider = aws.secondary

  load_balancer_arn = module.ui_alb_secondary.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = local.ui_cert_arn_secondary

  default_action {
    type  = "authenticate-oidc"
    order = 1

    authenticate_oidc {
      authorization_endpoint = "https://login.microsoftonline.com/${var.oidc_tenant_id}/oauth2/v2.0/authorize"
      token_endpoint         = "https://login.microsoftonline.com/${var.oidc_tenant_id}/oauth2/v2.0/token"
      user_info_endpoint     = "https://graph.microsoft.com/oidc/userinfo"
      issuer                 = "https://login.microsoftonline.com/${var.oidc_tenant_id}/v2.0"
      client_id              = var.oidc_client_id
      client_secret          = data.aws_ssm_parameter.oidc_client_secret_secondary[0].value
      scope                  = "openid email profile"
    }
  }

  default_action {
    type             = "forward"
    order            = 2
    target_group_arn = module.ui_alb_secondary.target_groups["ui"].arn
  }
}

# HA chunk 9b: regional hostname for secondary. Operator escape hatch
# for deep-linking to us-east-2 specifically (e.g. to verify the
# secondary ALB is healthy before flipping DNS). End users hit the
# canonical URL; this is admin-only.
resource "aws_route53_record" "ui_secondary" {
  count   = local.manage_route53 && local.deploy_ui_secondary ? 1 : 0
  zone_id = data.aws_route53_zone.main[0].zone_id
  name    = "${var.ui_subdomain}2.${var.domain}"
  type    = "A"

  alias {
    name                   = module.ui_alb_secondary.dns_name
    zone_id                = module.ui_alb_secondary.zone_id
    evaluate_target_health = true
  }
}

# -----------------------------------------------------------------------------
# OAuth Callback Bypass - skip OIDC for external provider redirects
# GitHub, Confluence, and Jira redirect back with code/state params that
# conflict with the ALB OIDC flow. These routes handle auth via their
# own state parameter, not the ALB session.
# -----------------------------------------------------------------------------

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
          essential                              = true
          image                                  = var.ui_container_image
          readonlyRootFilesystem                 = true
          create_cloudwatch_log_group            = true
          cloudwatch_log_group_name              = "/ecs/${var.environment}-${var.app_name}-ui"
          cloudwatch_log_group_retention_in_days = 7
          cloudwatch_log_group_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

          portMappings = [
            {
              name          = "ui"
              containerPort = 3000
              hostPort      = 3000
              protocol      = "tcp"
            }
          ]

          environment = concat([
            {
              name  = "LAMBDA_FUNCTION_URL"
              value = var.deploy_ui ? aws_lambda_function_url.chat_streaming.function_url : ""
            },
            {
              name  = "AWS_REGION"
              value = var.aws_region
            }
            ],
            # Add workspace Lambda URL only when workspaces feature is enabled
            var.enable_workspaces ? [
              {
                name  = "WORKSPACE_LAMBDA_URL"
                value = aws_lambda_function_url.workspace_management[0].function_url
              },
              {
                name  = "CONFLUENCE_LAMBDA_URL"
                value = aws_lambda_function_url.confluence_integration[0].function_url
              },
              {
                name  = "JIRA_LAMBDA_URL"
                value = aws_lambda_function_url.jira_integration[0].function_url
              },
              {
                name  = "GITHUB_LAMBDA_URL"
                value = aws_lambda_function_url.github_integration[0].function_url
              },
              {
                name  = "OUTLOOK_LAMBDA_URL"
                value = aws_lambda_function_url.outlook_integration[0].function_url
              },
              {
                name  = "TEAMS_LAMBDA_URL"
                value = aws_lambda_function_url.teams_integration[0].function_url
              },
              {
                name  = "SHAREPOINT_LAMBDA_URL"
                value = aws_lambda_function_url.sharepoint_integration[0].function_url
              },
              {
                name  = "ONEDRIVE_LAMBDA_URL"
                value = aws_lambda_function_url.onedrive_integration[0].function_url
              },
              {
                name  = "ONENOTE_LAMBDA_URL"
                value = aws_lambda_function_url.onenote_integration[0].function_url
              },
              {
                name  = "DATABASE_LAMBDA_URL"
                value = aws_lambda_function_url.database_integration[0].function_url
              }
            ] : [],
            # Add scanner JWT secret SSM path when security scanner is enabled
            local.scanner_enabled ? [
              {
                name  = "SCANNER_JWT_SECRET_SSM"
                value = aws_ssm_parameter.scanner_jwt_secret[0].name
              }
            ] : []
          )

          healthCheck = {
            command     = ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:3000/health || exit 1"]
            interval    = 30
            timeout     = 5
            retries     = 3
            startPeriod = 10
          }

        }
      }

      # Task IAM role for Lambda Function URL invocation (SigV4 signed requests).
      #
      # Single consolidated statement granting both lambda:InvokeFunctionUrl
      # and lambda:InvokeFunction on this app's Lambdas (region-scoped name
      # prefix). The dual-action + wildcard-resource shape is required in
      # us-east-2 — granting only lambda:InvokeFunctionUrl on a specific
      # function ARN returns AccessDeniedException at the Function URL
      # gateway, even though the IAM simulator reports "allowed." us-west-2
      # works with either shape; this superset is safe in both. See the
      # matching block in ui-fargate-secondary.tf for the full rationale.
      tasks_iam_role_statements = concat([
        {
          effect  = "Allow"
          actions = ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"]
          resources = [
            "arn:aws:lambda:${var.aws_region}:${data.aws_caller_identity.current.account_id}:function:${var.environment}-${var.app_name}-*"
          ]
        }
        ],
        # SSM read for scanner JWT secret when security scanner is enabled
        local.scanner_enabled ? [
          {
            effect = "Allow"
            actions = [
              "ssm:GetParameter"
            ]
            resources = [aws_ssm_parameter.scanner_jwt_secret[0].arn]
          },
          {
            effect = "Allow"
            actions = [
              "kms:Decrypt"
            ]
            resources = ["arn:aws:kms:${var.aws_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
          }
        ] : []
      )

      # Network configuration
      subnet_ids         = var.ui_private_subnet_ids
      security_group_ids = var.deploy_ui ? [module.ui_fargate_sg.security_group_id] : []
      assign_public_ip   = var.ui_fargate_assign_public_ip

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

output "alb_dns_name" {
  value       = var.deploy_ui ? module.ui_alb.dns_name : ""
  description = "DNS name of the UI ALB. CNAME local.fqdn to this in your external DNS when manage_dns_in_route53 = false."
}

output "alb_zone_id" {
  value       = var.deploy_ui ? module.ui_alb.zone_id : ""
  description = "Hosted zone ID of the UI ALB. If you're using Route53 in a different account, use this for an A-ALIAS instead of a CNAME (free + AWS-native)."
}

output "fqdn_to_cname" {
  value       = var.deploy_ui ? local.fqdn : ""
  description = "FQDN that must resolve to alb_dns_name. When manage_dns_in_route53 = true this is created automatically; when false, you create the CNAME (or cross-account R53 A-ALIAS) in your DNS system."
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
