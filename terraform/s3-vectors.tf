# ============================================================================
# S3 Vectors for Knowledge Base Vector Storage
# Replaces DynamoDB for vector storage with native similarity search
# ============================================================================

resource "aws_s3vectors_vector_bucket" "knowledge_base" {
  vector_bucket_name = "${var.environment}-${var.app_name}-vectors"

  encryption_configuration {
    sse_type = "AES256"
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
