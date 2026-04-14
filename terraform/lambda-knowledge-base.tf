# Knowledge Base Lambdas
#
# Resources:
# - module "process_batch_summary_lambda" (Process code map batch summaries from SQS)
# - aws_lambda_event_source_mapping "code_maps_queue_to_lambda" (SQS trigger, max concurrency 20)
# - module "process_repo_summary_lambda" (Generate repository architectural summaries)
# - aws_lambda_event_source_mapping "repo_summaries_queue_to_lambda" (SQS trigger, max concurrency 20)
#
# Batch summary Lambda processes code unit batches, generates embeddings
# via Bedrock Titan, and stores in S3 Vectors.
# Repo summary Lambda generates architectural summaries via Bedrock Claude.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
