# File Ingestion Pipeline
#
# Resources:
# - aws_sqs_queue "file_ingestion" (FIFO queue for file ingestion jobs)
# - aws_sqs_queue "file_ingestion_dlq" (Dead letter queue)
# - aws_sqs_queue "delete_queue" (Standard queue for delete operations)
# - aws_sqs_queue "delete_dlq" (Dead letter queue for deletes)
# - module "file_ingestion_lambda" (Download content from sources, upload to S3)
# - aws_lambda_event_source_mapping "file_ingestion" (SQS trigger, max concurrency 10)
# - module "process_documents_lambda" (Generate embeddings from S3 events)
# - aws_s3_bucket_notification "knowledge_base_notification" (S3 event trigger)
# - module "delete_worker_lambda" (Document and vector deletion)
# - aws_lambda_event_source_mapping "delete_worker" (SQS trigger, max concurrency 5)
#
# Conditional on enable_workspaces. Central pipeline for all document sources.
# Supports Textract OCR for scanned PDFs.
#
# Enterprise component. Full configuration available under license.
# https://www.outcomeops.ai
