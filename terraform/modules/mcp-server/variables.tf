# ============================================================================
# MCP Server Base Module - Variables
# Deploys a single MCP server as a Fargate Spot task with Cloud Map discovery
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

# Security
variable "allowed_security_group_ids" {
  type        = list(string)
  description = "Security group IDs allowed to reach this MCP server"
  default     = []
}
