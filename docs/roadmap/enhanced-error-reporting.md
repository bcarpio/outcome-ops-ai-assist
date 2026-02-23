# Enhanced Error Reporting

**Status:** Planned
**Priority:** Medium
**Target:** Q1 2026

## Overview

Improve run-tests Lambda error parsing and surface validation failures earlier in the pipeline with actionable information.

## Goals

- Extract package name from pip errors
- Include error details in EventBridge events
- Surface validation failures earlier in the pipeline
- Provide actionable feedback in GitHub issue comments

## Implementation

### Error Extraction

Parse specific error types:
- `ModuleNotFoundError`: Extract module name
- `ImportError`: Extract import path and name
- `SyntaxError`: Extract line number and code snippet
- `ValidationError`: Extract field and constraint

### EventBridge Enhancement

Include in events:
- Error category (syntax, import, validation, runtime)
- Affected file path and line number
- Suggested fix category
- Link to relevant ADR or documentation

### GitHub Integration

Post helpful comments on issues:
- Summarize what failed and why
- Suggest specific fixes when possible
- Link to documentation for common issues

## Success Metrics

- 90% of errors have actionable information
- Average time to understand error reduced by 50%
- Reduction in "what went wrong?" follow-up questions

## Related

- [Self-Healing Code Generation](./self-healing-code-generation.md)
