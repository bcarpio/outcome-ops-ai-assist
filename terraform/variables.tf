variable "environment" {
  type = string
  validation {
    condition     = can(regex("^[a-z0-9-]+$", var.environment))
    error_message = "Environment must contain only lowercase alphanumeric characters and hyphens."
  }
}

variable "app_name" {
  type    = string
  default = "outcome-ops-ai-assist"
}

variable "aws_region" {
  type = string
}

variable "github_usernames_to_tag" {
  type    = string
  default = ""
}

# ============================================================================
# Integration Toggles
# ============================================================================

variable "enable_workspaces" {
  type    = bool
  default = false
}

variable "initial_org_admin_email" {
  type    = string
  default = ""
}

variable "atlassian_oauth_client_id" {
  type    = string
  default = ""
}

variable "microsoft_oauth_client_id" {
  type    = string
  default = ""
}

variable "github_app_slug" {
  type    = string
  default = ""
}

variable "github_app_id" {
  type    = string
  default = ""
}

variable "github_client_id" {
  type    = string
  default = ""
}

variable "enable_github_issue_integration" {
  type    = bool
  default = true
}

variable "enable_jira_integration" {
  type    = bool
  default = false
}

variable "jira_external_id" {
  type    = string
  default = ""
}

# ============================================================================
# OutcomeOps License Configuration
# ============================================================================

variable "outcomeops_license_layer_arn" {
  type    = string
  default = ""
}

variable "outcomeops_license_ssm_param" {
  type    = string
  default = "/outcomeops/license/key"
}

variable "outcomeops_license_server_url" {
  type    = string
  default = "https://license.outcomeops.ai"
}

# ============================================================================
# Performance Tuning
# ============================================================================

variable "enable_bedrock_cooldown" {
  type    = bool
  default = true
}

# ============================================================================
# Bedrock Model Configuration
# ============================================================================

variable "bedrock_advanced_model_id" {
  type    = string
  default = "us.anthropic.claude-sonnet-4-6"
}

variable "bedrock_basic_model_id" {
  type    = string
  default = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
}

variable "bedrock_embedding_model_id" {
  type    = string
  default = "amazon.titan-embed-text-v2:0"
}

variable "bedrock_rerank_model_id" {
  type    = string
  default = "cohere.rerank-v3-5:0"
}

variable "rerank_max_fetch" {
  type    = number
  default = 90
}

variable "rerank_min_score" {
  type    = number
  default = 0.15
}

variable "bedrock_advanced_input_price" {
  type    = number
  default = 3.0
}

variable "bedrock_advanced_output_price" {
  type    = number
  default = 15.0
}

variable "bedrock_basic_input_price" {
  type    = number
  default = 0.80
}

variable "bedrock_basic_output_price" {
  type    = number
  default = 4.0
}

variable "rag_max_doc_chars" {
  type    = number
  default = 3000
}

# ============================================================================
# Resource Tags
# ============================================================================

variable "tags" {
  type    = map(string)
  default = {}
}

# ============================================================================
# Chat UI Deployment (Fargate + Internal ALB)
# ============================================================================

variable "deploy_ui" {
  type    = bool
  default = false
}

variable "ui_vpc_id" {
  type    = string
  default = ""
}

variable "ui_private_subnet_ids" {
  type    = list(string)
  default = []
}

variable "ui_container_image" {
  type    = string
  default = ""
}

variable "ui_alb_internal" {
  type    = bool
  default = false
}

variable "ui_fargate_assign_public_ip" {
  type    = bool
  default = false
}

# ============================================================================
# OIDC Authentication (Azure AD)
# ============================================================================

variable "domain" {
  type    = string
  default = ""
}

variable "cors_allowed_origins" {
  type    = list(string)
  default = ["http://localhost:*"]
}

variable "ui_subdomain" {
  type    = string
  default = "outcomeops"
}

variable "oidc_enabled" {
  type    = bool
  default = false
}

variable "oidc_client_id" {
  type    = string
  default = ""
}

variable "oidc_tenant_id" {
  type    = string
  default = ""
}

# ============================================================================
# MCP Server Catalog
# ============================================================================

variable "mcp_sonarqube_enabled" {
  type    = bool
  default = false
}

variable "mcp_sonarqube_url" {
  type    = string
  default = ""
}

variable "mcp_sonarqube_org" {
  type    = string
  default = ""
}

variable "mcp_snyk_enabled" {
  type    = bool
  default = false
}

variable "mcp_snyk_org" {
  type    = string
  default = ""
}

variable "mcp_server_enabled" {
  type    = bool
  default = false
}

# ============================================================================
# Audit & Compliance
# ============================================================================

variable "audit_alert_email" {
  type    = string
  default = ""
}

# ============================================================================
# Security Scanner (AWS Security Agent)
# ============================================================================

variable "security_scanner_enabled" {
  type    = bool
  default = false
}

variable "security_scanner_email" {
  type    = string
  default = "security-scanner@outcomeops.local"
}

# ============================================================================
# Encryption (Customer Managed Keys)
# ============================================================================

variable "enable_cmk_encryption" {
  type    = bool
  default = false
}
