# Lambda: Process Batch Summary

The process-batch-summary Lambda consumes code map batch messages from SQS, fetches file contents from GitHub, generates detailed summaries using Claude via Bedrock, creates embeddings with Titan v2, and stores them in S3 Vectors for semantic search during code generation. It handles different batch types (handlers, infrastructure, tests, schemas, shared code, docs) with specialized prompts tailored to each.

## Key Features

- SQS-driven processing with automatic retry and dead letter queue for failed messages
- Specialized summary prompts per batch type, including detailed debugging documentation for handler groups
- Embedding generation via Bedrock Titan v2 (1024 dimensions) stored in S3 Vectors
- Smart file handling: skips files over 50KB, truncates to 10KB per file
- Exponential backoff retry for Bedrock API throttling and transient errors
- Content deduplication via SHA-256 hashing
- Partial batch failure support using the ReportBatchItemFailures pattern

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
