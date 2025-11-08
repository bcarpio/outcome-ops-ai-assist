# Technical Reference

Complete technical documentation for OutcomeOps AI Assist system internals, deployment, and operations.

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Core Features](#core-features)
- [Environment Configuration](#environment-configuration)
- [Deployment](#deployment)
- [Monitoring](#monitoring)
- [Testing](#testing)
- [Solo Developer Workflow](#solo-developer-workflow)
- [Common Tasks](#common-tasks)
- [Troubleshooting](#troubleshooting)
- [Contributing & Extending](#contributing--extending)
- [Architecture Principles](#architecture-principles)

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

## Project Structure

```
outcome-ops-ai-assist/
├── docs/
│   ├── adr/                           # Architecture Decision Records
│   │   ├── ADR-001-create-adrs.md    # ADR pattern and template
│   │   └── TEMPLATE.md               # Template for new ADRs
│   ├── lambda-ingest-docs.md         # Ingest Lambda documentation
│   ├── lambda-generate-code-maps.md  # Code maps Lambda documentation
│   ├── lambda-analyze-pr.md          # PR analysis Lambda documentation
│   ├── architecture.md               # System architecture & design
│   ├── deployment.md                 # Deployment & operations guide
│   └── README.md                     # This docs directory overview
├── lambda/
│   ├── ingest-docs/                 # Lambda: Ingest ADRs/READMEs/Docs
│   │   ├── handler.py               # Main ingestion handler
│   │   ├── requirements.txt         # Python dependencies
│   │   └── tests/                   # Unit tests for this Lambda
│   ├── generate-code-maps/          # Lambda: Generate code maps
│   │   ├── handler.py               # Main code map generation handler
│   │   ├── backends/                # Pluggable backend abstraction
│   │   │   ├── base.py              # Abstract base classes
│   │   │   ├── factory.py           # Backend registry and factory
│   │   │   └── lambda_backend.py    # Lambda serverless backend
│   │   ├── state_tracker.py         # State persistence for incremental updates
│   │   └── requirements.txt         # Python dependencies
│   ├── analyze-pr/                  # Lambda: GitHub PR analysis orchestration
│   │   ├── handler.py               # Main PR analysis handler
│   │   └── requirements.txt         # Python dependencies
│   ├── process-pr-check/            # Lambda: PR check worker (SQS consumer)
│   │   ├── handler.py               # Main check processing handler
│   │   ├── check_handlers/          # Check handler implementations
│   │   │   ├── __init__.py          # Package exports
│   │   │   ├── adr_compliance.py    # ADR compliance check
│   │   │   ├── architectural_duplication.py  # Duplication detection
│   │   │   ├── breaking_changes.py  # Dependency detection
│   │   │   ├── readme_freshness.py  # README adequacy check
│   │   │   └── test_coverage.py     # Test file verification
│   │   └── requirements.txt         # Python dependencies
│   └── tests/
│       ├── unit/                    # Unit tests for all Lambdas
│       ├── integration/             # Integration tests
│       ├── fixtures/                # Test fixtures and sample data
│       ├── conftest.py              # Pytest configuration
│       └── Makefile                 # Test execution targets
├── terraform/
│   ├── main.tf                      # Core infrastructure
│   ├── lambda.tf                    # Lambda module configuration
│   ├── dynamodb.tf                  # DynamoDB table
│   ├── s3.tf                        # S3 knowledge base bucket
│   ├── variables.tf                 # Variable definitions
│   ├── backend.tf                   # Remote state configuration
│   ├── dev.tfvars                   # Dev environment (in .gitignore)
│   ├── prd.tfvars                   # Prod environment (in .gitignore)
│   └── .tfvars.example              # Template for tfvars files
├── Makefile                         # Build orchestration
├── .gitignore                       # Git ignore patterns
└── README.md                        # Project overview
```

---

## Core Features

### 1. Knowledge Base Ingestion

Automatically ingest patterns from your codebase:

```bash
# Manual trigger
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-ingest-docs \
  response.json
```

**What gets ingested:**
- **ADRs** from `docs/adr/` (Architecture Decision Records)
- **READMEs** from repository roots
- **Function-specific docs** from `docs/lambda-*.md` (avoids chunking large files)
- **Architecture docs** from `docs/architecture.md`
- **Deployment docs** from `docs/deployment.md`

**Stored in DynamoDB with embeddings** for semantic search and RAG pipelines.

**Full details:** See [lambda-ingest-docs.md](lambda-ingest-docs.md)

---

### 2. Code Map Generation

Analyze your repositories to extract architectural patterns and code organization using a **pluggable backend abstraction**.

**Backend Architecture:**
- **Pluggable design**: Supports multiple architecture types (Lambda, K8s, monolith)
- **Lambda Serverless Backend**: Discovers Lambda handlers, infrastructure, frontend, tests, and documentation
- **Backend Factory**: Registry-based pattern for easy backend instantiation
- **State Tracking**: Incremental updates via git-based change detection
- **Extensible**: Add new backends by implementing the CodeMapBackend interface

**Currently Supported:**
- **Lambda serverless**: Analyzes AWS Lambda architectures (current implementation)
- **Kubernetes**: Coming soon
- **Monolith**: Coming soon

**Full details:** See [lambda-generate-code-maps.md](lambda-generate-code-maps.md)

---

### 3. Query Knowledge Base

Ask questions about your patterns via CLI:

```bash
outcome-ops-assist "How should Lambda error handling work?"
outcome-ops-assist "What are our Terraform module standards?"
outcome-ops-assist "Show me examples of DynamoDB patterns"
```

**How it works:**
1. Generates query embedding via Bedrock Titan v2
2. Vector searches DynamoDB for similar patterns
3. Passes top results to Claude 3.5 Sonnet
4. Returns grounded answer with source citations

**See:** [CLI Usage Guide](cli-usage.md) for installation, options, examples, and troubleshooting

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

As you build your project, document decisions here. The knowledge base ingests these automatically.

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

## Environment Configuration

### Required Parameters

Create `dev.tfvars` and `prd.tfvars`:

```hcl
aws_region           = "us-east-1"
environment          = "dev"
app_name             = "outcome-ops-ai-assist"

# GitHub access
github_token_ssm_path = "/outcome-ops/dev/github-token"

# Bedrock models
bedrock_embedding_model = "amazon.titan-embed-text-v2:0"
bedrock_claude_model    = "anthropic.claude-3-5-sonnet-20241022"

# Repository allowlist
repos_to_ingest = [
  {
    name    = "outcome-ops-ai-assist"
    owner   = "bcarpio"
    type    = "application"  # or "standards" for ADR-only repos
  },
  {
    name    = "outcome-ops-analytics"
    owner   = "bcarpio"
    type    = "application"
  }
]
```

### Secrets Management

Store sensitive data in AWS Secrets Manager:

```bash
aws secretsmanager create-secret \
  --name outcome-ops/dev/github-token \
  --secret-string "your-github-token"
```

Lambda functions retrieve at runtime.

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

## Monitoring

### CloudWatch Logs

All Lambda functions log to CloudWatch:
- `dev-outcome-ops-ai-assist-ingest-docs`: Ingestion events
- `dev-outcome-ops-ai-assist-vector-query`: Search queries
- `dev-outcome-ops-ai-assist-ask-claude`: RAG generations
- `dev-outcome-ops-ai-assist-generate-code-maps`: Code analysis
- `dev-outcome-ops-ai-assist-analyze-pr`: PR analysis orchestration
- `dev-outcome-ops-ai-assist-process-pr-check`: PR check worker (SQS consumer)

### CloudWatch Alarms

Set up alerts for:
- Lambda error rates
- DynamoDB throttling
- SQS queue depth
- Bedrock API errors

```bash
# View recent errors
aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ask-claude --follow
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
aws lambda invoke --function-name dev-outcome-ops-ai-assist-ingest-docs response.json

# 2. Generate code maps
aws lambda invoke --function-name dev-outcome-ops-ai-assist-generate-code-maps response.json

# 3. Query knowledge base
outcome-ops-assist "How should I structure a new Lambda handler?"
```

---

## Solo Developer Workflow

As the solo developer, here's the recommended flow:

1. **Define outcome in Claude Code:**
   ```
   "Create a Lambda handler for user profile updates that:
    - Validates input with Pydantic
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
outcome-ops-assist "What's our standard for error handling?"
outcome-ops-assist "How should I structure Terraform for a new service?"
outcome-ops-assist "Show me examples of database query patterns" --topK 10
```

**See:** [CLI Usage Guide](cli-usage.md) for complete documentation and examples

### Add a New Repository to Ingest

1. Add to `terraform/dev.tfvars`:
   ```hcl
   repos_to_ingest = [
     {
       name  = "outcome-ops-analytics"
       owner = "bcarpio"
       type  = "application"
     }
   ]
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
     --function-name dev-outcome-ops-ai-assist-generate-code-maps \
     --payload '{"repos": ["outcome-ops-ai-assist"]}' \
     response.json
   ```

---

## Troubleshooting

### Knowledge base returns no results

1. Check ingestion completed:
   ```bash
   aws logs tail /aws/lambda/dev-outcome-ops-ai-assist-ingest-docs --follow
   ```

2. Verify documents in DynamoDB:
   ```bash
   aws dynamodb scan \
     --table-name dev-outcome-ops-ai-assist-kb \
     --limit 10
   ```

3. Re-trigger ingestion if needed

### Code generation feels off-pattern

1. Check if code map is current:
   ```bash
   aws lambda invoke \
     --function-name dev-outcome-ops-ai-assist-generate-code-maps \
     --payload '{"repos": ["outcome-ops-ai-assist"]}' \
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

1. Create handler in `lambda/my-handler/`
2. Add Terraform in `terraform/`
3. Add tests in `lambda/tests/unit/`
4. Update allowlist if needed
5. Deploy via Terraform

### Modifying Knowledge Base Schema

1. Update DynamoDB table schema in Terraform
2. Re-ingest data if schema changed
3. Test with existing queries

---

## Architecture Principles

1. **Knowledge drives generation** - The better your ADRs and code maps, the better Claude generates code
2. **Patterns matter** - Consistent patterns = better code generation
3. **You own the outcome** - Claude handles consistency, you own business logic
4. **Iterate together** - Each pattern you refine improves future generations
5. **Stay grounded** - RAG ensures answers reference your actual patterns

---

## External Resources

- **AWS Bedrock Documentation**: https://docs.aws.amazon.com/bedrock/
- **Claude 3.5 Sonnet**: https://www.anthropic.com/claude
- **Terraform AWS Lambda Module**: https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws/
