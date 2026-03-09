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

output "security_group_id" {
  description = "Security group ID of this MCP server"
  value       = aws_security_group.mcp.id
}

output "service_name" {
  description = "ECS service name"
  value       = aws_ecs_service.mcp.name
}
