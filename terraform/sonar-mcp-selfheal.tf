# -----------------------------------------------------------------------------
# SonarQube MCP self-heal (ops workaround)
#
# The third-party SonarQube MCP server shuts down its Apache HttpClient I/O
# reactor during idle (IOReactorShutdownException) and never recreates it, so
# every SonarCloud call fast-fails. The container stays RUNNING and the ALB `/`
# health check returns 200, so nothing self-heals. This detects the reactor
# death via a CloudWatch Logs subscription filter and force-redeploys the sonar
# service (debounced). Not a fix -- report upstream to SonarSource. Gated on the
# same var as the sonar server itself.
# -----------------------------------------------------------------------------

locals {
  sonar_selfheal_name = "${var.environment}-${var.app_name}-sonar-mcp-selfheal"
  ui_cluster_name     = "${var.environment}-${var.app_name}-ui"
  sonar_service_arn   = "arn:aws:ecs:${var.aws_region}:${data.aws_caller_identity.current.account_id}:service/${local.ui_cluster_name}/sonarqube-mcp"
}

module "sonar_selfheal_lambda" {
  count   = var.mcp_sonarqube_enabled ? 1 : 0
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"

  function_name = local.sonar_selfheal_name
  description   = "Force-redeploy the SonarQube MCP service on IOReactorShutdown (self-heal workaround)"
  handler       = "handler.handler"
  runtime       = "python3.12"
  timeout       = 30
  memory_size   = 128

  cloudwatch_logs_retention_in_days = 14
  cloudwatch_logs_kms_key_id        = aws_kms_key.cloudwatch_logs.arn

  source_path = [{
    path             = "${path.module}/../lambda/sonar-mcp-selfheal"
    pip_requirements = false
  }]
  quiet_archive_local_exec = true

  environment_variables = {
    CLUSTER          = local.ui_cluster_name
    SERVICE          = "sonarqube-mcp"
    DEBOUNCE_SECONDS = "600"
  }

  attach_policy_statements = true
  policy_statements = {
    # DescribeServices has no resource-level scoping; UpdateService is scoped to
    # the sonar service. forceNewDeployment is the only mutation used.
    describe_services = {
      effect    = "Allow"
      actions   = ["ecs:DescribeServices"]
      resources = ["*"]
    }
    restart_sonar = {
      effect    = "Allow"
      actions   = ["ecs:UpdateService"]
      resources = [local.sonar_service_arn]
    }
  }

  # Allow the sonar container log group's subscription filter to invoke us.
  allowed_triggers = {
    LogsSubscription = {
      principal  = "logs.amazonaws.com"
      source_arn = "${module.mcp_sonarqube[0].log_group_arn}:*"
    }
  }

  tags = { Purpose = "sonar-mcp-selfheal" }
}

resource "aws_cloudwatch_log_subscription_filter" "sonar_reactor_death" {
  count           = var.mcp_sonarqube_enabled ? 1 : 0
  name            = "sonar-mcp-ioreactor-shutdown"
  log_group_name  = module.mcp_sonarqube[0].log_group_name
  filter_pattern  = "IOReactorShutdownException"
  destination_arn = module.sonar_selfheal_lambda[0].lambda_function_arn
}

# Secondary-region parity (HA): intentionally primary-only today. The secondary
# sonar service (module.mcp_sonarqube_secondary in fargate-secondary.tf) only
# exists when HA is enabled (currently off), and its log group + self-heal must
# live in the secondary region under the aws.secondary provider. When HA is
# re-enabled, add a mirror of this file's Lambda + subscription filter there.
