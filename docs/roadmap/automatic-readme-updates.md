# Automatic README Updates

**Status:** Planned
**Priority:** Medium
**Target:** Q1 2026

## Overview

Automatically include a step in generated plans to update the repository README when new Lambda handlers or infrastructure are added.

## Problem Statement

The CI check `readme-freshness` detects when:
- New Lambda handlers are added
- Infrastructure files (Terraform) are changed
- README.md was not updated

Currently, generated code always fails this check because the plan generator doesn't include README updates.

## Goals

- Plan generator detects when new handlers are being created
- Automatically add a final step to update README
- Include new handler in the appropriate section (Lambda Functions table, etc.)
- Maintain consistent documentation format

## Implementation

### Plan Generator Enhancement

In `plan_generator.py`:

1. After generating steps, analyze `filesToCreate` across all steps
2. Detect patterns indicating new handlers:
   - `lambda/{name}/handler.py` (Python)
   - `lambda/{name}/index.ts` (TypeScript)
   - `lambda/{name}/Handler.java` (Java)
3. Detect infrastructure changes:
   - `terraform/*.tf` files
4. If new handlers or infra detected, append README update step

### README Update Step

```json
{
  "stepNumber": N,
  "title": "Update README with new handler documentation",
  "description": "Add list-recent-docs handler to the Lambda Functions table in README.md. Include handler name, description, trigger type, and link to detailed documentation.",
  "filesToCreate": ["README.md"],
  "kbQueries": ["README documentation format for Lambda handlers"]
}
```

### Context for README Step

Provide to Claude:
- Current README.md content
- New handler names and descriptions
- New infrastructure resources
- Documentation links created in earlier steps

## Success Metrics

- 100% of generated PRs pass readme-freshness check
- README updates are accurate and consistent
- No manual README editing required after code generation

## Related

- CI check: `.github/workflows/ci.yml` (readme-freshness job)
- Plan generator: `lambda/generate-code/plan_generator.py`
