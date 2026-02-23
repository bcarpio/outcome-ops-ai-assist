# Knowledge Base Repo Filtering

**Status:** Planned
**Priority:** High
**Target:** Q1 2026

## Problem Statement

KB retrieval returns documents from ALL repos in the vector store, not scoped to the relevant repo. This causes cross-contamination in multi-tenant scenarios.

Example:
```
Query: "explain what terraform-serverless is and its core features"

Sources returned:
  • docs-summary - terraform-serverless     ✅ Correct
  • config-summary - terraform-serverless   ✅ Correct
  • Code map - terraform-serverless         ✅ Correct
  • Code map - ooai-license-server          ❌ WRONG - different repo
```

The answer may still be mostly correct, but:
1. Wrong sources are cited
2. Context pollution from unrelated repos
3. Token waste on irrelevant content
4. Potential for incorrect information bleeding across repos

## Root Cause

KB queries use pure semantic/vector similarity search without metadata filtering:

```python
# Current implementation (likely)
results = kb_client.retrieve(
    knowledgeBaseId=kb_id,
    retrievalQuery={"text": query},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 5
        }
    }
)
```

Documents have repo metadata but it's not being used as a filter.

## Solution

Add metadata filter to KB queries when repo context is known:

```python
# Proposed implementation
results = kb_client.retrieve(
    knowledgeBaseId=kb_id,
    retrievalQuery={"text": query},
    retrievalConfiguration={
        "vectorSearchConfiguration": {
            "numberOfResults": 5,
            "filter": {
                "equals": {
                    "key": "repo",
                    "value": "terraform-serverless"
                }
            }
        }
    }
)
```

## Implementation

### Phase 1: Add Repo Filter to generate-code

When generating code for a specific repo:
1. Pass `repo_full_name` to KB query functions
2. Add metadata filter to restrict results to that repo
3. Optionally include "shared" ADRs that apply to all repos

### Phase 2: Add Repo Filter to query-kb Lambda

For CLI queries (`outcome-ops-assist`):
1. Add optional `--repo` flag to CLI
2. Pass repo filter to query-kb Lambda
3. Default behavior: search all repos (current behavior)
4. With `--repo`: filter to specific repo

### Phase 3: Hybrid Search for Standards

Some documents should be shared across repos:
- Core ADRs (dependency management, testing standards)
- Platform-wide patterns

Implementation:
```python
# Get repo-specific docs
repo_results = kb.retrieve(query, filter={"repo": repo_name})

# Get shared/platform docs
shared_results = kb.retrieve(query, filter={"repo": "shared"})

# Merge and dedupe
results = merge_results(repo_results, shared_results)
```

## Success Metrics

- Zero cross-repo contamination in sources
- Repo-specific queries return only relevant docs
- Shared standards still accessible when needed

## Related

- [KB Query Caching](./kb-query-caching.md) - Cache should also be repo-scoped
- Bedrock KB metadata filtering documentation
