# ============================================================================
# Secondary-region UI Fargate ECS (HA chunk 9h)
# ----------------------------------------------------------------------------
# Mirror of module.ui_ecs under aws.secondary. After this commit:
#
# - A Fargate Spot service runs in us-east-2 serving the chat UI on
#   outcomeops2.${domain} (and on outcomeops.${domain} post-DNS-flip).
# - Container env vars point at SECONDARY Function URLs so calls stay
#   region-local.
# - Task IAM role grants lambda:InvokeFunctionUrl on the SECONDARY
#   Lambda ARNs only.
# - Image: from secondary-region ECR (replicated via chunk 9a's
#   aws_ecr_replication_configuration), or var.ui_container_image_secondary
#   if explicitly set.
#
# Gated on local.deploy_ui_secondary (already defined in chunk 9b)
# which requires deploy_ui + secondary_region + ui_vpc_id_secondary.
# ============================================================================

locals {
  # Image for the secondary UI Fargate. Default: rewrite the region
  # substring of the primary image URI so the secondary pulls the
  # replicated copy from its own region's ECR. Override via
  # var.ui_container_image_secondary if the operator needs a
  # different digest in secondary (e.g. canary deploys).
  ui_container_image_for_secondary = (
    var.ui_container_image_secondary != ""
    ? var.ui_container_image_secondary
    : replace(var.ui_container_image, var.aws_region, var.secondary_region)
  )
}

module "ui_ecs_secondary" {
  source  = "terraform-aws-modules/ecs/aws"
  version = "7.2.0"

  create = local.deploy_ui_secondary

  providers = {
    aws = aws.secondary
  }

  cluster_name = "${var.environment}-${var.app_name}-ui"

  cluster_capacity_providers = ["FARGATE_SPOT"]

  default_capacity_provider_strategy = {
    FARGATE_SPOT = {
      weight = 100
      base   = 0
    }
  }

  cluster_setting = [
    {
      name  = "containerInsights"
      value = "disabled"
    }
  ]

  services = {
    ui = {
      cpu    = 256
      memory = 512

      capacity_provider_strategy = {
        fargate_spot = {
          capacity_provider = "FARGATE_SPOT"
          weight            = 100
          base              = 0
        }
      }

      desired_count = 1

      deployment_maximum_percent         = 200
      deployment_minimum_healthy_percent = 0

      container_definitions = {
        ui = {
          essential                              = true
          image                                  = local.ui_container_image_for_secondary
          readonlyRootFilesystem                 = true
          create_cloudwatch_log_group            = true
          cloudwatch_log_group_name              = "/ecs/${var.environment}-${var.app_name}-ui"
          cloudwatch_log_group_retention_in_days = 7
          cloudwatch_log_group_kms_key_id        = aws_kms_key.cloudwatch_logs_secondary[0].arn

          portMappings = [
            {
              name          = "ui"
              containerPort = 3000
              hostPort      = 3000
              protocol      = "tcp"
            }
          ]

          environment = concat(
            [
              {
                name  = "LAMBDA_FUNCTION_URL"
                value = aws_lambda_function_url.chat_streaming_secondary[0].function_url
              },
              {
                name  = "AWS_REGION"
                value = var.secondary_region
              },
              # UI banner (chunk 9k) reads THIS_REGION to decide whether
              # to render the passive-mode banner.
              {
                name  = "THIS_REGION"
                value = var.secondary_region
              },
            ],
            var.enable_workspaces ? [
              {
                name  = "WORKSPACE_LAMBDA_URL"
                value = aws_lambda_function_url.workspace_management_secondary[0].function_url
              },
              {
                name  = "CONFLUENCE_LAMBDA_URL"
                value = aws_lambda_function_url.confluence_integration_secondary[0].function_url
              },
              {
                name  = "JIRA_LAMBDA_URL"
                value = aws_lambda_function_url.jira_integration_secondary[0].function_url
              },
              {
                name  = "GITHUB_LAMBDA_URL"
                value = aws_lambda_function_url.github_integration_secondary[0].function_url
              },
              {
                name  = "OUTLOOK_LAMBDA_URL"
                value = aws_lambda_function_url.outlook_integration_secondary[0].function_url
              },
              {
                name  = "TEAMS_LAMBDA_URL"
                value = aws_lambda_function_url.teams_integration_secondary[0].function_url
              },
              {
                name  = "SHAREPOINT_LAMBDA_URL"
                value = aws_lambda_function_url.sharepoint_integration_secondary[0].function_url
              },
              {
                name  = "ONEDRIVE_LAMBDA_URL"
                value = aws_lambda_function_url.onedrive_integration_secondary[0].function_url
              },
              {
                name  = "ONENOTE_LAMBDA_URL"
                value = aws_lambda_function_url.onenote_integration_secondary[0].function_url
              },
              {
                name  = "DATABASE_LAMBDA_URL"
                value = aws_lambda_function_url.database_integration_secondary[0].function_url
              }
            ] : [],
            # Scanner secondary lands in chunk 9i; SSM path stays the
            # same so this env var resolves once 9i populates the param.
            local.scanner_enabled ? [
              {
                name  = "SCANNER_JWT_SECRET_SSM"
                value = "/${var.environment}/${var.app_name}/scanner/jwt-secret"
              }
            ] : [],
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

      tasks_iam_role_statements = concat(
        [
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.chat_streaming_lambda_secondary[0].lambda_function_arn]
          }
        ],
        var.enable_workspaces ? [
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.workspace_management_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.confluence_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.jira_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.github_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.outlook_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.teams_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.sharepoint_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.onedrive_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.onenote_integration_lambda_secondary[0].lambda_function_arn]
          },
          {
            effect    = "Allow"
            actions   = ["lambda:InvokeFunctionUrl"]
            resources = [module.database_integration_lambda_secondary[0].lambda_function_arn]
          }
        ] : [],
        # Scanner JWT SSM access — secondary copy of the SSM param ships
        # in chunk 9i. Grant by ARN-pattern since the resource may not
        # exist yet when this Fargate applies; chunk 9i landing fills it.
        local.scanner_enabled ? [
          {
            effect    = "Allow"
            actions   = ["ssm:GetParameter"]
            resources = ["arn:aws:ssm:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:parameter/${var.environment}/${var.app_name}/scanner/jwt-secret"]
          },
          {
            effect    = "Allow"
            actions   = ["kms:Decrypt"]
            resources = ["arn:aws:kms:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:alias/aws/ssm"]
          }
        ] : [],
      )

      subnet_ids         = var.ui_private_subnet_ids_secondary
      security_group_ids = local.deploy_ui_secondary ? [module.ui_fargate_sg_secondary.security_group_id] : []
      assign_public_ip   = var.ui_fargate_assign_public_ip

      load_balancer = {
        service = {
          target_group_arn = local.deploy_ui_secondary ? module.ui_alb_secondary.target_groups["ui"].arn : ""
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

# Outputs for operator visibility
output "ui_alb_dns_name_secondary" {
  description = "Secondary-region UI ALB DNS name (null when HA disabled)"
  value       = local.deploy_ui_secondary ? module.ui_alb_secondary.dns_name : null
}

output "ui_url_secondary" {
  description = "URL to access the secondary chat UI (deep-link / admin escape hatch)"
  value       = local.deploy_ui_secondary && local.use_oidc ? "https://${var.ui_subdomain}2.${var.domain}" : null
}
