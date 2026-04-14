# RAG Query Lambdas
#
# Resources:
# - module "ask_claude_lambda" (Generate RAG answers using Claude via Bedrock)
# - aws_ssm_parameter "ask_claude_lambda_arn" (SSM parameter for Lambda ARN)
# - module "query_kb_lambda" (Orchestrate RAG pipeline: S3 Vectors search + Claude)
#
# Two-stage RAG: query-kb performs vector search via S3 Vectors and
# Bedrock Rerank, then invokes ask-claude for answer generation.
# Supports both advanced (Sonnet) and basic (Haiku) models.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
