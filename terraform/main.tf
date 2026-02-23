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
    tags = merge(
      {
        Environment = var.environment
        Application = var.app_name
        ManagedBy   = "Terraform"
      },
      var.tags
    )
  }
}

data "aws_caller_identity" "current" {}

# Store GitHub usernames to tag when tests fail
# Supports teams by accepting comma-separated usernames
resource "aws_ssm_parameter" "github_usernames_to_tag" {
  name        = "/${var.environment}/${var.app_name}/config/github-usernames-to-tag"
  type        = "String"
  value       = var.github_usernames_to_tag
  description = "Comma-separated GitHub usernames to tag when tests fail (supports teams)"

  tags = {
    Purpose = "github-usernames-to-tag"
  }
}
