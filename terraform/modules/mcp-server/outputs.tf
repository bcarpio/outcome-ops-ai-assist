# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
# ============================================================================
# MCP Server Base Module - Outputs
# ============================================================================

output "service_discovery_dns" {
  description = "Cloud Map DNS name for this MCP server"
  value       = "${aws_service_discovery_service.mcp.name}.${var.service_discovery_namespace_name}"
}

output "service_url" {
  description = "Full URL to reach the MCP server's Streamable HTTP endpoint"
  value       = "http://${aws_service_discovery_service.mcp.name}.${var.service_discovery_namespace_name}:${var.container_port}/mcp"
}

output "alb_url" {
  description = "ALB-routed URL for the MCP server (empty if ALB not configured)"
  value       = local.use_alb ? "${var.mcp_path_pattern}" : ""
}

output "security_group_id" {
  description = "Security group ID of this MCP server"
  value       = aws_security_group.mcp.id
}

output "service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.mcp.name
}

output "target_group_arn" {
  description = "ALB target group ARN (empty if ALB not configured)"
  value       = local.use_alb ? aws_lb_target_group.mcp[0].arn : ""
}

output "task_role_name" {
  description = "Name of the ECS task IAM role"
  value       = aws_iam_role.task.name
}

output "task_role_arn" {
  description = "ARN of the ECS task IAM role"
  value       = aws_iam_role.task.arn
}

output "log_group_name" {
  description = "CloudWatch log group name for this MCP server's container"
  value       = aws_cloudwatch_log_group.mcp.name
}

output "log_group_arn" {
  description = "CloudWatch log group ARN for this MCP server's container"
  value       = aws_cloudwatch_log_group.mcp.arn
}
