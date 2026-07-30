# Generate Code: Java Backend

This document describes the Java backend for code generation, which handles Java/Kotlin applications of any type - Spring Boot, Lambda, Quarkus, Micronaut, or any other Java framework.

## Overview

The Java backend (`java`) is **language-focused, not framework-focused**. It handles:
- File validation (Java syntax, pom.xml/build.gradle)
- Test patterns (JUnit 5 conventions)
- Dependency management (Maven/Gradle)

**Framework-specific patterns** (Spring controllers, Lambda handlers, Quarkus resources) come from your repository's ADRs and the knowledge base - not from hardcoded prompts.

## How It Works

1. **Knowledge Base Queries**: The backend queries the KB for generic Java patterns
2. **ADR-Driven**: Your repo's ADRs teach Claude your specific framework conventions
3. **Project Context**: `.outcomeops.yaml` provides project-specific structure information
4. **Code Maps**: The generate-code-maps Lambda discovers your existing patterns

This means the same Java backend works for:
- Spring Boot microservices
- AWS Lambda handlers
- Quarkus applications
- Micronaut services
- Plain Java applications
- Any Java/Kotlin project

## Supported Patterns

| Code Unit Type | Directory Pattern | Storage Key | Description |
|----------------|-------------------|-------------|-------------|
| Source Modules | Package directories with 2+ files | `summary#module#<name>` | Java packages as code units |
| Unit Tests | `src/test/java/`, `*Test.java` | `summary#tests#unit` | JUnit/TestNG unit tests |
| Integration Tests | `src/integrationTest/`, `*IT.java` | `summary#tests#integration` | Integration tests |
| E2E Tests | `**/e2e/**`, `**/acceptance/**` | `summary#tests#e2e` | End-to-end tests |
| Build Config | `pom.xml`, `build.gradle` | `summary#build` | Maven/Gradle configuration |
| Infrastructure | `terraform/` | `summary#infrastructure` | Terraform IaC files |
| Kubernetes | `k8s/`, `kubernetes/`, `helm/` | `summary#kubernetes` | K8s manifests |
| Documentation | `docs/`, `*.md` | `summary#docs` | Markdown documentation |

## Directory Structure

The Java backend supports standard Maven/Gradle layouts:

```
my-java-project/
├── src/
│   ├── main/
│   │   ├── java/
│   │   │   └── com/
│   │   │       └── company/
│   │   │           └── project/
│   │   │               ├── controller/     # -> summary#module#controller
│   │   │               │   ├── UserController.java
│   │   │               │   └── OrderController.java
│   │   │               ├── service/        # -> summary#module#service
│   │   │               │   ├── UserService.java
│   │   │               │   └── OrderService.java
│   │   │               ├── repository/     # -> summary#module#repository
│   │   │               │   └── UserRepository.java
│   │   │               └── model/          # -> summary#module#model
│   │   │                   ├── User.java
│   │   │                   └── Order.java
│   │   ├── kotlin/                         # Kotlin source (optional)
│   │   └── resources/
│   │       └── application.yml
│   └── test/
│       └── java/
│           └── com/company/project/
│               ├── controller/             # -> summary#tests#unit
│               └── service/
├── pom.xml                                 # -> summary#build
├── terraform/                              # -> summary#infrastructure
└── docs/                                   # -> summary#docs
```

### Multi-Module Projects

For multi-module Maven/Gradle projects:

```
parent-project/
├── pom.xml
├── module-api/
│   ├── pom.xml
│   └── src/main/java/...
├── module-core/
│   ├── pom.xml
│   └── src/main/java/...
└── module-web/
    ├── pom.xml
    └── src/main/java/...
```

Each module is discovered as separate code units based on package structure.

## Configuration

### .outcomeops.yaml

Configure OutcomeOps in your repository. The config file serves two purposes:

1. **Code Map Discovery** (generate-code-maps): Where to find code units
2. **Code Generation** (generate-code): How to generate new code

```yaml
version: "1.0"

code_generation:
  # Backend selection (used by both lambdas)
  backend: "java"

  # === Code Map Discovery Options ===
  # These control how generate-code-maps discovers your code structure
  # Java uses Maven/Gradle conventions by default - usually no config needed

  # Include Kotlin files (default: true)
  # include_kotlin: true

  # Override source roots (default: Maven/Gradle conventions)
  # source_roots:
  #   - "src/main/java"
  #   - "src/main/kotlin"

  # === Code Generation Options ===
  # These control how generate-code creates new code

  # Custom guidelines appended to prompts
  custom_guidelines: |
    Use constructor injection for all beans.
    Follow the existing package structure.
    Use Lombok @Data for DTOs where available.

  # Additional KB queries for every plan
  additional_kb_queries:
    - "JPA repository custom query methods"
    - "Spring Security authentication patterns"

  # File patterns to exclude from generation
  exclude_patterns:
    - "*.generated.java"

# Project context - THIS IS KEY for framework-specific generation
# Tell Claude what kind of project this is
project_context: |
  This is a Spring Boot 3.x application using Java 21.

  Project structure:
  - src/main/java/com/company/app/controller/ - REST controllers
  - src/main/java/com/company/app/service/ - Business logic
  - src/main/java/com/company/app/repository/ - JPA repositories
  - src/main/java/com/company/app/model/ - JPA entities
  - src/main/java/com/company/app/dto/ - Data transfer objects
  - src/test/java/ - JUnit 5 tests with Mockito
  - terraform/ - ECS Fargate infrastructure

  We use PostgreSQL for persistence and Redis for caching.
  The API follows REST conventions with JSON payloads.

# Testing configuration
testing:
  framework: "junit"
  test_directory: "src/test/java"
  min_coverage: 80

# Infrastructure configuration
infrastructure:
  tool: "terraform"
  directory: "terraform"

# ADRs to always include in context
required_adrs:
  - "ADR-004"
```

### Lambda Example

```yaml
version: "1.0"

code_generation:
  backend: "java"

  custom_guidelines: |
    Use AWS SDK v2 for all AWS service calls.
    Initialize clients outside the handler for connection reuse.
    Use Pojo with Jackson annotations for request/response.

project_context: |
  This is a Java 21 AWS Lambda application.

  Project structure:
  - src/main/java/com/company/handlers/{Function}Handler.java - Lambda handlers
  - src/main/java/com/company/models/ - Request/response POJOs
  - src/main/java/com/company/services/ - Business logic
  - src/test/java/ - JUnit 5 tests with aws-lambda-java-tests
  - terraform/ - Lambda infrastructure

  Handler signature: handleRequest(APIGatewayProxyRequestEvent, Context)
  We use DynamoDB for persistence.

testing:
  framework: "junit"
  test_directory: "src/test/java"

infrastructure:
  tool: "terraform"
  directory: "terraform"
```

### Quarkus Example

```yaml
version: "1.0"

code_generation:
  backend: "java"

  custom_guidelines: |
    Use Quarkus CDI annotations (@Inject, @ApplicationScoped).
    Use JAX-RS annotations for REST endpoints.
    Use Panache for data access.

project_context: |
  This is a Quarkus 3.x application using Java 21.

  Project structure:
  - src/main/java/com/company/resource/ - JAX-RS resources
  - src/main/java/com/company/service/ - Business logic
  - src/main/java/com/company/entity/ - Panache entities
  - src/test/java/ - QuarkusTest tests
  - k8s/ - Kubernetes manifests

  We use PostgreSQL with Panache.

testing:
  framework: "junit"
  test_directory: "src/test/java"

infrastructure:
  tool: "terraform"
  directory: "k8s"

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BACKEND_TYPE` | Backend to use | `java` |
| `INCLUDE_KOTLIN` | Include `.kt` files | `true` |

## Code Unit Discovery (ADR-009)

The Java backend uses a generic algorithm to discover code unit boundaries:

### Discovery Criteria

A directory becomes a code unit when it meets ANY of these:

1. **File Count Threshold**: Contains 2+ source files directly
2. **Boundary Pattern Match**: Directory name matches known patterns
3. **Build Module Marker**: Contains `pom.xml` or `build.gradle`

### Boundary Patterns

Directory names that indicate a code unit boundary:

| Category | Patterns |
|----------|----------|
| Service/Business | `service`, `handler`, `controller`, `api`, `client`, `worker`, `processor` |
| Data/Models | `model`, `entity`, `dto`, `schema`, `domain` |
| Infrastructure | `repository`, `dao`, `adapter`, `gateway` |
| Utilities | `util`, `helper`, `common`, `shared`, `core`, `lib` |
| Configuration | `config`, `security`, `auth` |
| Implementation | `impl`, `internal`, `legacy` |

### Namespace Container Handling

For deep Java package hierarchies like `com/company/project/service/impl/deep/nested`:

- Directories with 0 source files and depth > 4 are treated as **namespace containers**
- These are traversed through, not made into code units
- This prevents creating code units for empty package levels

### Discovery Example

Given this structure:
```
src/main/java/com/apriori/common/
├── applicationkernel/     # 3 files -> code unit
│   ├── ApplicationKernel.java
│   ├── KernelConfig.java
│   └── KernelUtils.java
├── core/
│   └── model/
│       └── application/   # 2 files -> code unit
│           ├── AppModel.java
│           └── AppState.java
├── notification/
│   └── email/             # 3 files -> code unit
│       ├── EmailService.java
│       ├── EmailTemplate.java
│       └── EmailConfig.java
└── restapiclient/         # 2 files -> code unit
    ├── RestClient.java
    └── RestConfig.java
```

**Discovered Code Units:**
- `summary#module#common-applicationkernel`
- `summary#module#core-model-application`
- `summary#module#common-notification-email`
- `summary#module#common-restapiclient`

## DynamoDB Storage

Code maps are stored with this key schema:

| Code Unit | PK | SK |
|-----------|----|----|
| Module | `repo#<repo_name>` | `summary#module#<module_name>` |
| Tests | `repo#<repo_name>` | `summary#tests#<test_type>` |
| Build | `repo#<repo_name>` | `summary#build` |
| Infrastructure | `repo#<repo_name>` | `summary#infrastructure` |
| Kubernetes | `repo#<repo_name>` | `summary#kubernetes` |
| Docs | `repo#<repo_name>` | `summary#docs` |

## Test Discovery

Tests are discovered from standard Maven/Gradle test directories:

| Source Root | Test Type |
|-------------|-----------|
| `src/test/java` | Unit tests |
| `src/test/kotlin` | Unit tests (Kotlin) |
| `src/integrationTest/java` | Integration tests |
| `src/integrationTest/kotlin` | Integration tests (Kotlin) |

**Classification Logic:**
- Files with `integration` or `integ` in path -> Integration
- Files with `e2e`, `end-to-end`, or `acceptance` in path -> E2E
- Files in `src/test/` or with `unit` in path -> Unit
- Everything else -> Other

## Build Configuration

The backend discovers these build files:

| File | Description |
|------|-------------|
| `pom.xml` | Maven project file |
| `build.gradle` | Gradle build script |
| `build.gradle.kts` | Kotlin DSL build script |
| `settings.gradle` | Gradle settings |
| `settings.gradle.kts` | Kotlin DSL settings |
| `gradle.properties` | Gradle properties |

## Incremental Updates

The Java backend supports incremental updates:

1. **State Tracking**: Stores last processed commit SHA
2. **Change Detection**: Git compare between commits
3. **Selective Processing**: Only regenerates changed modules

**Example Flow:**
```
Last processed: abc123
Current commit: def456

Changed files:
  - src/main/java/com/company/service/UserService.java
  - src/main/java/com/company/service/UserServiceImpl.java

Result: Only regenerate summary#module#service
```

## Stale Code Map Cleanup

When files are deleted, stale code maps are removed:

| Deleted File | Deleted Code Map |
|--------------|------------------|
| `src/main/java/.../service/*.java` | `summary#module#service` |
| All files in a module directory | `summary#module#<module_name>` |

## Example Workflow

### Initial Setup

```bash
# Generate code maps for all repos
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps

# Generate for single repo
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps spring-petclinic
```

### Trigger Code Generation

Add the `approved-for-generation` label to a GitHub issue:

```markdown
## User Story

As a developer, I want to add a new REST endpoint for managing user preferences.

## Requirements

- Create `PreferenceController` in `com.company.controller`
- Create `PreferenceService` with CRUD operations
- Create `Preference` entity with JPA annotations
- Include unit tests with MockMvc
```

The system will:
1. Query knowledge base for Spring Boot patterns
2. Find repo-specific code maps for existing controllers/services
3. Generate code following the established package structure
4. Create tests using the project's testing conventions

## Spring Boot Patterns

The Java backend understands common Spring patterns:

| Pattern | Discovery |
|---------|-----------|
| Controllers | `*Controller.java` in `controller/` packages |
| Services | `*Service.java` in `service/` packages |
| Repositories | `*Repository.java` in `repository/` packages |
| Entities | `*.java` in `model/`, `entity/`, `domain/` packages |
| DTOs | `*DTO.java`, `*Request.java`, `*Response.java` |
| Config | `*Config.java`, `*Configuration.java` |

## Related Documentation

- [Lambda: Generate Code](lambda-generate-code.md) - Main code generation documentation
- Lambda: Generate Code Maps - Code map generation
- [ADR-009: Code Unit Discovery Pattern](adr/ADR-009-code-unit-discovery-pattern.md)

<!-- Confluence sync -->
