# Lambda: GitHub Scheduler

The github-scheduler Lambda runs hourly via EventBridge to scan DynamoDB for all workspace GitHub repositories with code maps enabled and queues a sync job for each one to the github-sync SQS queue. It drives the hourly incremental code map update cycle, ensuring workspace repositories stay current without manual intervention.

## Key Features

- Hourly EventBridge-triggered fan-out scheduler for workspace repo syncing
- Scans DynamoDB for repos with `include_code_maps=true` and active status
- Queues individual sync jobs to the github-sync SQS queue for parallel processing
- Lightweight design (256 MB memory, 60-second timeout)
- Continues processing remaining repos if individual SQS sends fail
- First step in the five-stage code maps pipeline (scheduler, sync, repo-summary, batch-summary, vectors)

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
