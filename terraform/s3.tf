module "knowledge_base_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "4.1.1"

  bucket = "${var.environment}-${var.app_name}-ai-assist-kb"

  # Versioning for recoverability
  versioning = {
    enabled = true
  }

  # Lifecycle rules to manage storage costs
  lifecycle_rule = [
    {
      id      = "expire-old-versions"
      status  = "Enabled"
      noncurrent_version_expiration = {
        days = 30
      }
      abort_incomplete_multipart_upload_days = 7
    }
  ]

  # Enforce encryption
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm = "AES256"
      }
    }
  }

  # Block public access
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  tags = {
    Purpose = "knowledge-base"
  }
}

resource "aws_ssm_parameter" "knowledge_base_bucket" {
  name  = "/${var.environment}/${var.app_name}-ai-assist/s3/knowledge-base-bucket"
  type  = "String"
  value = module.knowledge_base_bucket.s3_bucket_id

  tags = {
    Purpose = "knowledge-base-bucket-name"
  }
}

output "knowledge_base_bucket_name" {
  description = "Name of the knowledge base S3 bucket"
  value       = module.knowledge_base_bucket.s3_bucket_id
}

output "knowledge_base_bucket_arn" {
  description = "ARN of the knowledge base S3 bucket"
  value       = module.knowledge_base_bucket.s3_bucket_arn
}
