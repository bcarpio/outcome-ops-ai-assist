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

  # Gate the entire module on count rather than only the upstream
  # module's `create = ...` flag. With `create = false` terraform
  # still evaluates the module's input expressions (including the
  # `aws_kms_key.cloudwatch_logs_secondary[0]` / `aws_lambda_function_url.*_secondary[0]`
  # references in container_definitions below) and explodes with
  # "Invalid index" when those count-gated resources are empty
  # tuples -- which is what happens the instant `secondary_region = ""`.
  # Adding `count` here skips the entire block at evaluation time so
  # the secondary teardown plans cleanly. Consumers
  # (`fargate-secondary.tf:106,289`) reference
  # `module.ui_ecs_secondary.cluster_arn` from within modules already
  # gated on `local.deploy_ui_secondary`, so they won't dereference
  # the now-empty list either.
  count = local.deploy_ui_secondary ? 1 : 0

  create = local.deploy_ui_secondary

  providers = {
    aws = aws.secondary
  }

  cluster_name = "${var.environment}-${var.app_name}-ui"

  # fable-scale chunk 01 (HA parity): mixed capacity providers, same
  # weights as primary. See ui-fargate.tf for the full comment.
  cluster_capacity_providers = ["FARGATE", "FARGATE_SPOT"]

  default_capacity_provider_strategy = {
    FARGATE = {
      base   = var.ui_fargate_ondemand_base
      weight = var.ui_fargate_ondemand_weight
    }
    FARGATE_SPOT = {
      base   = 0
      weight = var.fargate_enable_spot ? var.ui_fargate_spot_weight : 0
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
      # fable-scale chunk 01 (HA parity): tfvars-driven sizing +
      # autoscaling, same shape as primary.
      cpu    = var.ui_fargate_cpu
      memory = var.ui_fargate_memory

      capacity_provider_strategy = {
        fargate = {
          capacity_provider = "FARGATE"
          base              = var.ui_fargate_ondemand_base
          weight            = var.ui_fargate_ondemand_weight
        }
        fargate_spot = {
          capacity_provider = "FARGATE_SPOT"
          base              = 0
          weight            = var.fargate_enable_spot ? var.ui_fargate_spot_weight : 0
        }
      }

      desired_count = var.ui_fargate_desired_count

      deployment_maximum_percent         = 200
      deployment_minimum_healthy_percent = var.ui_fargate_min_healthy_percent

      # fable-scale chunk 01 (HA parity): target-tracking autoscaling on
      # ALBRequestCountPerTarget for the secondary ALB + target group.
      enable_autoscaling       = true
      autoscaling_min_capacity = var.ui_fargate_min_count
      autoscaling_max_capacity = var.ui_fargate_max_count
      autoscaling_policies = {
        alb_request_count = {
          policy_type = "TargetTrackingScaling"
          target_tracking_scaling_policy_configuration = {
            predefined_metric_specification = {
              predefined_metric_type = "ALBRequestCountPerTarget"
              resource_label         = local.deploy_ui_secondary ? "${module.ui_alb_secondary.arn_suffix}/${module.ui_alb_secondary.target_groups["ui"].arn_suffix}" : ""
            }
            target_value       = var.ui_fargate_target_requests_per_task
            scale_out_cooldown = 60
            scale_in_cooldown  = 180
          }
        }
      }

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
              # try(...) wraps every per-integration Function URL so a
              # disabled integration's absent aws_lambda_function_url[0]
              # doesn't blow up plan (see the primary-region file for
              # the fuller comment).
              {
                name  = "CONFLUENCE_LAMBDA_URL"
                value = try(aws_lambda_function_url.confluence_integration_secondary[0].function_url, "")
              },
              {
                name  = "JIRA_LAMBDA_URL"
                value = try(aws_lambda_function_url.jira_integration_secondary[0].function_url, "")
              },
              {
                name  = "GITHUB_LAMBDA_URL"
                value = try(aws_lambda_function_url.github_integration_secondary[0].function_url, "")
              },
              {
                name  = "AZURE_DEVOPS_LAMBDA_URL"
                value = try(aws_lambda_function_url.azure_devops_integration_secondary[0].function_url, "")
              },
              {
                name  = "AZURE_DEVOPS_BOARDS_LAMBDA_URL"
                value = try(aws_lambda_function_url.azure_devops_boards_integration_secondary[0].function_url, "")
              },
              {
                name  = "OUTLOOK_LAMBDA_URL"
                value = try(aws_lambda_function_url.outlook_integration_secondary[0].function_url, "")
              },
              {
                name  = "TEAMS_LAMBDA_URL"
                value = try(aws_lambda_function_url.teams_integration_secondary[0].function_url, "")
              },
              {
                name  = "SHAREPOINT_LAMBDA_URL"
                value = try(aws_lambda_function_url.sharepoint_integration_secondary[0].function_url, "")
              },
              {
                name  = "ONEDRIVE_LAMBDA_URL"
                value = try(aws_lambda_function_url.onedrive_integration_secondary[0].function_url, "")
              },
              {
                name  = "ONENOTE_LAMBDA_URL"
                value = try(aws_lambda_function_url.onenote_integration_secondary[0].function_url, "")
              },
              {
                name  = "DATABASE_LAMBDA_URL"
                value = try(aws_lambda_function_url.database_integration_secondary[0].function_url, "")
              },
              {
                name  = "SERVICENOW_LAMBDA_URL"
                value = try(aws_lambda_function_url.servicenow_integration_secondary[0].function_url, "")
              },
              {
                name  = "SALESFORCE_LAMBDA_URL"
                value = try(aws_lambda_function_url.salesforce_integration_secondary[0].function_url, "")
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
          # Lambda Function URL invokes (single consolidated statement).
          #
          # us-east-2 evaluates Function URL authorization differently than
          # us-west-2: granting lambda:InvokeFunctionUrl on a SPECIFIC
          # function ARN returns AccessDeniedException at the URL gateway,
          # even though the IAM simulator reports "allowed." Two changes
          # are required for us-east-2 to authorize:
          #   1. Both lambda:InvokeFunctionUrl AND lambda:InvokeFunction
          #      must be granted (the gateway internally re-checks via
          #      Invoke).
          #   2. The Resource must use a region-scoped wildcard (the
          #      application's name prefix is sufficient); explicit
          #      function ARNs and `function:NAME*` suffixes both fail.
          # us-west-2 works with either shape; the broader pattern below
          # is a superset so it's safe in both regions. Bisected empirically.
          # The primary's task role (ui-fargate.tf) uses the same shape for
          # symmetry and to defend against this behavior rolling out to
          # other regions.
          {
            effect  = "Allow"
            actions = ["lambda:InvokeFunctionUrl", "lambda:InvokeFunction"]
            resources = [
              "arn:aws:lambda:${var.secondary_region}:${data.aws_caller_identity.current.account_id}:function:${var.environment}-${var.app_name}-*"
            ]
          }
        ],
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
