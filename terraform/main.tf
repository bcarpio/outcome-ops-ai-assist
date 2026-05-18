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
        Region      = "primary"
      },
      var.tags
    )
  }
}

# Secondary-region provider for HA active/passive deployments.
# When var.secondary_region is "", this alias points at the primary
# region — resources gated on var.secondary_region != "" simply
# won't be created, so the alias never gets used. Defining it
# unconditionally lets downstream modules reference
# `providers = { aws = aws.secondary }` without conditional plumbing.
provider "aws" {
  alias  = "secondary"
  region = var.secondary_region != "" ? var.secondary_region : var.aws_region

  default_tags {
    tags = merge(
      {
        Environment = var.environment
        Application = var.app_name
        ManagedBy   = "Terraform"
        Region      = "secondary"
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
