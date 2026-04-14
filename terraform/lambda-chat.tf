# Chat Streaming Lambda
#
# Resources:
# - module "chat_streaming_lambda" (Lambda with LWA for streaming chat responses)
# - aws_lambda_function_url "chat_streaming" (Function URL with IAM auth, RESPONSE_STREAM mode)
# - aws_ssm_parameter "chat_streaming_url" (SSM parameter for streaming URL)
#
# Uses Lambda Web Adapter for HTTP streaming via Function URL.
# Integrates with Bedrock (Claude, Titan embeddings, Cohere Rerank),
# DynamoDB (chat, audit, workspaces), S3 Vectors for KB search,
# and conversation memory.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
