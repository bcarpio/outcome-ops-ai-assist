# Getting Started with ADRs (Architecture Decision Records)

## What Are ADRs?

Architecture Decision Records (ADRs) are lightweight documents that capture important architectural decisions along with their context and consequences.

Think of them as design docs that answer: **"Why did we decide to do it this way?"**

## Why ADRs Matter for OutcomeOps

When Claude generates code, it needs to know YOUR standards:
- How do you handle errors?
- What testing patterns do you use?
- Which libraries are approved?
- How do you structure infrastructure?

**ADRs are how you tell the AI your decisions.**

## ADR Template

```markdown
# ADR-NNN: [Short Decision Title]

## Status
[Proposed | Accepted | Deprecated | Superseded]

## Context
What's the problem or situation that requires a decision?

## Decision
What did you decide? Be specific.

## Consequences
What are the trade-offs?
- Positive: What improves?
- Negative: What gets harder?
- Neutral: What changes?

## Examples
Show working code examples if possible.
```

## Your First ADR: Error Handling

Let's create your first ADR for Lambda error handling:

**File:** `docs/adr/ADR-001-lambda-error-handling.md`

```markdown
# ADR-001: Lambda Error Handling Standard

## Status
Accepted

## Context
Our Lambda handlers need consistent error handling across all functions:
- User-facing errors should be clear
- Internal errors should be logged with context
- All errors should return consistent JSON format
- Correlation IDs needed for debugging

## Decision
All Lambda handlers will:

1. **Use try-except blocks** around handler logic
2. **Log errors with structured logging** including correlation IDs
3. **Return consistent error format:**
   ```python
   {
     "error": "User-friendly message",
     "code": "ERROR_CODE",
     "requestId": "correlation-id"
   }
   ```
4. **Distinguish error types:**
   - 400: Client errors (validation, bad input)
   - 500: Server errors (our bugs, downstream failures)

## Consequences

**Positive:**
- Debugging is faster with correlation IDs
- Errors are consistent across platform
- User experience improves with clear messages

**Negative:**
- Slightly more boilerplate per handler
- Need to maintain error code registry

**Neutral:**
- Need to train team on error patterns

## Example Implementation

```python
import logging
from typing import Dict, Any
from pydantic import BaseModel, ValidationError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

class UserRequest(BaseModel):
    email: str
    name: str

def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    request_id = context.request_id

    try:
        # Parse and validate input
        body = json.loads(event.get('body', '{}'))
        user_request = UserRequest(**body)

        # Business logic
        result = process_user(user_request)

        return {
            'statusCode': 200,
            'body': json.dumps({
                'data': result,
                'requestId': request_id
            })
        }

    except ValidationError as e:
        logger.error(f"Validation error: {e}", extra={'requestId': request_id})
        return {
            'statusCode': 400,
            'body': json.dumps({
                'error': 'Invalid input format',
                'code': 'VALIDATION_ERROR',
                'details': e.errors(),
                'requestId': request_id
            })
        }

    except Exception as e:
        logger.error(f"Unexpected error: {e}", extra={'requestId': request_id}, exc_info=True)
        return {
            'statusCode': 500,
            'body': json.dumps({
                'error': 'Internal server error',
                'code': 'INTERNAL_ERROR',
                'requestId': request_id
            })
        }
```

## References
- Python logging best practices: [link]
- Pydantic validation: [link]
```

## More ADR Examples

### ADR-002: Terraform Module Standards

```markdown
# ADR-002: Terraform Module Organization

## Status
Accepted

## Context
We're managing multiple AWS services with Terraform. Need consistent module structure.

## Decision
- Use terraform-aws-modules community modules when available
- Pin module versions: `version = "~> 4.0"`
- Organize by service: `modules/lambda/`, `modules/dynamodb/`
- Use workspaces for environments (dev, prd)

## Consequences
- Faster module development (reuse community work)
- Predictable infrastructure (pinned versions)
- Easier to onboard new developers

## Examples
See `terraform/modules/lambda/main.tf`
```

### ADR-003: Testing Standards

```markdown
# ADR-003: Python Testing Standards

## Status
Accepted

## Context
Need consistent testing approach across Lambda functions.

## Decision
- Use pytest for all tests
- Mock AWS services with moto
- 80% code coverage minimum
- Test files match source: `handler.py` → `test_handler.py`

## Consequences
- Faster test execution (mocked AWS)
- Consistent test patterns
- Higher confidence in deployments

## Examples
See `tests/unit/test_ingest_docs.py`
```

## How OutcomeOps Uses ADRs

1. **Ingestion**: ADRs are ingested into the knowledge base
2. **Embedding**: Each ADR gets a vector embedding
3. **Search**: When you ask "how should I handle errors?", OutcomeOps searches ADRs
4. **Generation**: Claude uses your ADR as context to generate code

**Result: Generated code follows YOUR decisions, not generic patterns.**

## Best Practices

### Do:
- ✅ Write ADRs for important decisions
- ✅ Include code examples
- ✅ Keep them concise (1-2 pages max)
- ✅ Update them when decisions change
- ✅ Number them sequentially (ADR-001, ADR-002, etc.)

### Don't:
- ❌ Document every tiny decision
- ❌ Make them too long (no one will read them)
- ❌ Write them after the fact (capture decisions when made)
- ❌ Let them get stale (update or deprecate)

## When to Write an ADR

Write an ADR when:
- You make an architectural decision that affects multiple parts of the system
- You choose between multiple approaches (document why you chose this one)
- You establish a standard pattern (error handling, testing, infrastructure)
- Future developers will ask "why did they do it this way?"

## Quick Start Checklist

- [ ] Create `docs/adr/` directory
- [ ] Copy the ADR template from `docs/adr/TEMPLATE.md`
- [ ] Write ADR-001 for your most important pattern
- [ ] Add code examples
- [ ] Commit to repository
- [ ] Trigger OutcomeOps ingestion
- [ ] Test with `outcome-ops-assist "What's our error handling standard?"`

## Resources

- [ADR GitHub Org](https://adr.github.io/) - ADR documentation and tools
- [Joel Parker Henderson's ADR repo](https://github.com/joelparkerhenderson/architecture-decision-record) - Hundreds of ADR examples
- [OutcomeOps ADR Template](../adr/TEMPLATE.md) - Our template

---

**Next Steps:**
- [Create your first ADR](../adr/TEMPLATE.md)
- [Deploy OutcomeOps](getting-started.md)
- [Test ADR ingestion](technical-reference.md#knowledge-base-ingestion)
