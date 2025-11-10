# OutcomeOps Roadmap

This document tracks planned features and improvements for the OutcomeOps AI-assisted code generation platform.

## Current Focus

- Improving reliability and resilience of code generation
- Enhancing test coverage and validation
- Better error handling and feedback loops

## Planned Features

### Multi-Language Support

**Status:** Planned
**Priority:** High
**Target:** Q1 2026

**Current State:**
- OutcomeOps currently supports **Python Lambda functions only**
- Dependency validation (ADR-006) is Python-specific using `pip install --dry-run`
- Code generation prompts and validation logic assume Python

**Goals:**
Extend OutcomeOps to support multiple programming languages for Lambda functions and general code generation:

**Supported Languages:**
- **Node.js/TypeScript**: package.json, npm/yarn validation
- **Go**: go.mod, go get validation
- **Rust**: Cargo.toml, cargo check validation
- **Java**: pom.xml/build.gradle, Maven/Gradle validation

**Implementation Requirements:**

1. **Language Detection**:
   - Analyze issue requirements to determine target language
   - Support explicit language specification in issue templates
   - Default to Python for backward compatibility

2. **Validation Tooling**:
   - **Node.js**: `npm install --dry-run --package-lock-only`
   - **Go**: `go mod download` and `go mod verify`
   - **Rust**: `cargo check --manifest-path`
   - **Java**: `mvn validate` or `gradle dependencies`

3. **ADR Updates**:
   - Create language-specific dependency management ADRs
   - Define standard libraries and versions per language
   - Document testing frameworks for each ecosystem

4. **Step Executor Enhancement**:
   - Detect language from file extensions and manifest files
   - Route validation to appropriate language-specific validator
   - Handle language-specific build/test commands

5. **Knowledge Base**:
   - Index language-specific patterns and examples
   - Store language-specific standards and ADRs
   - Enable cross-language pattern queries

**Success Metrics:**
- Successfully generate Node.js Lambda with validated package.json
- Successfully generate Go Lambda with validated go.mod
- Zero hallucinated dependencies across all languages
- Consistent validation time (<5s per manifest)

**Risks:**
- Each language adds complexity to validation pipeline
- Different package ecosystems have different behaviors
- Maintaining consistency across language-specific ADRs

**Related:**
- ADR-006: Python Dependency Management (template for other languages)
- Issue #6: Incident that motivated dependency validation

---

### Self-Healing Code Generation

**Status:** Planned
**Priority:** Medium
**Target:** Q1 2026

Automatically retry failed generation steps with enhanced context:
- Parse validation errors
- Inject error details into retry prompt
- Limit retries to 2 attempts per step

---

### Enhanced Error Reporting

**Status:** Planned
**Priority:** Medium
**Target:** Q1 2026

Improve run-tests Lambda error parsing:
- Extract package name from pip errors
- Include error details in EventBridge events
- Surface validation failures earlier in the pipeline

---

### Version Management

**Status:** Planned
**Priority:** Low
**Target:** Q2 2026

Tooling to manage dependency versions:
- Detect outdated packages across all Lambdas
- Suggest coordinated updates
- Test compatibility before applying

---

## Completed Features

### Test Coverage Improvements
- Added comprehensive tests for plan_generator module
- Achieved 85% overall test coverage (exceeding 80% target)
- Completed: 2025-11-10

### Granular Test Step Generation
- Plan generator now breaks tests into focused steps
- Prevents Claude timeouts during test generation
- Each test step limited to 1-3 test functions
- Completed: 2025-11-10

### Increased Bedrock Timeout
- Bedrock read timeout increased to 850s
- Lambda timeout remains at 900s (50s buffer)
- Completed: 2025-11-10

---

## Contributing

Have a feature idea? Open an issue with the `enhancement` label or start a discussion.

**Last Updated:** 2025-11-10
