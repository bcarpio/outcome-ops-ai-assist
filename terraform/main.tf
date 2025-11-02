terraform {
  required_version = ">= 1.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
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
