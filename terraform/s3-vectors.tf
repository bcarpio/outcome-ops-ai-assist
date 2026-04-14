# S3 Vectors Knowledge Base
#
# Resources:
# - aws_s3vectors_vector_bucket "knowledge_base" (Vector storage bucket)
# - aws_s3vectors_index "knowledge_base" (Vector index, 1024-dim cosine similarity)
# - aws_ssm_parameter "vector_bucket_name" (SSM parameter for bucket name)
# - aws_ssm_parameter "vector_index_name" (SSM parameter for index name)
#
# Native similarity search for knowledge base embeddings.
# Uses Titan Embed Text v2 (1024 dimensions), float32, cosine metric.
# Optional KMS encryption via enable_cmk_encryption.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
