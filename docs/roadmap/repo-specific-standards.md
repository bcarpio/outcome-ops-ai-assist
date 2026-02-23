# Repo-Specific Standards Override

**Status:** Planned
**Priority:** Medium

## Problem

When multiple teams share a knowledge base, they may have conflicting standards:
- Team A uses Spring Boot 3, Team B uses Spring Boot 2
- Team A uses Terraform module v8.1.2, Team B uses v7.14.0
- Different naming conventions, testing frameworks, etc.

Currently, the KB returns all matching ADRs regardless of which repo they came from, which can lead to conflicting guidance.

## Proposed Solution

Allow repos to specify ADR priority or override behavior in `.outcomeops.yaml`:

```yaml
code_generation:
  backend: "python"

  # Standards configuration
  standards:
    # Primary ADR source - these ADRs take precedence
    primary_adr_repo: "my-org/standards"

    # Fallback to org-wide standards if not found in primary
    fallback_adr_repo: "my-org/org-standards"

    # Or completely ignore ADRs from other repos
    adr_scope: "repo-only"  # repo-only | org-wide | custom
```

## Alternative: ADR Namespacing

Another approach is to namespace ADRs by team/project:

```
ADR-001-terraform-standards           # Org-wide
ADR-001-team-alpha-terraform-standards # Team-specific override
```

The KB query could then prioritize team-specific ADRs when they exist.

## Implementation Considerations

1. **Query filtering**: Filter KB results by repo before returning
2. **Priority scoring**: Boost relevance of repo-specific ADRs
3. **Explicit overrides**: Allow `.outcomeops.yaml` to specify which ADRs to use
4. **Convergence**: Over time, AI-driven code will naturally converge standards as teams see consistent patterns

## Related

- Multi-team deployments share usage limits per license key
- KB caching should respect repo-specific filtering
