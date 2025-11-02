# FantacyAI OutcomeOps Assistant

An AI-powered engineering assistant for myfantasy.ai that shifts from task-oriented to outcome-oriented development. This system ingests your codebase patterns, architectural decisions, and conventions into a knowledge base, then leverages Claude to generate code that matches your exact practices.

## Purpose

Instead of manually coding every feature, the OutcomeOps assistant:
1. **Ingests your patterns** - ADRs, READMEs, code conventions, test fixtures
2. **Generates contextual code** - Lambda handlers, Terraform, tests that match YOUR standards
3. **Validates outcomes** - You review for business logic, the system ensures technical consistency
4. **Improves iteratively** - Each pattern you refine updates the knowledge base

This system empowers you to move faster as a solo developer by automating the "how" while you focus on the "why."

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Claude Code Interface                        │
│                    (User Stories via Chat)                       │
└──────────────────────────┬──────────────────────────────────────┘
                           │
                ┌──────────┴──────────┐
                │                     │
                ▼                     ▼
        ┌───────────────┐    ┌──────────────────┐
        │  Query KB     │    │  Generate Code   │
        │  (RAG Search) │    │  (Code Maps)     │
        └───────┬───────┘    └────────┬─────────┘
                │                     │
                └──────────┬──────────┘
                           ▼
                   ┌───────────────┐
                   │  AWS Bedrock  │
                   │  + Claude 3.5 │
                   └───────┬───────┘
                           │
        ┌──────────────────┼──────────────────┐
        │                  │                  │
        ▼                  ▼                  ▼
    ┌────────┐        ┌─────────────┐   ┌─────────┐
    │Vector  │        │ DynamoDB    │   │    S3   │
    │Search  │        │(Embeddings) │   │(Docs)   │
    └────────┘        └─────────────┘   └─────────┘
```

### Core Components

**Knowledge Base Ingestion:**
- Scans your ADRs and codebase via GitHub API
- Generates embeddings using Bedrock Titan v2
- Stores in DynamoDB for semantic search

**Code Map Generation:**
- Analyzes repository structure and patterns
- Groups related files by type (handlers, schemas, infrastructure)
- Generates Claude summaries of architectural intent

**RAG Pipeline (Retrieval Augmented Generation):**
- Accepts natural language queries
- Vector searches knowledge base for relevant patterns
- Returns Claude-generated answers grounded in YOUR conventions

**CLI Tool (`fantacyai-assist`):**
- Query knowledge base from terminal: `fantacyai-assist "How should error handling work?"`
- See sources and reasoning directly

---

## Tech Stack

| Component | Technology | Notes |
|-----------|-----------|-------|
| **Compute** | AWS Lambda (Python 3.12) | Serverless functions |
| **Knowledge Base** | AWS Bedrock (Titan v2 + Claude) | Embeddings + LLM |
| **Vector Storage** | DynamoDB | Single-table with embeddings |
| **Document Storage** | S3 | ADRs, READMEs, code artifacts |
| **Messaging** | SQS FIFO | Batch processing queue |
| **Infrastructure** | Terraform | IaC for all AWS resources |
| **Source Control** | GitHub API | Read-only repo access |
| **CI/CD** | GitHub Actions | Automated deployments |

---

## Development Setup

### Prerequisites

1. **Python 3.12+**
   ```bash
   pyenv install 3.12
   pyenv local 3.12
   ```

2. **Terraform 1.5+**
   ```bash
   brew install terraform
   ```

3. **GitHub Personal Access Token** (for repo access)
   ```bash
   # Create at: https://github.com/settings/tokens
   # Scopes needed: repo (full control)
   ```

### Local Setup

1. **Clone and initialize:**
   ```bash
   git clone git@github.com:bcarpio/fantacyai-ai-assist.git
   cd fantacyai-ai-assist
   python -m venv venv
   source venv/bin/activate
   pip install -r requirements.txt
   ```

2. **Environment configuration:**
   ```bash
   cp .env.example .env
   # Edit .env with:
   # - GITHUB_TOKEN=your-token
   # - AWS_REGION=us-east-1
   ```

3. **Deploy infrastructure:**
   ```bash
   cd terraform
   terraform init
   terraform workspace new dev
   terraform plan -var-file=dev.tfvars
   terraform apply -var-file=dev.tfvars
   ```

---

## Project Structure

```
fantacyai-ai-assist/
├── docs/
│   ├── adr/                      # Architecture Decision Records
│   │   ├── ADR-001-kb-design.md
│   │   ├── ADR-002-code-generation.md
│   │   └── ...
│   ├── architecture.md           # System architecture details
│   └── deployment.md             # Deployment runbook
├── src/
│   ├── handlers/                 # Lambda functions
│   │   ├── ingest-kb/           # Ingest ADRs and codebase
│   │   ├── vector-query/        # Semantic search
│   │   ├── ask-claude/          # RAG answer generation
│   │   ├── query-kb/            # Query orchestration
│   │   ├── generate-code-maps/  # Code analysis and summaries
│   │   └── process-batch/       # Async batch processing
│   ├── shared/
│   │   ├── utils/               # Helper functions
│   │   ├── models/              # Data models and schemas
│   │   └── tests/               # Unit tests
│   └── config/
│       ├── allowlist.yaml       # Repos to ingest
│       └── schemas.yaml         # Data validation schemas
├── terraform/
│   ├── main.tf                  # Core infrastructure
│   ├── variables.tf
│   ├── outputs.tf
│   ├── dev.tfvars               # Dev environment
│   ├── prd.tfvars               # Production environment
│   └── modules/
│       ├── lambda/
│       ├── dynamodb/
│       └── s3/
├── scripts/
│   ├── fantacyai-assist         # CLI tool (main entry point)
│   ├── ingest-repos.sh          # Manual ingestion script
│   └── deploy.sh                # Deployment helper
├── tests/
│   ├── unit/
│   ├── integration/
│   └── fixtures/
├── requirements.txt
├── .env.example
├── .gitignore
└── README.md
```

---

## Core Features

### 1. Knowledge Base Ingestion

Automatically ingest patterns from your codebase:

```bash
# Manual trigger
aws lambda invoke \
  --function-name dev-fantacyai-ingest-kb \
  response.json
```

**What gets ingested:**
- ADRs from `docs/adr/` (all markdown files)
- READMEs from repository roots and key directories
- Code patterns from Lambda handlers
- Terraform module examples
- Test fixtures and schemas

**Stored in DynamoDB as:**
```json
{
  "PK": "repo#fantacyai-api-aws",
  "SK": "doc#adr-error-handling",
  "type": "adr",
  "content": "...",
  "embedding": [0.123, 0.456, ...],
  "source": "docs/adr/ADR-001-error-handling.md",
  "repo": "fantacyai-api-aws"
}
```

---

### 2. Code Map Generation

Analyze your repositories to understand architectural patterns:

```bash
# Generate code maps for all repos (or just changed ones)
aws lambda invoke \
  --function-name dev-fantacyai-generate-code-maps \
  --payload '{"repos": ["fantacyai-api-aws"]}' \
  response.json
```

**Generates:**
- Architectural summaries (directory structure + intent)
- Batch summaries (groups of related files)
- File relationship analysis
- Pattern identification

**Example output stored in DynamoDB:**
```json
{
  "PK": "repo#fantacyai-api-aws",
  "SK": "summary#architecture",
  "type": "code-map",
  "content": "This repository implements the myfantasy.ai backend...",
  "embedding": [0.789, 0.012, ...],
  "fileCount": 145,
  "timestamp": "2025-01-15T10:00:00Z"
}
```

---

### 3. Query Knowledge Base

Ask questions about your patterns via CLI or API:

```bash
# CLI query
fantacyai-assist "How should Lambda error handling work?"

# Output:
# 🤖 Querying knowledge base...
#
# 📚 Answer:
# Lambda handlers should follow the error handling pattern defined in ADR-001...
#
# 📖 Sources:
#   - ADR: error-handling-standards
#   - Code map: handler-patterns
```

**Works by:**
1. Generating query embedding via Bedrock Titan v2
2. Vector searching DynamoDB for similar patterns
3. Passing top results to Claude 3.5 Sonnet
4. Returning grounded answer with citations

---

### 4. Code Generation

Guide Claude to generate code using your patterns:

```bash
# In Claude Code (Claude's native IDE):
# 1. Chat: "Create a new Lambda handler for listing users following ADR-001"
# 2. Claude queries knowledge base for your handler patterns
# 3. Claude generates handler code that matches YOUR conventions
# 4. You review the outcome (business logic), not the implementation
```

Claude can:
- Generate Lambda handler boilerplate with error handling
- Create Terraform infrastructure modules
- Write test fixtures matching your patterns
- Generate database schemas and migrations

---

## Architecture Decision Records (ADRs)

Store your architectural decisions in `docs/adr/`:

```markdown
# ADR-001: Error Handling in Lambda Functions

## Status: Accepted

## Context
Lambda handlers need consistent error handling across the platform.

## Decision
All handlers will use a standard error wrapper that:
- Catches exceptions
- Logs to CloudWatch with correlation IDs
- Returns consistent error format
- Includes user-friendly messages

## Consequences
- Handlers are more maintainable
- Debugging is faster with structured logs
- Errors are consistent across platform

## Example Implementation
[Link to working handler example]
```

As you build myfantasy.ai, document decisions here. The knowledge base ingests these automatically.

---

## ADR Ingestion Pipeline

ADRs flow into the knowledge base:

```
docs/adr/*.md
     ↓
[ingest-kb Lambda]
     ↓
Generate embedding via Bedrock Titan v2
     ↓
Store in DynamoDB with metadata
     ↓
Available for queries + code generation
```

---

## Deployment

### Dev Environment

```bash
cd terraform
terraform workspace select dev
terraform plan -var-file=dev.tfvars
terraform apply -var-file=dev.tfvars
```

### Production Environment

```bash
cd terraform
terraform workspace select prd
terraform plan -var-file=prd.tfvars
terraform apply -var-file=prd.tfvars
```

### CI/CD Pipeline

GitHub Actions automatically:
1. Validates Terraform syntax
2. Runs security scans (Trivy, Snyk)
3. Plans infrastructure changes
4. Applies on main branch (with manual approval for prod)

---

## Environment Configuration

### Required Parameters

Create `dev.tfvars` and `prd.tfvars`:

```hcl
aws_region           = "us-east-1"
environment          = "dev"
app_name             = "fantacyai-ai-assist"

# GitHub access
github_token_ssm_path = "/fantacyai/dev/github-token"

# Bedrock models
bedrock_embedding_model = "amazon.titan-embed-text-v2:0"
bedrock_claude_model    = "anthropic.claude-3-5-sonnet-20241022"

# Repository allowlist
repos_to_ingest = [
  {
    name    = "fantacyai-api-aws"
    owner   = "bcarpio"
    type    = "application"  # or "standards" for ADR-only repos
  },
  {
    name    = "fantacyai-analytics"
    owner   = "bcarpio"
    type    = "application"
  }
]
```

### Secrets Management

Store sensitive data in AWS Secrets Manager:

```bash
aws secretsmanager create-secret \
  --name fantacyai/dev/github-token \
  --secret-string "your-github-token"
```

Lambda functions retrieve at runtime.

---

## Monitoring

### CloudWatch Logs

All Lambda functions log to CloudWatch:
- `dev-fantacyai-ingest-kb`: Ingestion events
- `dev-fantacyai-vector-query`: Search queries
- `dev-fantacyai-ask-claude`: RAG generations
- `dev-fantacyai-generate-code-maps`: Code analysis

### CloudWatch Alarms

Set up alerts for:
- Lambda error rates
- DynamoDB throttling
- SQS queue depth
- Bedrock API errors

```bash
# View recent errors
aws logs tail /aws/lambda/dev-fantacyai-ask-claude --follow
```

---

## Testing

### Unit Tests

```bash
pip install pytest pytest-cov moto boto3
pytest tests/unit --cov=src
```

### Integration Tests

```bash
pytest tests/integration -v
```

### Manual Testing

Test the full RAG pipeline:

```bash
# 1. Ingest knowledge
aws lambda invoke --function-name dev-fantacyai-ingest-kb response.json

# 2. Generate code maps
aws lambda invoke --function-name dev-fantacyai-generate-code-maps response.json

# 3. Query knowledge base
fantacyai-assist "How should I structure a new Lambda handler?"
```

---

## Solo Developer Workflow

As the solo developer, here's the recommended flow:

1. **Define outcome in Claude Code:**
   ```
   "Create a Lambda handler for user profile updates that:
    - Validates input with Zod
    - Updates DynamoDB following our patterns
    - Returns consistent error format
    - Includes proper logging"
   ```

2. **Claude queries knowledge base:**
   - Searches for error handling patterns (ADR-001)
   - Finds similar handler examples from code maps
   - Retrieves validation schema standards

3. **Claude generates code:**
   - Creates handler matching your conventions
   - Includes tests following your fixtures
   - Adds Terraform for deployment

4. **You review for:**
   - Business logic correctness
   - Outcome achievement
   - Any patterns you want to refine

5. **Refine patterns if needed:**
   - Update ADR if decision changes
   - Update code map if structure shifts
   - Commit and redeploy

This way, you focus on architecture and outcomes. The system handles consistency.

---

## Common Tasks

### Query Knowledge Base from Terminal

```bash
fantacyai-assist "What's our standard for error handling?"
fantacyai-assist "How should I structure Terraform for a new service?"
fantacyai-assist "Show me examples of database query patterns"
```

### Add a New Repository to Ingest

1. Add to `src/config/allowlist.yaml`:
   ```yaml
   repos:
     - name: fantacyai-analytics
       owner: bcarpio
       type: application
   ```

2. Redeploy Lambda:
   ```bash
   cd terraform && terraform apply
   ```

3. Trigger ingestion manually or wait for scheduled run

### Create a New ADR

1. Create file in `docs/adr/ADR-NNN-title.md`
2. Follow the template in docs/adr/TEMPLATE.md
3. Commit to main
4. Next ingest cycle picks it up automatically

### Update an Existing Pattern

1. Update the ADR or code
2. Commit to main
3. Trigger code map regeneration:
   ```bash
   aws lambda invoke \
     --function-name dev-fantacyai-generate-code-maps \
     --payload '{"repos": ["fantacyai-api-aws"]}' \
     response.json
   ```

---

## Troubleshooting

### Knowledge base returns no results

1. Check ingestion completed:
   ```bash
   aws logs tail /aws/lambda/dev-fantacyai-ingest-kb --follow
   ```

2. Verify documents in DynamoDB:
   ```bash
   aws dynamodb scan \
     --table-name dev-fantacyai-kb \
     --limit 10
   ```

3. Re-trigger ingestion if needed

### Code generation feels off-pattern

1. Check if code map is current:
   ```bash
   aws lambda invoke \
     --function-name dev-fantacyai-generate-code-maps \
     --payload '{"repos": ["fantacyai-api-aws"]}' \
     response.json
   ```

2. Review and update relevant ADRs for clarity

3. Add code examples to ADRs showing expected patterns

### Bedrock API throttling

The system uses SQS FIFO queue to prevent throttling:
- Each batch processes sequentially
- Max 100k tokens per minute respected
- Failed batches go to DLQ for retry

Check queue depth:
```bash
aws sqs get-queue-attributes \
  --queue-url <queue-url> \
  --attribute-names ApproximateNumberOfMessages
```

---

## Contributing & Extending

### Adding New Lambda Functions

1. Create handler in `src/handlers/my-handler/`
2. Add Terraform in `terraform/handlers/`
3. Add tests in `tests/unit/handlers/`
4. Update allowlist if needed
5. Deploy via Terraform

### Modifying Knowledge Base Schema

1. Update `src/config/schemas.yaml`
2. Update DynamoDB table schema in Terraform
3. Re-ingest data if schema changed
4. Test with existing queries

---

## Architecture Principles

1. **Knowledge drives generation** - The better your ADRs and code maps, the better Claude generates code
2. **Patterns matter** - Consistent patterns = better code generation
3. **You own the outcome** - Claude handles consistency, you own business logic
4. **Iterate together** - Each pattern you refine improves future generations
5. **Stay grounded** - RAG ensures answers reference your actual patterns

---

## Next Steps

1. Set up dev environment (follow Development Setup)
2. Deploy infrastructure (`terraform apply`)
3. Start with ADRs for your key decisions
4. Test knowledge base with queries
5. Use in Claude Code for code generation

---

## Resources

- **ADR Template**: `docs/adr/TEMPLATE.md`
- **Architecture Details**: `docs/architecture.md`
- **Deployment Runbook**: `docs/deployment.md`
- **API Documentation**: [Link to Bedrock docs]
- **Claude 3.5 Sonnet**: [Link to model docs]

---

## Support

- GitHub Issues for bugs and feature requests
- ADRs for architectural questions
- Code generation feedback via Claude Code interface

---

**Built for solo developer velocity at myfantasy.ai.** 🚀

Cloud infrastructure by code. Engineering outcomes by Claude. Your vision, automated execution.
