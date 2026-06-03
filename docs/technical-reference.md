# Technical Reference

Complete technical documentation for OutcomeOps AI Assist system internals, deployment, and operations.

## Table of Contents

- [Tech Stack](#tech-stack)
- [Project Structure](#project-structure)
- [Core Features](#core-features)
- [Repository Configuration (.outcomeops.yaml)](#repository-configuration-outcomeopsyaml)
- [Environment Configuration](#environment-configuration)
- [Resource Tagging](#resource-tagging)
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
| **Vector Storage** | S3 Vectors | Native vector database for semantic search |
| **State Tracking** | DynamoDB | Commit SHAs for incremental ingestion |
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
  --function-name prd-outcome-ops-ai-assist-ingest-docs \
  response.json
```

**What gets ingested:**
- **ADRs** from `docs/adr/` (Architecture Decision Records)
- **READMEs** from repository roots
- **Function-specific docs** from `docs/lambda-*.md` (avoids chunking large files)
- **Architecture docs** from `docs/architecture.md`
- **Deployment docs** from `docs/deployment.md`

**Stored in S3 Vectors with embeddings** for semantic search and RAG pipelines.

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
2. Vector searches S3 Vectors for candidate documents (<100ms)
3. Reranks candidates with Cohere Rerank 3.5 for precision ranking
4. Passes top reranked results to Claude Sonnet 4.5
5. Returns grounded answer with source citations

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

## Repository Configuration (.outcomeops.yaml)

Each repository can have a `.outcomeops.yaml` file at the root to configure code generation, testing, and auto-fix behavior.

### Full Configuration Reference

```yaml
# .outcomeops.yaml - Repository configuration for OutcomeOps AI Assist
version: "1.0"

# Ingestion settings - controls how files are classified during sync
ingestion:
  # Directories containing standards/ADRs (files here get doc_type="adr")
  # Used by generate-code for standards context
  # Default: ["docs/adr"]
  standards_paths:
    - "docs/adr"
    - "decisions"
    - "engineering-standards"

# Code generation settings
code_generation:
  # Backend type: "python", "java", or "typescript"
  backend: "python"

  # Custom guidelines for this repository (included in every plan)
  custom_guidelines: |
    Follow ADR-034 for Python dependency management (pin exact versions).
    Use Pydantic for all request/response validation.
    All Lambda handlers must follow the pattern in existing handlers.

  # Additional KB queries to run for every plan
  additional_kb_queries:
    - "Lambda handler patterns"
    - "Bedrock Claude integration"

  # Python-specific configuration (when backend: "python")
  python:
    # Directories containing local modules (for auto-fix module detection)
    source_paths:
      - "src"
      - "app"
      - "myproject"

    # Regex patterns for local module names (skip pip install, use Claude fix)
    local_module_patterns:
      - "^mycompany_"
      - "^internal_"

  # Java-specific configuration (when backend: "java")
  java:
    # Directories containing Java source files
    source_paths:
      - "src/main/java"
      - "src/test/java"
      - "app/src/main/java"

  # TypeScript-specific configuration (when backend: "typescript")
  typescript:
    # Directories containing TypeScript/JavaScript source files
    source_paths:
      - "src"
      - "lib"
      - "packages/core/src"  # monorepo

# Project context for plan generation
project_context: |
  This is a Django web application with the following structure:
  - myproject/ - Django project settings
  - users/ - User management app
  - orders/ - Order processing app
  - api/ - REST API endpoints

# Testing configuration
testing:
  framework: "pytest"  # or "junit", "jest"
  test_directory: "tests/"  # or "lambda/tests/unit", "src/test/java"
  min_coverage: 80

# Infrastructure configuration
infrastructure:
  tool: "terraform"  # or "cloudformation", "pulumi"
  directory: "terraform"

# ADRs to always include in context
required_adrs:
  # ADRs from this repository
  repo:
    - "ADR-001-authentication"
    - "ADR-002-database-patterns"
  # ADRs from global/organization ADR repositories
  global:
    - "ADR-001-terraform-infrastructure-patterns"
    - "ADR-004-lambda-handler-standards"
```

### Python Backend Configuration

For Python projects (Lambda, Flask, Django, etc.), the `python` section controls auto-fix behavior:

```yaml
code_generation:
  backend: "python"
  python:
    # Where to look for local modules
    source_paths:
      - "src"           # src/mymodule/
      - "app"           # app/models.py
      - "myproject"     # myproject/__init__.py

    # Patterns that indicate a local module (not PyPI)
    local_module_patterns:
      - "^mycompany_"   # mycompany_utils, mycompany_auth
      - "^internal_"    # internal_services
```

**Default source paths** (always included, combined with config):
- Root directory
- `src/`, `app/`, `apps/`, `lib/`
- `lambda/`, `lambdas/`, `functions/`
- `django/`, `flask_app/`

**Default local module patterns** (always included, combined with config):
- `^lambda_` - Lambda function modules
- `^test_` - Test modules
- `_handler$` - Handler modules
- `_service$` - Service modules
- `_utils$`, `_helpers$` - Utility modules
- `_models$`, `_views$` - Django/Flask modules

### Java Backend Configuration

For Java/Kotlin projects (Spring Boot, Gradle, Maven):

```yaml
code_generation:
  backend: "java"
  java:
    # Where to look for Java source files
    source_paths:
      - "src/main/java"
      - "src/test/java"
      - "app/src/main/java"  # Android
```

**Default source paths** (always included, combined with config):
- `src/main/java`, `src/test/java`
- `src/main/kotlin`, `src/test/kotlin`
- `*/src/main/java` (multi-module)
- `app/src/main/java` (Android)

### TypeScript Backend Configuration

For TypeScript/JavaScript projects (Next.js, React, Node.js):

```yaml
code_generation:
  backend: "typescript"
  typescript:
    # Where to look for TypeScript/JavaScript source files
    source_paths:
      - "src"
      - "lib"
      - "packages/core/src"  # monorepo
```

**Default source paths** (always included, combined with config):
- Root directory
- `src/`, `lib/`, `app/`, `pages/`, `components/`
- `utils/`, `services/`
- `test/`, `tests/`, `__tests__/`, `spec/`

**Supported package managers**: npm, yarn, pnpm (auto-detected)
**Supported test runners**: Jest, Vitest, Mocha (auto-detected)

### Auto-Fix Behavior

When tests fail with import/compilation errors, OutcomeOps:

1. **Detects error type**: import_error, syntax_error, or logic_error
2. **For import errors**:
   - Checks if module is local (using `source_paths` and `local_module_patterns`)
   - **Local module**: Uses Claude to analyze and fix the import
   - **External package**: Tries `pip install` first, falls back to Claude if it fails
3. **For syntax errors**: Uses Claude to fix the syntax
4. **For logic errors**: Creates PR and notifies humans (no auto-fix)

**Example auto-fix flow for Python:**

```
Error: ModuleNotFoundError: No module named 'users.models'

1. Check source_paths: Found 'users/' in configured paths → LOCAL
2. Skip pip install (it's not a PyPI package)
3. Query knowledge base for:
   - "Python import patterns and module structure"
   - "Python testing import patterns and conventions"
4. Send to Claude with:
   - File content
   - Error message
   - Knowledge base context
5. Apply Claude's fix
6. Re-run tests
```

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
[ingest-docs Lambda]
     ↓
Generate embedding via Bedrock Titan v2
     ↓
Store in S3 Vectors with metadata
     ↓
Available for queries + code generation
```

---

## Environment Configuration

### Required Parameters

Create `prd.tfvars`:

```hcl
aws_region           = "us-east-1"
environment          = "prd"
app_name             = "outcome-ops-ai-assist"

# GitHub access
github_token_ssm_path = "/outcome-ops/prd/github-token"

# Bedrock models
bedrock_embedding_model = "amazon.titan-embed-text-v2:0"
bedrock_claude_model    = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

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

### Resource Tagging

All AWS resources are automatically tagged with default tags for cost allocation, compliance, and resource management. You can also add custom tags for your organization's requirements.

**Default Tags (Applied Automatically)**

All resources created by Terraform include these tags:

| Tag Key | Value | Purpose |
|---------|-------|---------|
| `Environment` | `dev` or `prd` | Environment identification |
| `Application` | `outcome-ops-ai-assist` | Application/cost center grouping |
| `ManagedBy` | `Terraform` | Infrastructure management tracking |

**Adding Custom Tags**

Add custom tags in your environment's tfvars file (`dev.tfvars` or `prd.tfvars`):

```hcl
# Additional Tags
# Add custom tags here - they will be merged with default tags
tags = {
  CostCenter  = "engineering"
  Team        = "platform"
  Owner       = "devops@company.com"
  Project     = "ai-assist"
  Compliance  = "sox"
}
```

**Enterprise Tag Examples**

Common enterprise tagging patterns:

```hcl
# Cost allocation
tags = {
  CostCenter   = "12345"
  BusinessUnit = "engineering"
  Project      = "outcome-ops"
}

# Compliance and governance
tags = {
  DataClassification = "internal"
  Compliance         = "hipaa"
  Owner              = "security-team"
}

# Operations
tags = {
  SupportTeam  = "platform-engineering"
  OnCallGroup  = "devops-oncall"
  BackupPolicy = "daily"
}
```

**Tag Naming Convention**

Follow PascalCase for tag keys (e.g., `CostCenter`, `BusinessUnit`, `DataClassification`) to maintain consistency with AWS best practices and the default tags.

---

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
terraform workspace select prd
terraform plan -var-file=prd.tfvars
terraform apply -var-file=prd.tfvars
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
- `prd-outcome-ops-ai-assist-ingest-docs`: Ingestion events
- `prd-outcome-ops-ai-assist-query-kb`: Knowledge base queries (vector search + RAG)
- `prd-outcome-ops-ai-assist-ask-claude`: RAG generations
- `prd-outcome-ops-ai-assist-generate-code-maps`: Code analysis
- `prd-outcome-ops-ai-assist-analyze-pr`: PR analysis orchestration
- `prd-outcome-ops-ai-assist-process-pr-check`: PR check worker (SQS consumer)

### CloudWatch Alarms

Set up alerts for:
- Lambda error rates
- DynamoDB throttling
- SQS queue depth
- Bedrock API errors

```bash
# View recent errors
aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-ask-claude --follow
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
aws lambda invoke --function-name prd-outcome-ops-ai-assist-ingest-docs response.json

# 2. Generate code maps
aws lambda invoke --function-name prd-outcome-ops-ai-assist-generate-code-maps response.json

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

1. Add to `terraform/prd.tfvars`:
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
     --function-name prd-outcome-ops-ai-assist-generate-code-maps \
     --payload '{"repos": ["outcome-ops-ai-assist"]}' \
     response.json
   ```

---

## Troubleshooting

### Knowledge base returns no results

1. Check ingestion completed:
   ```bash
   aws logs tail /aws/lambda/prd-outcome-ops-ai-assist-ingest-docs --follow
   ```

2. Verify embeddings exist:
   ```bash
   # Check S3 Vectors index has vectors
   aws s3vectors list-vectors \
     --vector-bucket-name prd-outcome-ops-ai-assist-vectors \
     --index-name knowledge-base \
     --max-results 10
   ```

3. Re-trigger ingestion if needed

### Code generation feels off-pattern

1. Check if code map is current:
   ```bash
   aws lambda invoke \
     --function-name prd-outcome-ops-ai-assist-generate-code-maps \
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
- **Claude Sonnet 4.5**: https://www.anthropic.com/claude
- **Terraform AWS Lambda Module**: https://registry.terraform.io/modules/terraform-aws-modules/lambda/aws/

<!-- Confluence sync -->
