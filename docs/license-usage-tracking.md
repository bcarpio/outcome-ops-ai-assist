# License Usage Tracking

OutcomeOps AI Assist tracks usage metrics for license enforcement, billing, and sales analytics. Lambdas check license limits before performing operations (repo limits for code maps and ingestion, PR limits for code generation) and report usage to the central OutcomeOps License Server via a shared Lambda layer. The system supports both hard enforcement (block at limit) and soft enforcement (alert only) depending on the customer's license tier.

## Key Features

- License enforcement checks before code map generation, document ingestion, and PR creation
- Tiered enforcement: hard limits for dev/poc/pilot tiers, soft limits (alert only) for team/division/enterprise
- Three tracked metrics: active repos (deduplicated), code map updates (summed), and PRs generated (billing)
- Threshold alerts via SES at 80% and 100% of limits to both internal support and customer contacts
- Non-blocking design: usage tracking failures never break customer workflows
- Usage data reported to central License Server via Lambda layer
- Supports sales analytics with engagement metrics (e.g., code map update frequency)

This is an enterprise component. Full documentation available under license at https://www.outcomeops.ai
