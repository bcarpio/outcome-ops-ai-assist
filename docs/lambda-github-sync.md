# Lambda: GitHub Sync

The github-sync Lambda processes SQS messages to sync GitHub repositories for workspace-scoped code map generation and document ingestion. It authenticates via GitHub App installation tokens, fetches the repository file tree, and routes based on repository type: application repos go to the code maps pipeline, while standards repos go to the document ingestion pipeline.

## Key Features

- Workspace-scoped GitHub repository synchronization via GitHub App installation tokens
- Intelligent routing: application repos to code map generation, standards repos to document ingestion
- Change detection using commit SHA comparison to skip redundant syncs
- File filtering by extension, directory, and size for efficient processing
- Sync status tracking in DynamoDB with success/failure state and error details
- Triggered by both the hourly scheduler and webhook events on repo addition
- SQS-based processing with automatic retry and dead letter queue

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
