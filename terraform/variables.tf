variable "environment" {
  type        = string
  description = "Environment name (dev or prd)"
  validation {
    condition     = contains(["dev", "prd"], var.environment)
    error_message = "Environment must be 'dev' or 'prd'."
  }
}

variable "app_name" {
  type        = string
  description = "Application name"
  default     = "outcome-ops-ai-assist"
}

variable "aws_region" {
  type        = string
  description = "AWS region"
  default     = "us-west-2"
}

variable "repos_to_ingest" {
  type = list(object({
    name    = string
    project = string
    type    = string  # "application" or "standards"
  }))
  description = "List of repositories to ingest into the knowledge base"
  default = [
    {
      name    = "outcome-ops-ai-assist"
      project = "bcarpio/outcome-ops-ai-assist"
      type    = "standards"
    }
  ]
}
