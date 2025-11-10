# ADR-006: Python Dependency Management Standards

## Status: Accepted

## Context

Lambda functions should use minimal, consistent, and verifiable Python dependencies to ensure:
- **Reliability**: Only include packages that actually exist on PyPI
- **Security**: Minimize attack surface by avoiding unnecessary dependencies
- **Consistency**: Use compatible versions across all Lambda functions
- **Performance**: Reduce cold start times with smaller dependency bundles

### The Problem

During automated code generation (issue #6), Claude hallucinated a non-existent package `extra==1.16.0` in requirements.txt, causing:
- Test execution failures in the run-tests Lambda
- Delayed feedback loop (failure happened 15+ minutes after generation)
- Manual intervention required to fix the generated code

### Root Causes

1. **Insufficient Context**: Requirements generation had zero KB queries, no examples
2. **No Validation**: Generated files committed without checking PyPI existence
3. **Missing Standards**: No ADR documenting approved packages or validation requirements

## Decision

### 1. Minimal Dependencies Policy

**Only include packages that are directly imported in the Lambda handler code.**

Do not add "nice to have" or "commonly used" packages unless they are actually imported.

### 2. Standard Package Versions

Use consistent package versions across all Lambda functions:

**Core Dependencies:**
```text
boto3>=1.28.0          # AWS SDK (always required)
pydantic>=2.0.0        # Data validation (when needed)
requests>=2.31.0       # HTTP client (when calling external APIs)
```

**Test Dependencies:**
```text
pytest>=7.0.0          # Test framework
pytest-cov>=4.0.0      # Coverage reporting
moto>=5.0.0           # AWS mocking
responses>=0.23.0      # HTTP mocking (when testing requests)
```

**Avoid:**
- Date/time libraries (use stdlib `datetime`)
- JSON libraries (use stdlib `json`)
- OS/path libraries (use stdlib `os`, `pathlib`)
- Type hint libraries (use stdlib `typing`)

### 3. Validation Requirements

Every requirements.txt file MUST be validated before commit using:

```bash
pip install --dry-run -r requirements.txt
```

This catches:
- Non-existent packages (like `extra==1.16.0`)
- Invalid version specifiers
- Dependency conflicts

### 4. Language-Specific Scope

**IMPORTANT:** This ADR applies to **Python Lambda functions only**.

Multi-language support (Node.js, Go, Rust) is tracked separately in the roadmap and will require:
- Language-specific validation tooling
- Separate ADRs for each ecosystem
- Enhanced step executor logic to detect language

## Consequences

### Positive

- **Prevents hallucinations**: Validation catches non-existent packages before commit
- **Faster feedback**: Errors detected in seconds, not minutes
- **Consistency**: All Lambdas use compatible package versions
- **Maintainability**: Smaller dependency trees are easier to update
- **Security**: Minimal attack surface from third-party packages

### Tradeoffs

- **Validation overhead**: Adds 2-5 seconds to code generation per requirements.txt
- **Strictness**: May reject valid but unconventional version specs
- **Python-only**: Needs extension for other languages (see roadmap)

## Implementation

### Starting Today

1. **Add validation to step_executor.py**:
   - Before committing requirements.txt, run `pip install --dry-run`
   - If validation fails, log error and retry generation with context
   - Include validation errors in step completion metadata

2. **Update plan generation context**:
   - When creating requirements.txt steps, query KB for existing examples
   - Include reference requirements from 2-3 similar Lambdas
   - Emphasize "only include imported packages" in prompts

3. **Knowledge base ingestion**:
   - Ensure all requirements.txt files are indexed
   - Tag them as "dependency-examples" for easy retrieval

### Next Phases

1. **Self-healing**: If validation fails, automatically retry with corrected context
2. **Multi-language support**: Extend validation to package.json, go.mod, Cargo.toml
3. **Version pinning**: Add tooling to suggest version updates across all Lambdas
4. **Dependency scanning**: Integrate with security scanning tools

## Related ADRs

- ADR-005: Testing Standards - Defines test dependency requirements
- ADR-002: Development Workflow - Covers code generation process

## References

- PyPI package index: https://pypi.org/
- pip documentation: https://pip.pypa.io/
- Python packaging guide: https://packaging.python.org/
- Issue #6: First hallucination incident (list-recent-docs Lambda)

## Version History

- v1.0 (2025-11-10): Initial decision after `extra==1.16.0` hallucination incident
