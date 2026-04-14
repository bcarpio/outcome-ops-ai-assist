# SQS Queues
#
# Resources:
# - module "code_maps_dlq" (Dead letter queue for batch processing)
# - module "code_maps_queue" (Standard queue for code maps batch processing)
# - aws_ssm_parameter "code_maps_queue_url" (SSM parameter for queue URL)
# - module "pr_checks_dlq" (FIFO dead letter queue for PR checks)
# - module "pr_checks_queue" (FIFO queue for PR check jobs)
# - aws_ssm_parameter "pr_checks_queue_url" (SSM parameter for queue URL)
# - module "code_generation_dlq" (FIFO dead letter queue for code generation)
# - module "code_generation_queue" (FIFO queue for code generation steps)
# - aws_ssm_parameter "code_generation_queue_url" (SSM parameter for queue URL)
# - module "repo_summaries_dlq" (FIFO dead letter queue for repo summaries)
# - module "repo_summaries_queue" (FIFO queue for architectural summaries)
# - aws_ssm_parameter "repo_summaries_queue_url" (SSM parameter for queue URL)
#
# All queues use optional KMS encryption via enable_cmk_encryption.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
