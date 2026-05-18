# ============================================================================
# S3 Vectors for Knowledge Base Vector Storage
# Replaces DynamoDB for vector storage with native similarity search
# ============================================================================

resource "aws_s3vectors_vector_bucket" "knowledge_base" {
  vector_bucket_name = "${var.environment}-${var.app_name}-vectors"

  encryption_configuration {
    sse_type    = var.enable_cmk_encryption ? "aws:kms" : "AES256"
    kms_key_arn = local.data_cmk_arn
  }

  tags = {
    Purpose = "knowledge-base-vectors"
  }
}

resource "aws_s3vectors_index" "knowledge_base" {
  vector_bucket_name = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name
  index_name         = "knowledge-base"

  # Data type for vectors
  data_type = "float32"

  # Titan Embed Text v2 outputs 1024 dimensions
  dimension = 1024

  # Cosine similarity for semantic search
  distance_metric = "cosine"

  # Metadata fields that don't need filtering (reduces index size)
  # Content is large and never filtered on, only returned
  metadata_configuration {
    non_filterable_metadata_keys = ["content", "file_path", "content_hash", "timestamp"]
  }

  tags = {
    Purpose = "knowledge-base-index"
  }
}

# Store bucket name in SSM for Lambda access
resource "aws_ssm_parameter" "vector_bucket_name" {
  name  = "/${var.environment}/${var.app_name}/s3vectors/bucket-name"
  type  = "String"
  value = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name

  tags = {
    Purpose = "vector-bucket-config"
  }
}

resource "aws_ssm_parameter" "vector_index_name" {
  name  = "/${var.environment}/${var.app_name}/s3vectors/index-name"
  type  = "String"
  value = aws_s3vectors_index.knowledge_base.index_name

  tags = {
    Purpose = "vector-index-config"
  }
}

output "vector_bucket_name" {
  description = "Name of the S3 Vector bucket"
  value       = aws_s3vectors_vector_bucket.knowledge_base.vector_bucket_name
}

output "vector_index_name" {
  description = "Name of the vector index"
  value       = aws_s3vectors_index.knowledge_base.index_name
}

# ============================================================================
# Secondary-region copy (HA mode only).
# S3 Vectors is regional. Bucket name is unique per region, so the same
# name is fine in the secondary region. If the chosen secondary_region
# doesn't support S3 Vectors, terraform apply fails loudly here — the
# operator picks a different secondary region or disables HA.
# ============================================================================

resource "aws_s3vectors_vector_bucket" "knowledge_base_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  vector_bucket_name = "${var.environment}-${var.app_name}-vectors"

  encryption_configuration {
    sse_type    = var.enable_cmk_encryption ? "aws:kms" : "AES256"
    kms_key_arn = local.data_cmk_arn_secondary
  }

  tags = {
    Purpose = "knowledge-base-vectors"
  }
}

resource "aws_s3vectors_index" "knowledge_base_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  vector_bucket_name = aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name
  index_name         = "knowledge-base"

  data_type       = "float32"
  dimension       = 1024
  distance_metric = "cosine"

  metadata_configuration {
    non_filterable_metadata_keys = ["content", "file_path", "content_hash", "timestamp"]
  }

  tags = {
    Purpose = "knowledge-base-index"
  }
}

resource "aws_ssm_parameter" "vector_bucket_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/s3vectors/bucket-name"
  type  = "String"
  value = aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name

  tags = {
    Purpose = "vector-bucket-config"
  }
}

resource "aws_ssm_parameter" "vector_index_name_secondary" {
  count = var.secondary_region != "" ? 1 : 0

  provider = aws.secondary

  name  = "/${var.environment}/${var.app_name}/s3vectors/index-name"
  type  = "String"
  value = aws_s3vectors_index.knowledge_base_secondary[0].index_name

  tags = {
    Purpose = "vector-index-config"
  }
}

output "vector_bucket_name_secondary" {
  description = "Name of the secondary-region S3 Vector bucket (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_s3vectors_vector_bucket.knowledge_base_secondary[0].vector_bucket_name : null
}

output "vector_index_name_secondary" {
  description = "Name of the secondary-region vector index (null when HA disabled)"
  value       = var.secondary_region != "" ? aws_s3vectors_index.knowledge_base_secondary[0].index_name : null
}
