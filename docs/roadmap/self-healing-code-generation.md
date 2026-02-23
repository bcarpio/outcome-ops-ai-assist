# Self-Healing Code Generation

**Status:** Planned
**Priority:** Medium
**Target:** Q1 2026

## Overview

Automatically retry failed generation steps with enhanced context to improve success rate without human intervention.

## Goals

- Parse validation errors and extract actionable information
- Inject error details into retry prompt for Claude
- Limit retries to 2 attempts per step to prevent infinite loops
- Track retry success/failure rates for improvement

## Implementation

### Error Parsing

Extract meaningful information from:
- Python syntax errors (line numbers, error messages)
- Import validation failures (module name, invalid import)
- Dependency validation errors (package name, version conflicts)
- Terraform validation errors (resource, attribute)

### Retry Prompt Enhancement

Include in retry prompt:
- Original step description
- Previous attempt's generated code
- Specific error message and location
- Guidance on how to fix the error type

### Retry Limits

- Maximum 2 retry attempts per step
- Exponential backoff between retries
- Mark step as failed after all retries exhausted
- Continue to next step or halt based on configuration

## Success Metrics

- 50% reduction in permanently failed steps
- Average 1.2 attempts per step (down from potential 3)
- Clear error attribution in failure reports

## Related

- [Enhanced Error Reporting](./enhanced-error-reporting.md)
