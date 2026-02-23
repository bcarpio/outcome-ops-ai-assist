# Feature: Policy-Based Auto-Merge

---

## Overview

Automatically merge low-risk PRs that pass all checks. Require human review for high-risk changes.

## Risk Classification

**Low Risk (auto-merge):**
- Test files only
- Documentation updates
- Simple handlers with tests following established patterns

**Medium Risk (team lead review):**
- New business logic
- New API endpoints
- Modified handlers (non-breaking)

**High Risk (mandatory review):**
- Infrastructure changes (Terraform)
- Database migrations
- Security-related code
- Breaking API changes

## Decision Logic

```
All checks passed? → Assess risk level
├─ Low risk → Auto-merge
├─ Medium risk → Request team lead review
└─ High risk → Request specialized team review (platform, security, etc.)
```

## Configuration

**SSM Parameter: `/{env}/{app}/guardrails/policies`**

```yaml
auto_merge:
  low_risk_patterns:
    - type: test_file
      paths: ["**/tests/**", "**/*test.py"]
    - type: documentation
      paths: ["**/README.md", "docs/**"]

requires_review:
  high_risk_patterns:
    - type: infrastructure
      paths: ["terraform/**"]
      reviewers: ["platform-team"]
    - type: security
      paths: ["**/auth/**"]
      reviewers: ["security-team"]
```

## Implementation

**Lambda: policy-check** - Triggered after all PR checks complete, analyzes files against risk patterns, executes auto-merge or requests reviewers via GitHub API

**DynamoDB table: auto-merge-decisions** - Logs all decisions with risk level, reason, and outcome for audit trail
