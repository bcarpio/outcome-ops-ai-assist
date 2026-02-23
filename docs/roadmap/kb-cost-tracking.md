# KB Query Cost Tracking

**Status:** Planned
**Priority:** High
**Target:** Q1 2026

## Problem Statement

Current cost reporting only tracks code generation (Claude invocations), not KB queries:

```markdown
## Total Cost

**Total:** $2.19
**Input Tokens:** 87,759
**Output Tokens:** 128,283
```

Missing from this:
- Titan embedding generation for queries (~$0.0001 per query)
- Bedrock KB retrieval API calls
- Any Claude usage in query processing

For a 13-step plan with ~40+ KB queries, we're underreporting true costs by potentially 5-10%.

## Why This Matters

Even if the total cost is $5 per feature:
- **Transparency**: Customers deserve accurate billing
- **Optimization**: Can't optimize what we don't measure
- **ROI justification**: "$5 vs $500 human cost" is more compelling with accurate numbers
- **Budgeting**: Enterprise customers need predictable costs

## Current Cost Components (Tracked)

| Component | Tracked | Cost Model |
|-----------|---------|------------|
| Plan generation (Claude) | ✅ Yes | Input/output tokens |
| Step execution (Claude) | ✅ Yes | Input/output tokens |
| Requirements validation (pip) | ✅ No | N/A (free) |

## Missing Cost Components (Not Tracked)

| Component | Tracked | Estimated Cost |
|-----------|---------|----------------|
| KB query embeddings (Titan) | ❌ No | ~$0.0001/query |
| KB retrieval API | ❌ No | ~$0.0002/query |
| Architectural summary (Claude) | ❌ No | Varies |
| Code map generation (Claude) | ❌ No | Varies |

## Implementation

### Phase 1: Track KB Query Costs

Add cost tracking to `knowledge_base.py`:

```python
def query_knowledge_base(query: str) -> Tuple[List[str], KBQueryCost]:
    """Query KB and return results with cost."""
    # Track embedding cost (Titan)
    embedding_cost = calculate_titan_cost(query)

    # Track retrieval cost
    retrieval_cost = BEDROCK_KB_RETRIEVAL_COST

    results = kb_client.retrieve(...)

    return results, KBQueryCost(
        embedding_tokens=len(query.split()),
        embedding_cost_usd=embedding_cost,
        retrieval_cost_usd=retrieval_cost,
        total_cost_usd=embedding_cost + retrieval_cost
    )
```

### Phase 2: Aggregate in Plan

Update `ExecutionPlan` model:

```python
class ExecutionPlan(BaseModel):
    # Existing
    total_cost: TokenUsage

    # New
    kb_query_cost: KBQueryCost
    total_true_cost: float  # generation + KB queries
```

### Phase 3: Display in Plan Markdown

```markdown
## Total Cost

**Code Generation:** $2.19 (87,759 input / 128,283 output tokens)
**KB Queries:** $0.12 (42 queries)
**Total:** $2.31

💡 Estimated human equivalent: $500-2,000 (4-16 hours @ $125/hr)
```

### Phase 4: Track Ingestion Costs

Also track costs incurred during repo ingestion:
- Document chunking and embedding
- Architectural summary generation
- Code map generation

Store as repo metadata for amortization calculations.

## Cost Model Reference

### Bedrock Titan Embeddings (us-east-1)
- Input: $0.0001 per 1,000 tokens

### Bedrock KB Retrieval
- ~$0.0002 per query (approximate)

### Claude Sonnet 4.5 (current)
- Input: $0.003 per 1,000 tokens
- Output: $0.015 per 1,000 tokens

## Success Metrics

- 100% of costs tracked and reported
- Cost variance vs actual AWS bill < 5%
- Clear ROI visible in every plan ("$2.31 vs ~$1,000 human cost")

## Related

- [KB Query Caching](./kb-query-caching.md) - Reduce costs by caching
- [KB Repo Filtering](./kb-repo-filtering.md) - Reduce costs by filtering
