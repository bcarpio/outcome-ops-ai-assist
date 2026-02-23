# Feature: Cost Guardrails

---

## Overview

Prevent budget overruns for AI-assisted code generation through pre-generation estimation and real-time tracking.

## Core Functionality

**Pre-Generation Validation:**
- Estimate token usage and cost before generation starts
- Block stories exceeding per-story limit (default: $5)
- Warn when approaching monthly budget (default: $500)

**Cost Tracking:**
- Log actual token usage and cost per generation to DynamoDB
- Track monthly spend totals
- Alert when budget thresholds crossed (default: 80%)

**Cost Data Storage:**
```
DynamoDB table: generation-costs
PK: MONTH#{YYYY-MM}
SK: STORY#{story-id}#{timestamp}
Attributes: cost, tokens_input, tokens_output, repository, team, user_email
```

## Configuration

**SSM Parameters:**
- `/{env}/{app}/guardrails/cost/per-story-limit` (default: $5.00)
- `/{env}/{app}/guardrails/cost/monthly-budget` (default: $500.00)
- `/{env}/{app}/guardrails/cost/warning-threshold` (default: 0.80)

## Implementation

**Lambda: validate-story** - Estimates cost, enforces limits, approves/rejects generation

**Lambda: track-generation-cost** - Logs actual costs after generation completes
