# S3 Knowledge Base Bucket
#
# Resources:
# - module "knowledge_base_bucket" (S3 bucket for knowledge base documents)
# - aws_ssm_parameter "knowledge_base_bucket" (SSM parameter for bucket name)
#
# Versioned bucket with 30-day noncurrent version expiration.
# CORS configured for browser-based document uploads via presigned URLs.
# Optional KMS encryption, public access blocked.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
