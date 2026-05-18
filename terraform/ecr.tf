# ============================================================================
# ECR Repository for run-tests Lambda Container Image
# ============================================================================
# This repository stores the container image for the run-tests Lambda which
# supports multiple languages (Python, Java, TypeScript, Terraform, etc.)
#
# BOOTSTRAP PROCESS:
# 1. Deploy ECR repository:
#    terraform workspace select dev
#    terraform apply -var-file=dev.tfvars -target=aws_ecr_repository.run_tests
#
# 2. Build and push container image:
#    ./scripts/build-run-tests-image.sh --env dev
#
# 3. Deploy Lambda:
#    terraform apply -var-file=dev.tfvars
#
# For production, replace 'dev' with 'prd' in all commands above.
# ============================================================================

resource "aws_ecr_repository" "run_tests" {
  name                 = "${var.environment}-${var.app_name}-run-tests"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "run-tests-lambda"
  }
}

# Lifecycle policy to clean up old images
resource "aws_ecr_lifecycle_policy" "run_tests" {
  repository = aws_ecr_repository.run_tests.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

# Output the repository URL for the build script
output "run_tests_ecr_repository_url" {
  description = "ECR repository URL for run-tests Lambda container image"
  value       = aws_ecr_repository.run_tests.repository_url
}

output "run_tests_ecr_repository_arn" {
  description = "ECR repository ARN for run-tests Lambda container image"
  value       = aws_ecr_repository.run_tests.arn
}

# ============================================================================
# ECR Repository for Chat UI Container Image
# ============================================================================
# Stores nginx + React build for Fargate deployment
#
# BUILD & PUSH:
#   ENVIRONMENT=dev make build-ui-image
#   ENVIRONMENT=prd make build-ui-image
# ============================================================================

resource "aws_ecr_repository" "chat_ui" {
  count = var.deploy_ui ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-chat-ui"
  image_tag_mutability = "MUTABLE" # Allow :latest for simpler deploys
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "chat-ui"
  }
}

resource "aws_ecr_lifecycle_policy" "chat_ui" {
  count = var.deploy_ui ? 1 : 0

  repository = aws_ecr_repository.chat_ui[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 10 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 10
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "chat_ui_ecr_repository_url" {
  description = "ECR repository URL for chat UI container image"
  value       = var.deploy_ui ? aws_ecr_repository.chat_ui[0].repository_url : null
}

output "chat_ui_ecr_repository_arn" {
  description = "ECR repository ARN for chat UI container image"
  value       = var.deploy_ui ? aws_ecr_repository.chat_ui[0].arn : null
}

# ============================================================================
# ECR Repositories for Database Integration Lambda Container Images
# ============================================================================
# The database integration ships as container images (not a Lambda layer) -
# the schema introspection engine + Postgres/MySQL/MSSQL drivers total over 350
# MB unzipped, well past Lambda's 250 MB unzipped layer cap. Container
# images go up to 10 GB.
#
# Two repos, one per Lambda:
#   - database-integration: UI-facing picker (validate connection, list schemas)
#   - database-sync:        SQS worker (full schema introspection + license-gated)
#
# Bootstrap (same pattern as run-tests):
#   ENVIRONMENT=prd make build-database-images
#     -> creates ECR repos via targeted terraform apply
#     -> builds + pushes both images
#     -> stores image tags in SSM for the main terraform apply to pick up
# ============================================================================

resource "aws_ecr_repository" "database_integration" {
  count = var.enable_workspaces ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-database-integration"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "database-integration-lambda"
  }
}

resource "aws_ecr_lifecycle_policy" "database_integration" {
  count = var.enable_workspaces ? 1 : 0

  repository = aws_ecr_repository.database_integration[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = { type = "expire" }
      }
    ]
  })
}

resource "aws_ecr_repository" "database_sync" {
  count = var.enable_workspaces ? 1 : 0

  name                 = "${var.environment}-${var.app_name}-database-sync"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = true

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = var.enable_cmk_encryption ? "KMS" : "AES256"
    kms_key         = local.data_cmk_arn
  }

  tags = {
    Purpose = "database-sync-lambda"
  }
}

resource "aws_ecr_lifecycle_policy" "database_sync" {
  count = var.enable_workspaces ? 1 : 0

  repository = aws_ecr_repository.database_sync[0].name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep last 5 images"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 5
        }
        action = { type = "expire" }
      }
    ]
  })
}

output "database_integration_ecr_repository_url" {
  description = "ECR repository URL for database-integration Lambda container image"
  value       = var.enable_workspaces ? aws_ecr_repository.database_integration[0].repository_url : null
}

output "database_sync_ecr_repository_url" {
  description = "ECR repository URL for database-sync Lambda container image"
  value       = var.enable_workspaces ? aws_ecr_repository.database_sync[0].repository_url : null
}

# ============================================================================
# ECR Cross-Region Replication (HA chunk 9a)
# ----------------------------------------------------------------------------
# When secondary_region is set, replicate the platform images from the
# primary registry to the secondary region. AWS handles syncing on every
# image push automatically; first replication for existing images can take
# 5-15 minutes per image.
#
# Replication is configured at the REGISTRY level (one config per account
# per region). The PREFIX_MATCH filter scopes replication to this app's
# repositories only, so customer images in the same account aren't
# replicated unintentionally.
#
# Verify post-apply with:
#   aws ecr describe-images --region <secondary> \
#     --repository-name <env>-<app>-chat-ui
# ============================================================================

resource "aws_ecr_replication_configuration" "ha" {
  count = var.secondary_region != "" ? 1 : 0

  replication_configuration {
    rule {
      destination {
        region      = var.secondary_region
        registry_id = data.aws_caller_identity.current.account_id
      }

      repository_filter {
        filter      = "${var.environment}-${var.app_name}-"
        filter_type = "PREFIX_MATCH"
      }
    }
  }
}
