# Generate Code: TypeScript Backend

This document describes the TypeScript backend for code generation, which handles TypeScript/JavaScript applications of any type - Lambda, Express, Next.js, React, or any other Node.js framework.

## Overview

The TypeScript backend (`typescript`) is **language-focused, not framework-focused**. It handles:
- File validation (TypeScript syntax, package.json)
- Test patterns (Jest/Vitest conventions)
- Dependency management (npm/yarn/pnpm)

**Framework-specific patterns** (Lambda handlers, Express routes, Next.js pages, React components) come from your repository's ADRs and the knowledge base - not from hardcoded prompts.

## How It Works

1. **Knowledge Base Queries**: The backend queries the KB for generic TypeScript/Node.js patterns
2. **ADR-Driven**: Your repo's ADRs teach Claude your specific framework conventions
3. **Project Context**: `.outcomeops.yaml` provides project-specific structure information
4. **Code Maps**: The generate-code-maps Lambda discovers your existing patterns

This means the same TypeScript backend works for:
- AWS Lambda serverless applications
- Express/Fastify REST APIs
- Next.js full-stack applications
- React/Vue/Svelte frontends
- NestJS microservices
- Any TypeScript/Node.js project

## Supported Patterns

| Code Unit Type | Directory Pattern | Storage Key | Description |
|----------------|-------------------|-------------|-------------|
| Handler Groups | `handlers/`, `lambda/`, `functions/` | `summary#handler#<name>` | Serverless Lambda handlers |
| API Routes | `routes/`, `api/`, `controllers/` | `summary#api#<name>` | Express/Fastify routes |
| Frontend | `components/`, `pages/`, `app/` | `summary#frontend#<name>` | React/Vue/Svelte components |
| Schemas | `*-schema.ts`, `schemas/` | `summary#schemas` | Zod/Yup validation schemas |
| Shared | `shared/`, `common/`, `utils/`, `lib/` | `summary#shared#<name>` | Shared utilities |
| Tests | `*.test.ts`, `*.spec.ts`, `__tests__/` | `summary#tests#<type>` | Unit, integration, e2e tests |
| Config | `serverless.yml`, `tsconfig.json`, etc. | `summary#config` | Build/deploy configuration |
| Infrastructure | `terraform/` | `summary#infrastructure` | Terraform IaC files |
| Kubernetes | `k8s/`, `kubernetes/`, `helm/` | `summary#kubernetes` | K8s manifests |
| Documentation | `docs/`, `*.md` | `summary#docs` | Markdown documentation |

## Directory Structures

### Serverless Lambda Structure

```
my-serverless-app/
├── api/                              # Handler directories (configurable)
│   ├── login/                        # -> summary#handler#login
│   │   ├── handler.ts
│   │   └── types.ts
│   ├── register/                     # -> summary#handler#register
│   │   ├── handler.ts
│   │   └── validator.ts
│   └── bookings/                     # -> summary#handler#bookings
│       ├── handler.ts
│       └── booking-schema.ts
├── shared/                           # -> summary#shared#shared
│   ├── database.ts
│   └── auth.ts
├── schemas/                          # -> summary#schemas
│   ├── user-schema.ts
│   └── booking-schema.ts
├── serverless.yml                    # -> summary#config
├── tsconfig.json
└── terraform/                        # -> summary#infrastructure
```

### Express/Fastify API Structure

```
my-api/
├── src/
│   ├── handlers/
│   │   └── api-gw/                   # Handler groups by subdirectory
│   │       ├── action-mapper/        # -> summary#handler#api-gw-action-mapper
│   │       │   ├── handler.ts
│   │       │   └── mapper.ts
│   │       └── user-auth/            # -> summary#handler#api-gw-user-auth
│   │           ├── handler.ts
│   │           └── auth.ts
│   ├── routes/
│   │   ├── users/                    # -> summary#api#routes-users
│   │   │   ├── index.ts
│   │   │   └── controller.ts
│   │   └── orders/                   # -> summary#api#routes-orders
│   │       └── index.ts
│   ├── shared/
│   │   └── database/                 # -> summary#shared#shared-database
│   │       ├── client.ts
│   │       └── migrations.ts
│   └── utils/                        # -> summary#shared#utils
│       └── helpers.ts
├── tests/
│   ├── unit/                         # -> summary#tests#tests-unit
│   └── integration/                  # -> summary#tests#tests-integration
└── tsconfig.json                     # -> summary#config
```

### Next.js/React Structure

```
my-nextjs-app/
├── app/                              # Next.js App Router
│   ├── api/                          # -> summary#api#app-api
│   │   └── users/
│   │       └── route.ts
│   ├── dashboard/                    # -> summary#frontend#app-dashboard
│   │   └── page.tsx
│   └── layout.tsx
├── components/                       # -> summary#frontend#src-components
│   ├── Button.tsx
│   └── Header.tsx
├── lib/                              # -> summary#shared#lib
│   ├── api.ts
│   └── utils.ts
├── types/                            # (part of schemas)
│   └── user.types.ts
└── next.config.js                    # -> summary#config
```

### Monorepo Structure

```
my-monorepo/
├── packages/
│   ├── api/
│   │   └── src/
│   │       └── handlers/             # Discovered per handler_directories config
│   ├── shared/
│   │   └── src/                      # -> summary#shared#packages-shared
│   └── ui/
│       └── src/
│           └── components/           # -> summary#frontend#packages-ui
└── apps/
    └── web/
        └── src/
            └── pages/                # -> summary#frontend#apps-web
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
  backend: "typescript"

  # === Code Map Discovery Options ===
  # These control how generate-code-maps discovers your code structure

  # Handler directories - where to look for Lambda handlers
  # Each subdirectory containing a handler file becomes a code unit
  handler_directories:
    - "api/"           # Discovers api/login/, api/register/, etc.
    - "handlers/"      # Discovers handlers/auth/, handlers/users/
    - "./"             # Root-level handlers like email-notification/

  # Handler file patterns (default: handler.ts, *-handler.ts)
  handler_patterns:
    - "handler.ts"
    - "-handler.ts"
    - ".handler.ts"

  # Override source roots
  source_roots:
    - "src"
    - "lib"

  # Include JavaScript files (default: true)
  # include_javascript: true

  # === Code Generation Options ===
  # These control how generate-code creates new code

  # Custom guidelines appended to prompts
  custom_guidelines: |
    Use Zod for all runtime validation.
    Follow functional programming patterns.
    Use TypeScript strict mode.

  # Additional KB queries for every plan
  additional_kb_queries:
    - "DynamoDB patterns for Node.js"
    - "S3 presigned URL patterns"

  # File patterns to exclude from generation
  exclude_patterns:
    - "*.generated.ts"

# Project context - THIS IS KEY for framework-specific generation
# Tell Claude what kind of project this is
project_context: |
  This is a serverless TypeScript application using AWS Lambda.

  Project structure:
  - api/{function}/handler.ts - Lambda handlers
  - shared/ - Shared utilities across handlers
  - schemas/ - Zod validation schemas
  - tests/unit/ - Jest unit tests
  - tests/integration/ - Integration tests
  - serverless.yml - Serverless Framework config

  We use DynamoDB for persistence and S3 for file storage.
  API Gateway handles HTTP routing.

# Testing configuration
testing:
  framework: "jest"
  test_directory: "tests/unit"
  min_coverage: 80

# Infrastructure configuration
infrastructure:
  tool: "terraform"
  directory: "terraform"

# ADRs to always include in context
required_adrs:
  - "ADR-004"
```

### Express Example

```yaml
version: "1.0"

code_generation:
  backend: "typescript"

  # No handler_directories needed - Express uses routes/
  source_roots:
    - "src"

  custom_guidelines: |
    Use Express Router for route organization.
    Use Zod for request validation middleware.
    Use dependency injection with tsyringe.

project_context: |
  This is an Express.js REST API deployed to Kubernetes.

  Project structure:
  - src/routes/{resource}.ts - Express routers
  - src/services/{resource}Service.ts - Business logic
  - src/middleware/ - Express middleware
  - src/types/ - TypeScript interfaces
  - tests/unit/ - Jest unit tests
  - tests/integration/ - Supertest integration tests
  - k8s/ - Kubernetes manifests

  We use PostgreSQL with Prisma ORM.

testing:
  framework: "jest"
  test_directory: "tests"

infrastructure:
  tool: "terraform"
  directory: "k8s"
```

### Next.js Example

```yaml
version: "1.0"

code_generation:
  backend: "typescript"

  source_roots:
    - "app"
    - "components"
    - "lib"

  custom_guidelines: |
    Use App Router conventions (app/ directory).
    Use Server Components by default.
    Use Server Actions for mutations.

project_context: |
  This is a Next.js 14 application deployed to Vercel.

  Project structure:
  - app/ - App Router pages and layouts
  - app/api/ - API route handlers
  - components/ - React components
  - lib/ - Shared utilities
  - prisma/ - Database schema
  - __tests__/ - Jest + Testing Library tests

  We use PostgreSQL with Prisma.

testing:
  framework: "jest"
  test_directory: "__tests__"
```

### NestJS Example

```yaml
version: "1.0"

code_generation:
  backend: "typescript"

  source_roots:
    - "src"

  custom_guidelines: |
    Use NestJS decorators (@Controller, @Injectable).
    Use class-validator for DTO validation.
    Use TypeORM for database access.

project_context: |
  This is a NestJS microservice deployed to ECS.

  Project structure:
  - src/modules/{module}/ - Feature modules
  - src/modules/{module}/{module}.controller.ts - Controllers
  - src/modules/{module}/{module}.service.ts - Services
  - src/modules/{module}/dto/ - DTOs with class-validator
  - src/modules/{module}/entities/ - TypeORM entities
  - test/ - Jest e2e tests
  - terraform/ - ECS infrastructure

  We use PostgreSQL with TypeORM.

testing:
  framework: "jest"
  test_directory: "test"

infrastructure:
  tool: "terraform"
  directory: "terraform"

### Handler Directories

The `handler_directories` option is key for serverless projects. It tells the backend where to look for Lambda handler directories.

**Default values:**
```yaml
handler_directories:
  - "src/"
  - "handlers/"
  - "lambda/"
  - "functions/"
```

**Special value `"./"` or `""`:** Looks for handler directories at the repository root level.

**Example configurations:**

```yaml
# Node.js Serverless with API Gateway handlers
handler_directories:
  - "api/"         # api/login/, api/register/, api/bookings/
  - "./"           # email-notification/, sms-notification/

# Standard handlers directory
handler_directories:
  - "handlers/"    # handlers/auth/, handlers/users/

# Monorepo with handlers in packages
handler_directories:
  - "packages/api/src/handlers/"
  - "packages/workers/src/"
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BACKEND_TYPE` | Backend to use | `typescript` |
| `INCLUDE_JAVASCRIPT` | Include `.js` files | `true` |

## Code Unit Discovery

### Handler Groups

Handler groups are discovered based on `handler_directories` configuration:

**Method 1: Custom handler_directories**
```
handler_directories: ["api/"]

api/
├── login/           # -> summary#handler#login
│   └── handler.ts
└── register/        # -> summary#handler#register
    └── handler.ts
```

**Method 2: Standard handlers/ directory**
```
src/handlers/
├── api-gw/
│   ├── action-mapper/   # -> summary#handler#api-gw-action-mapper
│   │   └── handler.ts
│   └── user-auth/       # -> summary#handler#api-gw-user-auth
│       └── handler.ts
```

**Handler file patterns:**
- `handler.ts`, `handler.js`
- `*-handler.ts`, `*-handler.js`
- `*.handler.ts`, `*.handler.js`

### API Routes

Files in these directories are grouped as API routes:

| Directory | Example Storage Key |
|-----------|---------------------|
| `routes/users/` | `summary#api#routes-users` |
| `api/v1/` | `summary#api#api-v1` |
| `controllers/auth/` | `summary#api#controllers-auth` |

### Frontend Components

Frontend files are discovered by extension and directory:

| Extension | Directories |
|-----------|-------------|
| `.tsx`, `.jsx` | `src/`, `app/`, `components/`, `pages/` |
| `.vue` | `src/`, `components/`, `pages/` |
| `.svelte` | `src/`, `components/`, `routes/` |

**Example Storage Keys:**
- `summary#frontend#src-components`
- `summary#frontend#app-dashboard`
- `summary#frontend#pages-auth`

### Schemas

Files matching these patterns are grouped as schemas:

| Pattern | Example |
|---------|---------|
| `*-schema.ts` | `user-schema.ts` |
| `*.schema.ts` | `booking.schema.ts` |
| `schemas/*.ts` | `schemas/validation.ts` |

**Storage Key:** `summary#schemas`

### Shared Code

Files in shared directories (excluding handlers/):

| Directory | Example Storage Key |
|-----------|---------------------|
| `shared/database/` | `summary#shared#shared-database` |
| `common/utils/` | `summary#shared#common-utils` |
| `utils/` | `summary#shared#utils` |
| `lib/` | `summary#shared#lib` |
| `helpers/` | `summary#shared#helpers` |

### Tests

Tests are discovered and categorized:

| Pattern | Category | Storage Key |
|---------|----------|-------------|
| `*.test.ts`, `*.spec.ts` | Unit | `summary#tests#tests-unit` |
| `**/integration/**` | Integration | `summary#tests#tests-integration` |
| `**/e2e/**`, `**/cypress/**` | E2E | `summary#tests#tests-e2e` |
| `__tests__/` | Unit | `summary#tests#tests-unit` |

### Configuration Files

These files are grouped as config:

| File | Description |
|------|-------------|
| `serverless.yml`, `serverless.yaml` | Serverless Framework |
| `tsconfig.json`, `tsconfig.base.json` | TypeScript config |
| `vite.config.ts`, `webpack.config.js` | Build config |
| `next.config.js`, `next.config.mjs` | Next.js config |
| `tailwind.config.js` | Tailwind CSS |
| `jest.config.js`, `vitest.config.ts` | Test config |
| `.gitlab-ci.yml`, `.github/workflows/*` | CI/CD |
| `Makefile`, `justfile` | Build automation |

**Storage Key:** `summary#config`

## DynamoDB Storage

Code maps are stored with this key schema:

| Code Unit | PK | SK |
|-----------|----|----|
| Handler | `repo#<repo_name>` | `summary#handler#<name>` |
| API Routes | `repo#<repo_name>` | `summary#api#<name>` |
| Frontend | `repo#<repo_name>` | `summary#frontend#<name>` |
| Schemas | `repo#<repo_name>` | `summary#schemas` |
| Shared | `repo#<repo_name>` | `summary#shared#<name>` |
| Tests | `repo#<repo_name>` | `summary#tests#<name>` |
| Config | `repo#<repo_name>` | `summary#config` |
| Infrastructure | `repo#<repo_name>` | `summary#infrastructure` |
| Kubernetes | `repo#<repo_name>` | `summary#kubernetes` |
| Docs | `repo#<repo_name>` | `summary#docs` |

## Incremental Updates

The TypeScript backend supports incremental updates:

1. **State Tracking**: Stores last processed commit SHA
2. **Change Detection**: Git compare between commits
3. **Selective Processing**: Only regenerates changed code units

**Example Flow:**
```
Last processed: abc123
Current commit: def456

Changed files:
  - api/login/handler.ts
  - api/login/validator.ts
  - shared/auth.ts

Result: Regenerate:
  - summary#handler#login
  - summary#shared#shared
```

## Stale Code Map Cleanup

When files are deleted, stale code maps are removed:

| Deleted File Pattern | Deleted Code Map |
|---------------------|------------------|
| `api/login/handler.ts` | `summary#handler#login` |
| `routes/users/*.ts` | `summary#api#routes-users` |
| `components/**/*.tsx` | `summary#frontend#<group>` |
| `schemas/*.ts` | `summary#schemas` |
| `shared/utils/*.ts` | `summary#shared#shared-utils` |

## Example Workflow

### Initial Setup

```bash
# Generate code maps for all repos
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps

# Generate for single repo
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps my-serverless-app
```

### Trigger Code Generation

Add the `approved-for-generation` label to a GitHub issue:

```markdown
## User Story

As a user, I want to create a new Lambda handler for processing webhook events.

## Requirements

- Create `api/webhook/handler.ts`
- Validate incoming payload with Zod schema
- Store events in DynamoDB
- Return 200 OK on success
- Include unit tests with Jest
```

The system will:
1. Query knowledge base for TypeScript Lambda patterns
2. Find repo-specific code maps for existing handlers
3. Generate code following the established patterns
4. Create tests using the project's Jest configuration

## Excluded Patterns

These directories are automatically excluded from discovery:

| Pattern | Description |
|---------|-------------|
| `node_modules/` | npm dependencies |
| `dist/`, `build/`, `builds/` | Build output |
| `.next/`, `.nuxt/` | Framework build cache |
| `coverage/` | Test coverage reports |
| `__tests__/`, `__mocks__/` | Test infrastructure (discovered separately) |
| `.turbo/`, `.vercel/` | Build system cache |

## Related Documentation

- [Lambda: Generate Code](lambda-generate-code.md) - Main code generation documentation
- Lambda: Generate Code Maps - Code map generation
- [ADR-009: Code Unit Discovery Pattern](adr/ADR-009-code-unit-discovery-pattern.md)

<!-- Confluence sync -->
