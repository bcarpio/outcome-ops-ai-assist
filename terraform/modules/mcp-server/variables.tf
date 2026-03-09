# ============================================================================
# MCP Server Base Module - Variables
# Deploys a single MCP server as a Fargate Spot task with ALB routing
# ============================================================================

variable "environment" {
  type        = string
  description = "Environment name (dev or prd)"
}

variable "app_name" {
  type        = string
  description = "Application name"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
}

variable "server_name" {
  type        = string
  description = "MCP server name (e.g., sonarqube). Used in resource naming."
}

variable "container_image" {
  type        = string
  description = "Docker image for the MCP server"
}

variable "container_port" {
  type        = number
  description = "Port the MCP server listens on"
  default     = 8080
}

variable "cpu" {
  type        = number
  description = "CPU units for the Fargate task (256 = 0.25 vCPU)"
  default     = 512
}

variable "memory" {
  type        = number
  description = "Memory in MB for the Fargate task"
  default     = 1024
}

variable "environment_variables" {
  type        = map(string)
  description = "Environment variables for the MCP server container"
  default     = {}
}

variable "secrets" {
  type        = map(string)
  description = "Secrets to inject: env var name -> SSM parameter ARN"
  default     = {}
}

# ECS Cluster
variable "ecs_cluster_arn" {
  type        = string
  description = "ARN of the existing ECS cluster"
}

# Networking
variable "vpc_id" {
  type        = string
  description = "VPC ID"
}

variable "private_subnet_ids" {
  type        = list(string)
  description = "Subnet IDs for the Fargate task"
}

variable "assign_public_ip" {
  type        = bool
  description = "Assign public IP to Fargate task"
  default     = false
}

# Service Discovery
variable "service_discovery_namespace_id" {
  type        = string
  description = "Cloud Map private DNS namespace ID"
}

variable "service_discovery_namespace_name" {
  type        = string
  description = "Cloud Map namespace DNS name (e.g., mcp.local)"
}

# ALB Integration
variable "alb_listener_arn" {
  type        = string
  description = "ARN of the ALB listener for MCP routing (bypasses OIDC)"
  default     = ""
}

variable "alb_listener_rule_priority" {
  type        = number
  description = "Priority for the ALB listener rule"
  default     = 100
}

variable "mcp_path_pattern" {
  type        = string
  description = "ALB path pattern for routing to this MCP server"
  default     = "/mcp"
}

variable "health_check_matcher" {
  type        = string
  description = "HTTP response codes for ALB health check (MCP servers may return non-200 on GET)"
  default     = "200-499"
}

# Security
variable "allowed_security_group_ids" {
  type        = list(string)
  description = "Security group IDs allowed to reach this MCP server"
  default     = []
}
