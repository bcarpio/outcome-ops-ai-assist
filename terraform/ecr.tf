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

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
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

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
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
