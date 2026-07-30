# Generate Code: Python Backend

This document describes the Python backend for code generation, which handles Python applications of any type - Lambda, Flask, Django, FastAPI, or any other Python framework.

## Overview

The Python backend (`python`) is **language-focused, not framework-focused**. It handles:
- File validation (Python syntax, requirements.txt)
- Test patterns (pytest conventions)
- Dependency management (pip/requirements.txt)

**Framework-specific patterns** (Lambda handlers, Flask blueprints, Django views, FastAPI routes) come from your repository's ADRs and the knowledge base - not from hardcoded prompts.

## How It Works

1. **Knowledge Base Queries**: The backend queries the KB for generic Python patterns
2. **ADR-Driven**: Your repo's ADRs teach Claude your specific framework conventions
3. **Project Context**: `.outcomeops.yaml` provides project-specific structure information
4. **Code Maps**: The generate-code-maps Lambda discovers your existing patterns

This means the same Python backend works for:
- AWS Lambda serverless applications
- Flask REST APIs
- Django web applications
- FastAPI microservices
- CLI tools
- Any Python project

## Supported Patterns

| Code Unit Type | Directory Pattern | Storage Key | Description |
|----------------|-------------------|-------------|-------------|
| Handler Groups | `lambda/<name>/` | `summary#handler#<name>` | Lambda function directories |
| Infrastructure | `terraform/` | `summary#infrastructure` | Terraform IaC files |
| Backend Tests | `**/test*/**/*.py` | `summary#tests#<type>` | Unit, integration, fixtures |
| Frontend Tests | `**/*.test.{ts,tsx}` | `summary#frontend-tests` | Jest/Vitest tests |
| Frontend Pages | `pages/`, `routes/` | `summary#frontend#pages` | Next.js/Remix pages |
| Frontend Components | `components/` | `summary#frontend#components` | React/Vue components |
| Frontend Utils | `utils/`, `hooks/`, `lib/` | `summary#frontend#utils` | Shared frontend utilities |
| Frontend Types | `types/`, `*.types.ts` | `summary#frontend#types` | TypeScript type definitions |
| Backend Shared | `src/`, `utils/`, `common/` | `summary#shared` | Shared Python utilities |
| Backend Schemas | `**/schema*/**`, `**/model*/**` | `summary#schemas` | Pydantic models, schemas |
| Documentation | `docs/`, `*.md` | `summary#docs` | Markdown documentation |
| Kubernetes | `k8s/`, `kubernetes/`, `helm/` | `summary#kubernetes` | K8s manifests |

## Directory Structure

The Python backend expects this standard structure:

```
my-project/
├── lambda/                      # Lambda functions directory
│   ├── ingest-docs/            # Handler group: ingest-docs
│   │   ├── handler.py          # Lambda entry point
│   │   ├── requirements.txt    # Dependencies
│   │   └── utils.py            # Handler-specific utilities
│   ├── query-kb/               # Handler group: query-kb
│   │   ├── handler.py
│   │   └── requirements.txt
│   └── tests/                  # Centralized tests
│       ├── unit/               # Unit tests
│       │   ├── test_ingest_docs.py
│       │   └── test_query_kb.py
│       └── integration/        # Integration tests
│           └── test_api.py
├── src/                        # Shared Python modules
│   ├── common/
│   │   └── logging.py
│   └── utils/
│       └── helpers.py
├── terraform/                  # Infrastructure as Code
│   ├── main.tf
│   ├── lambda.tf
│   └── variables.tf
├── docs/                       # Documentation
│   └── architecture.md
└── frontend/                   # Optional frontend (Next.js, etc.)
    ├── pages/
    ├── components/
    └── utils/
```

## Configuration

### .outcomeops.yaml

Configure OutcomeOps in your repository. The config file serves two purposes:

1. **Code Map Discovery** (generate-code-maps): Where to find code units
2. **Code Generation** (generate-code): How to generate new code

```yaml
version: "1.0"

code_generation:
  # Backend selection (used by both lambdas)
  backend: "python"

  # === Code Map Discovery Options ===
  # These control how generate-code-maps discovers your code structure

  # Override Lambda directory (default: "lambda")
  # lambda_directory: "src/functions"

  # === Code Generation Options ===
  # These control how generate-code creates new code

  # Custom guidelines appended to prompts
  custom_guidelines: |
    Use Pydantic v2 for all data models.
    Follow our established patterns from existing handlers.
    Use structured logging with JSON format.

  # Additional KB queries for every plan
  additional_kb_queries:
    - "DynamoDB pagination patterns"
    - "S3 presigned URL patterns"

  # File patterns to exclude from generation
  exclude_patterns:
    - "*.generated.py"

# Project context - THIS IS KEY for framework-specific generation
# Tell Claude what kind of project this is
project_context: |
  This is a serverless Python application using AWS Lambda.

  Project structure:
  - lambda/{function-name}/handler.py - Lambda handlers
  - lambda/{function-name}/requirements.txt - Per-function dependencies
  - lambda/tests/unit/test_{function_name}.py - Unit tests with pytest + moto
  - terraform/*.tf - Infrastructure as code

  We use DynamoDB for persistence and S3 for file storage.
  All handlers follow ADR-004 Lambda Handler Standards.

# Testing configuration
testing:
  framework: "pytest"
  test_directory: "lambda/tests/unit"
  min_coverage: 80

# Infrastructure configuration
infrastructure:
  tool: "terraform"
  directory: "terraform"

# ADRs to always include in context
required_adrs:
  - "ADR-004"
```

### Flask Example

```yaml
version: "1.0"

code_generation:
  backend: "python"

  custom_guidelines: |
    Use Flask blueprints for route organization.
    Use Flask-SQLAlchemy for database access.
    Use marshmallow for serialization.

project_context: |
  This is a Flask REST API deployed to Kubernetes.

  Project structure:
  - app/__init__.py - Flask app factory
  - app/blueprints/{resource}.py - Route blueprints
  - app/models/{resource}.py - SQLAlchemy models
  - app/schemas/{resource}.py - Marshmallow schemas
  - tests/unit/ - pytest unit tests
  - tests/integration/ - pytest integration tests with pytest-flask
  - k8s/ - Kubernetes manifests

  We use PostgreSQL for persistence and Redis for caching.

testing:
  framework: "pytest"
  test_directory: "tests"

infrastructure:
  tool: "terraform"
  directory: "terraform"
```

### Django Example

```yaml
version: "1.0"

code_generation:
  backend: "python"

  custom_guidelines: |
    Follow Django REST Framework conventions.
    Use Django ORM for all database access.
    Use serializers for request/response validation.

project_context: |
  This is a Django REST API deployed to ECS Fargate.

  Project structure:
  - myproject/settings.py - Django settings
  - apps/{app_name}/views.py - DRF ViewSets
  - apps/{app_name}/models.py - Django models
  - apps/{app_name}/serializers.py - DRF serializers
  - apps/{app_name}/tests/ - Django TestCase tests
  - terraform/ - ECS infrastructure

  We use PostgreSQL via RDS.

testing:
  framework: "pytest"
  test_directory: "apps"

infrastructure:
  tool: "terraform"
  directory: "terraform"

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BACKEND_TYPE` | Backend to use | `lambda` |
| `LAMBDA_DIRECTORY` | Directory containing Lambda functions | `lambda` |
| `HANDLER_FILE` | Handler filename | `handler.py` |

## Code Unit Discovery

### Handler Groups

Each subdirectory under `lambda/` containing Python files becomes a handler group:

```
lambda/
├── ingest-docs/     -> summary#handler#ingest-docs
├── query-kb/        -> summary#handler#query-kb
└── generate-code/   -> summary#handler#generate-code
```

**Discovery Logic:**
1. Find all `.py` files under `lambda/`
2. Group by the first subdirectory name
3. Create a `handler-group` code unit for each group

### Test Groups

Tests are discovered and grouped by type:

| Test Type | Pattern | Storage Key |
|-----------|---------|-------------|
| Unit | `**/unit/**/*.py`, `test_*.py` | `summary#tests#unit` |
| Integration | `**/integration/**/*.py` | `summary#tests#integration` |
| Fixtures | `**/fixture*/**/*.py` | `summary#tests#fixtures` |
| Other | Remaining test files | `summary#tests#other` |

### Shared Code

Python files in these directories are grouped as shared utilities:

- `src/`
- `utils/`
- `common/`
- `shared/`

Excludes `__init__.py` files from the summary.

### Schemas and Models

Files matching these patterns are grouped as schemas:

- `**/schema*/**/*.py`
- `**/model*/**/*.py`

## DynamoDB Storage

Code maps are stored with this key schema:

| Code Unit | PK | SK |
|-----------|----|----|
| Handler | `repo#<repo_name>` | `summary#handler#<handler_name>` |
| Tests | `repo#<repo_name>` | `summary#tests#<test_type>` |
| Infrastructure | `repo#<repo_name>` | `summary#infrastructure` |
| Shared | `repo#<repo_name>` | `summary#shared` |
| Schemas | `repo#<repo_name>` | `summary#schemas` |
| Frontend | `repo#<repo_name>` | `summary#frontend#<type>` |
| Docs | `repo#<repo_name>` | `summary#docs` |

## Incremental Updates

The Python backend supports incremental updates via git-based change detection:

1. **State Tracking**: Stores last processed commit SHA per repository
2. **Change Detection**: Compares current commit to last processed
3. **Selective Processing**: Only regenerates code maps for changed handlers

**Example Flow:**
```
Last processed: abc123
Current commit: def456

Changed files:
  - lambda/ingest-docs/handler.py
  - lambda/ingest-docs/utils.py

Result: Only regenerate summary#handler#ingest-docs
```

## Stale Code Map Cleanup

When files are deleted, the backend automatically cleans up stale code maps:

| Deleted File | Deleted Code Map |
|--------------|------------------|
| `lambda/my-handler/handler.py` | `summary#handler#my-handler` |
| `terraform/old-module.tf` | (Infrastructure regenerated) |

## Example Workflow

### Initial Setup

```bash
# Generate code maps for all repos
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps

# Generate for single repo
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps my-python-app
```

### Trigger Code Generation

Add the `approved-for-generation` label to a GitHub issue:

```markdown
## User Story

As a developer, I want to add a new Lambda handler that processes S3 events.

## Requirements

- Create `lambda/s3-processor/handler.py`
- Handle S3 put events
- Write processed data to DynamoDB
- Include unit tests
```

The system will:
1. Query knowledge base for Python Lambda patterns
2. Find repo-specific code maps for existing handlers
3. Generate code following established patterns
4. Create tests in `lambda/tests/unit/`

## Related Documentation

- [Lambda: Generate Code](lambda-generate-code.md) - Main code generation documentation
- Lambda: Generate Code Maps - Code map generation
- ADR-004: Lambda Handler Standards
- [ADR-009: Code Unit Discovery Pattern](adr/ADR-009-code-unit-discovery-pattern.md)

<!-- Confluence sync -->
