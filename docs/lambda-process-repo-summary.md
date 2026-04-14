# Lambda: Process Repo Summary

The process-repo-summary Lambda generates architectural summaries for repositories and fans out individual code units for detailed batch processing. It receives repository data from the repo-summaries FIFO queue, generates an architectural summary using Claude via Bedrock, creates embeddings, stores them in S3 Vectors, and sends code unit batches to the code-maps queue for processing by process-batch-summary.

## Key Features

- Middle step in the workspace code maps pipeline between github-sync and process-batch-summary
- Generates architectural summaries using Claude Sonnet 4.5 with Titan v2 embeddings
- Supports workspace mode (GitHub App installation tokens) and legacy mode (PAT-based)
- Automatic code unit discovery via backend abstractions for Python, TypeScript, Java, and more
- SQS-based fan-out of individual code units for parallel processing
- Exponential backoff retry for Bedrock throttling (up to 5 retries)
- Partial batch failure support with dead letter queue for permanently failed messages

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
