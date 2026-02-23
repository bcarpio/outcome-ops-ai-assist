# Multi-Language Support

**Status:** Planned
**Priority:** High
**Target:** Q1 2026

## Overview

Extend OutcomeOps to support multiple programming languages for Lambda functions and general code generation.

## Current State

- OutcomeOps currently supports **Python Lambda functions only**
- Dependency validation (ADR-006) is Python-specific using `pip install --dry-run`
- Code generation prompts and validation logic assume Python

## Goals

### Supported Languages

- **Node.js/TypeScript**: package.json, npm/yarn validation
- **Go**: go.mod, go get validation
- **Rust**: Cargo.toml, cargo check validation
- **Java**: pom.xml/build.gradle, Maven/Gradle validation

## Implementation Requirements

### 1. Language Detection

- Analyze issue requirements to determine target language
- Support explicit language specification in issue templates
- Default to Python for backward compatibility

### 2. Validation Tooling

- **Node.js**: `npm install --dry-run --package-lock-only`
- **Go**: `go mod download` and `go mod verify`
- **Rust**: `cargo check --manifest-path`
- **Java**: `mvn validate` or `gradle dependencies`

### 3. ADR Updates

- Create language-specific dependency management ADRs
- Define standard libraries and versions per language
- Document testing frameworks for each ecosystem

### 4. Step Executor Enhancement

- Detect language from file extensions and manifest files
- Route validation to appropriate language-specific validator
- Handle language-specific build/test commands

### 5. Knowledge Base

- Index language-specific patterns and examples
- Store language-specific standards and ADRs
- Enable cross-language pattern queries

## Success Metrics

- Successfully generate Node.js Lambda with validated package.json
- Successfully generate Go Lambda with validated go.mod
- Zero hallucinated dependencies across all languages
- Consistent validation time (<5s per manifest)

## Risks

- Each language adds complexity to validation pipeline
- Different package ecosystems have different behaviors
- Maintaining consistency across language-specific ADRs

## Related

- ADR-006: Python Dependency Management (template for other languages)
- Issue #6: Incident that motivated dependency validation
