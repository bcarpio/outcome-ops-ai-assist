# MCP Server Base Module
#
# Resources:
# - aws_security_group "mcp" (Security group for MCP server)
# - aws_security_group_rule "mcp_ingress" (Inbound from authorized callers)
# - aws_security_group_rule "mcp_egress" (All outbound)
# - aws_cloudwatch_log_group "mcp" (CloudWatch log group, 7-day retention)
# - aws_iam_role "execution" (ECS task execution role)
# - aws_iam_role_policy_attachment "execution_base" (ECS execution policy)
# - aws_iam_role_policy "execution_ssm" (SSM read for secrets)
# - aws_iam_role "task" (ECS task role for the container)
# - aws_service_discovery_service "mcp" (Cloud Map service discovery)
# - aws_lb_target_group "mcp" (ALB target group for routing)
# - aws_lb_listener_rule "mcp" (ALB listener rule, bypasses OIDC)
# - aws_ecs_task_definition "mcp" (Fargate task definition)
# - aws_ecs_service "mcp" (Fargate Spot ECS service)
#
# Reusable module for deploying MCP servers as Fargate Spot tasks
# with ALB routing and Cloud Map service discovery.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
