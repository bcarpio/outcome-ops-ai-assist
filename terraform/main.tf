# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source = "hashicorp/aws"
      # Pinned to 6.36.0 -- the version RetrieveIT ran the ADR-016
      # inference-profile rollout on. Provider versions 6.53.x (auto-
      # picked from ~> 6.0 on init day for this repo) regressed
      # aws_bedrock_inference_profile authentication and made
      # CreateInferenceProfile expect a Bedrock bearer token; SigV4
      # credentials that work in the CLI fail with
      # "Authentication failed: Please make sure your API Key is valid."
      # Bump both this and RetrieveIT together when a later 6.x release
      # is confirmed to fix the regression.
      version = "6.36.0"
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
