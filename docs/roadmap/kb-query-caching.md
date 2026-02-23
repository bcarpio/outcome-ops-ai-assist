# Knowledge Base Query Caching

**Status:** Planned
**Priority:** High
**Target:** Q1 2026

## Problem Statement

Current code generation runs excessive KB queries:
- Plan generation: ~18 queries for standards
- Each step execution: Additional step-specific queries
- Many queries are redundant across steps
- Cost of KB queries (Bedrock Titan embeddings + retrieval) is NOT tracked
- Same query returns same results - no caching

Example from terraform-serverless generation (13 steps):
```
Plan generation: 18 queries
Step 1: 2 queries
Step 2: 2 queries
Step 3: 0 queries (but re-queries standards)
...
Step 13: 2 queries

Total: ~40+ KB queries for one issue
```

Each query involves:
- Titan embedding generation (~$0.0001 per query)
- Bedrock KB retrieval API call
- Network latency (500ms-2s per query)

## Goals

1. Reduce redundant KB queries by 80%+
2. Track and report KB query costs alongside generation costs
3. Improve downstream step context with enriched cache
4. Reduce total generation time by eliminating redundant queries

## Implementation

### Phase 1: Simple DynamoDB Cache

Create a per-execution cache in DynamoDB:

```python
# Cache key structure
{
    "pk": "EXEC#{issue_number}#{repo}",
    "sk": "QUERY#{query_hash}",
    "query": "Lambda handler standards and patterns",
    "results": [...],  # KB retrieval results
    "timestamp": "2025-11-28T00:00:00Z",
    "ttl": 1732752000  # 24 hour TTL
}
```

### Phase 2: Query Deduplication

Before querying KB:
1. Hash the query string
2. Check DynamoDB cache for existing result
3. If hit: return cached results
4. If miss: query KB, store in cache, return results

### Phase 3: Enriched Context Cache

Store additional context per execution:

```python
{
    "pk": "EXEC#{issue_number}#{repo}",
    "sk": "CONTEXT",
    "standards": {
        "handler": [...],
        "testing": [...],
        "infrastructure": [...]
    },
    "previous_steps": {
        "step_1": {
            "files_created": ["handler.js"],
            "key_decisions": ["Used AWS SDK v2", "UUID validation regex"],
            "patterns_used": ["module.exports.handler", "createResponse helper"]
        }
    },
    "accumulated_knowledge": [
        "This repo uses Serverless Framework",
        "Existing handlers use 'use strict'",
        "IAM roles stored in SSM parameters"
    ]
}
```

### Phase 4: Smart Context Propagation

Each step receives:
1. Cached standards (no re-query)
2. Previous step summaries (what was built, patterns used)
3. Accumulated learnings from earlier steps
4. Step-specific KB queries only when truly needed

## Cost Tracking

Add to plan markdown:
```markdown
## Total Cost

**Code Generation:** $2.19
**KB Queries:** $0.08 (42 queries)
**Total:** $2.27
```

## Success Metrics

- KB queries reduced from ~40 to ~10 per issue
- Total generation time reduced by 20%+
- Full cost visibility (generation + KB)
- Improved step coherence from shared context

## Technical Notes

- Use DynamoDB TTL for automatic cache expiration
- Query hash should normalize whitespace/case
- Cache hit rate target: 70%+
- Consider semantic similarity for near-match queries (future)

## Related

- [Enhanced Error Reporting](./enhanced-error-reporting.md) - Error context could also be cached
- [Self-Healing Code Generation](./self-healing-code-generation.md) - Retry context benefits from cache
