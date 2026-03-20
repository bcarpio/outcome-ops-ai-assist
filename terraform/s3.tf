module "knowledge_base_bucket" {
  source  = "terraform-aws-modules/s3-bucket/aws"
  version = "4.1.1"

  bucket = "${var.environment}-${var.app_name}-kb-${data.aws_caller_identity.current.account_id}"

  # Versioning for recoverability
  versioning = {
    enabled = true
  }

  # Lifecycle rules to manage storage costs
  lifecycle_rule = [
    {
      id     = "expire-old-versions"
      status = "Enabled"
      noncurrent_version_expiration = {
        days = 30
      }
      abort_incomplete_multipart_upload_days = 7
    }
  ]

  # Enforce encryption (CMK when enabled, AES256 otherwise)
  server_side_encryption_configuration = {
    rule = {
      apply_server_side_encryption_by_default = {
        sse_algorithm     = var.enable_cmk_encryption ? "aws:kms" : "AES256"
        kms_master_key_id = local.data_cmk_arn
      }
      bucket_key_enabled = var.enable_cmk_encryption
    }
  }

  # Block public access
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true

  # CORS configuration for browser-based document uploads via presigned URLs
  cors_rule = [
    {
      allowed_headers = ["*"]
      allowed_methods = ["PUT", "POST"]
      allowed_origins = concat(
        var.domain != "" ? ["https://*.${var.domain}"] : [],
        var.cors_allowed_origins
      )
      expose_headers  = ["ETag"]
      max_age_seconds = 3600
    }
  ]

  tags = {
    Purpose = "knowledge-base"
  }
}

resource "aws_ssm_parameter" "knowledge_base_bucket" {
  name  = "/${var.environment}/${var.app_name}/s3/knowledge-base-bucket"
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
