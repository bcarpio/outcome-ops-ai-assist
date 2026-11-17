# ============================================================================
# MCP Server Base Module
# Deploys a single MCP server as a Fargate Spot task with ALB routing
# and Cloud Map service discovery
# ============================================================================

locals {
  name_prefix = "${var.environment}-${var.app_name}"
  server_full = "${local.name_prefix}-mcp-${var.server_name}"
  # Prefer the explicit var.use_alb when the caller passed it (HA
  # multi-region path); fall back to the legacy "arn is non-empty"
  # derivation for single-region callers that haven't been updated.
  use_alb = var.use_alb != null ? var.use_alb : var.alb_listener_arn != ""

  # Read-only root filesystem hardening (Prowler
  # ecs_task_definitions_containers_readonly_access). Fargate does NOT support
  # linuxParameters.tmpfs, so writable scratch is provided as task-scoped
  # ephemeral volumes mounted at var.writable_paths. Map: path -> sanitized
  # volume name (Fargate volume names allow alnum / - / _ only).
  scratch_volumes = var.readonly_root_filesystem ? {
    for p in var.writable_paths : p => "scratch-${replace(trim(p, "/"), "/[^a-zA-Z0-9]+/", "-")}"
  } : {}
}

# -----------------------------------------------------------------------------
# Security Group
# -----------------------------------------------------------------------------

resource "aws_security_group" "mcp" {
  name        = "${local.server_full}-sg"
  description = "MCP server ${var.server_name}"
  vpc_id      = var.vpc_id

  tags = {
    Name    = "${local.server_full}-sg"
    Purpose = "mcp-${var.server_name}"
  }
}

resource "aws_security_group_rule" "mcp_ingress" {
  # for_each on the caller's label→SG-ID map. Keys are static strings
  # picked by the caller (e.g. "ui_alb", "mcp_outcomeops"), so the
  # instance set is known at plan time even when the SG IDs themselves
  # are computed during a -target bootstrap apply.
  for_each = var.allowed_security_group_ids

  type                     = "ingress"
  from_port                = var.container_port
  to_port                  = var.container_port
  protocol                 = "tcp"
  source_security_group_id = each.value
  security_group_id        = aws_security_group.mcp.id
  description              = "Allow inbound from ${each.key}"
}

resource "aws_security_group_rule" "mcp_egress" {
  type              = "egress"
  from_port         = 0
  to_port           = 0
  protocol          = "-1"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.mcp.id
  description       = "Allow all outbound"
}

# -----------------------------------------------------------------------------
# CloudWatch Log Group
# -----------------------------------------------------------------------------

resource "aws_cloudwatch_log_group" "mcp" {
  name              = "/ecs/${local.server_full}"
  retention_in_days = 7

  tags = {
    Purpose = "mcp-${var.server_name}"
  }
}

# -----------------------------------------------------------------------------
# IAM - Task Execution Role (pull images, read secrets, write logs)
# -----------------------------------------------------------------------------

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${local.server_full}-exec${var.role_name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json

  tags = {
    Purpose = "mcp-${var.server_name}-execution"
  }
}

resource "aws_iam_role_policy_attachment" "execution_base" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

# SSM read access for secrets
resource "aws_iam_role_policy" "execution_ssm" {
  count = length(var.secrets) > 0 ? 1 : 0

  name = "ssm-read"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = ["ssm:GetParameters"]
        Resource = values(var.secrets)
      }
    ]
  })
}

# -----------------------------------------------------------------------------
# IAM - Task Role (minimal - the container itself)
# -----------------------------------------------------------------------------

resource "aws_iam_role" "task" {
  name               = "${local.server_full}-task${var.role_name_suffix}"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json

  tags = {
    Purpose = "mcp-${var.server_name}-task"
  }
}

# -----------------------------------------------------------------------------
# Cloud Map Service Discovery
# -----------------------------------------------------------------------------

resource "aws_service_discovery_service" "mcp" {
  name = "${var.server_name}-mcp"

  dns_config {
    namespace_id = var.service_discovery_namespace_id

    dns_records {
      ttl  = 10
      type = "A"
    }

    routing_policy = "MULTIVALUE"
  }
}

# -----------------------------------------------------------------------------
# ALB Target Group and Listener Rule
# Routes traffic from ALB to MCP container, bypassing OIDC authentication.
# -----------------------------------------------------------------------------

resource "aws_lb_target_group" "mcp" {
  count = local.use_alb ? 1 : 0

  name        = "${var.environment}-mcp-${var.server_name}"
  port        = var.container_port
  protocol    = "HTTP"
  vpc_id      = var.vpc_id
  target_type = "ip"

  health_check {
    path                = "/"
    interval            = 30
    healthy_threshold   = 2
    unhealthy_threshold = 3
    matcher             = var.health_check_matcher
  }

  tags = {
    Purpose = "mcp-${var.server_name}"
  }
}

resource "aws_lb_listener_rule" "mcp" {
  count = local.use_alb ? 1 : 0

  listener_arn = var.alb_listener_arn
  priority     = var.alb_listener_rule_priority

  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.mcp[0].arn
  }

  condition {
    path_pattern {
      values = [var.mcp_path_pattern, "${var.mcp_path_pattern}/*"]
    }
  }
}

# -----------------------------------------------------------------------------
# ECS Task Definition
# -----------------------------------------------------------------------------

resource "aws_ecs_task_definition" "mcp" {
  family                   = local.server_full
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.cpu
  memory                   = var.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([
    {
      name      = var.server_name
      image     = var.container_image
      essential = true

      portMappings = [
        {
          containerPort = var.container_port
          hostPort      = var.container_port
          protocol      = "tcp"
        }
      ]

      environment = [
        for k, v in var.environment_variables : {
          name  = k
          value = v
        }
      ]

      secrets = [
        for k, v in var.secrets : {
          name      = k
          valueFrom = v
        }
      ]

      # Prowler ecs_task_definitions_containers_readonly_access: read-only root
      # with writable scratch mounted from task-scoped ephemeral volumes
      # (Fargate has no tmpfs). See local.scratch_volumes / var.writable_paths.
      readonlyRootFilesystem = var.readonly_root_filesystem

      mountPoints = [
        for path, name in local.scratch_volumes : {
          sourceVolume  = name
          containerPath = path
          readOnly      = false
        }
      ]

      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = aws_cloudwatch_log_group.mcp.name
          "awslogs-region"        = var.aws_region
          "awslogs-stream-prefix" = var.server_name
        }
      }
    }
  ])

  # Ephemeral, task-scoped writable volumes backing the read-only root's scratch
  # paths (Fargate ephemeral storage). One per var.writable_paths; none when
  # readonly_root_filesystem = false.
  dynamic "volume" {
    for_each = local.scratch_volumes
    content {
      name = volume.value
    }
  }

  tags = {
    Purpose = "mcp-${var.server_name}"
  }
}

# -----------------------------------------------------------------------------
# ECS Service (Fargate Spot)
# -----------------------------------------------------------------------------

resource "aws_ecs_service" "mcp" {
  name            = "${var.server_name}-mcp"
  cluster         = var.ecs_cluster_arn
  task_definition = aws_ecs_task_definition.mcp.arn
  desired_count   = var.desired_count

  # mixed on-demand + spot. Base=0 keeps the
  # sandbox default behaviour (all-spot); callers wanting always-on
  # tasks pass ondemand_base > 0.
  dynamic "capacity_provider_strategy" {
    for_each = var.ondemand_weight > 0 || var.ondemand_base > 0 ? [1] : []
    content {
      capacity_provider = "FARGATE"
      base              = var.ondemand_base
      weight            = var.ondemand_weight
    }
  }

  capacity_provider_strategy {
    capacity_provider = "FARGATE_SPOT"
    weight            = var.spot_weight
    base              = 0
  }

  deployment_maximum_percent         = 200
  deployment_minimum_healthy_percent = var.min_healthy_percent

  # required when capacity_provider_strategy changes.
  # ECS refuses the update otherwise ("force_new_deployment should be true
  # when capacity_provider_strategy is being updated"). Same default as the
  # terraform-aws-modules/ecs/aws service submodule. Session state lives in
  # DDB () so rolling a task is stateless-safe; the
  # min_healthy_percent = 100 guarantees an in-flight tools/call isn't
  # dropped mid-roll.
  force_new_deployment = true

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.mcp.id]
    assign_public_ip = var.assign_public_ip
  }

  service_registries {
    registry_arn = aws_service_discovery_service.mcp.arn
  }

  # ALB registration (when ALB is configured)
  dynamic "load_balancer" {
    for_each = local.use_alb ? [1] : []
    content {
      target_group_arn = aws_lb_target_group.mcp[0].arn
      container_name   = var.server_name
      container_port   = var.container_port
    }
  }

  # Ignore desired_count changes from auto-scaling
  lifecycle {
    ignore_changes = [desired_count]
  }

  tags = {
    Purpose = "mcp-${var.server_name}"
  }
}

# -----------------------------------------------------------------------------
# application autoscaling (target-tracking on
# ALBRequestCountPerTarget). Only wired when the caller opts in AND the ALB
# path is in use — Cloud Map-only deployments would need a different metric.
# Enabling this on MCP requires session state to live outside the container
# (); with in-memory sessions, a second task can't share
# state, so autoscaling would multiply session losses.
# -----------------------------------------------------------------------------

resource "aws_appautoscaling_target" "mcp" {
  count = var.enable_autoscaling && local.use_alb ? 1 : 0

  min_capacity = var.autoscaling_min_capacity
  max_capacity = var.autoscaling_max_capacity

  resource_id        = "service/${element(split("/", var.ecs_cluster_arn), 1)}/${aws_ecs_service.mcp.name}"
  scalable_dimension = "ecs:service:DesiredCount"
  service_namespace  = "ecs"
}

resource "aws_appautoscaling_policy" "mcp_alb_requests" {
  count = var.enable_autoscaling && local.use_alb ? 1 : 0

  name               = "${local.server_full}-alb-requests"
  policy_type        = "TargetTrackingScaling"
  resource_id        = aws_appautoscaling_target.mcp[0].resource_id
  scalable_dimension = aws_appautoscaling_target.mcp[0].scalable_dimension
  service_namespace  = aws_appautoscaling_target.mcp[0].service_namespace

  target_tracking_scaling_policy_configuration {
    predefined_metric_specification {
      predefined_metric_type = "ALBRequestCountPerTarget"
      # Format: app/<alb-name>/<alb-id>/targetgroup/<tg-name>/<tg-id>.
      # The caller supplies the ALB arn_suffix (up through the /<alb-id>);
      # we append the target group's arn_suffix (targetgroup/... onward).
      resource_label = "${var.alb_arn_suffix}/${aws_lb_target_group.mcp[0].arn_suffix}"
    }
    target_value       = var.autoscaling_target_requests_per_task
    scale_out_cooldown = 60
    scale_in_cooldown  = 180
  }
}
