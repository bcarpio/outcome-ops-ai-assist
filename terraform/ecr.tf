resource "aws_ecr_repository" "code_runtime" {
  name                 = "${var.environment}-${var.app_name}-runtime"
  image_tag_mutability = "MUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
  }

  tags = {
    Purpose = "code-runtime-image"
  }
}

resource "aws_ecr_lifecycle_policy" "code_runtime" {
  repository = aws_ecr_repository.code_runtime.name

  policy = jsonencode({
    rules = [
      {
        rulePriority = 1
        description  = "Keep the last 15 images, expire older ones"
        selection = {
          tagStatus   = "any"
          countType   = "imageCountMoreThan"
          countNumber = 15
        }
        action = {
          type = "expire"
        }
      }
    ]
  })
}

output "code_runtime_ecr_repository_url" {
  description = "ECR repository URL for the code runtime container image"
  value       = aws_ecr_repository.code_runtime.repository_url
}
