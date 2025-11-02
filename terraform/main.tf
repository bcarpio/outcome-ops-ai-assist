terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
  }
}

provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      environment = var.environment
      app         = var.app_name
      managedBy   = "Terraform"
    }
  }
}

data "aws_caller_identity" "current" {}

# Store the repos allowlist in SSM Parameter Store
# This allows users to configure their allowlist via tfvars without changing code
resource "aws_ssm_parameter" "repos_allowlist" {
  name  = "/${var.environment}/${var.app_name}/config/repos-allowlist"
  type  = "String"
  value = jsonencode({
    repos = var.repos_to_ingest
  })

  description = "List of repositories to ingest into the knowledge base"

  tags = {
    Purpose = "repos-allowlist"
  }
}
