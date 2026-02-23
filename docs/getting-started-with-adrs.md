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
- [Python logging best practices](https://docs.python.org/3/howto/logging.html)
- [Pydantic validation](https://docs.pydantic.dev/latest/concepts/validators/)
```

## More ADR Examples

### ADR-002: Terraform Module Standards

```markdown
# ADR-002: Terraform Module Organization

## Status
Accepted

## Summary
This ADR defines infrastructure as code standards for Terraform. Use exact
version pinning (e.g., `version = "8.1.2"`) and standard variable naming
(`var.environment`, `var.app_name`).

## Context
We're managing multiple AWS services with Terraform. Need consistent module structure.

## Decision
- Use terraform-aws-modules community modules when available
- Pin module versions exactly: `version = "8.1.2"` (never use `~>` or `*`)
- Use `var.environment` (not `var.env`) for environment names
- Resource naming: `${var.environment}-${var.app_name}-resource-name`
- Organize by service: `modules/lambda/`, `modules/dynamodb/`
- Use workspaces for environments (dev, prd)

## Consequences
- Faster module development (reuse community work)
- Predictable infrastructure (exact pinned versions)
- Easier to onboard new developers

## Examples
```hcl
module "my_lambda" {
  source  = "terraform-aws-modules/lambda/aws"
  version = "8.1.2"  # Always pin exact version

  function_name = "${var.environment}-${var.app_name}-my-function"
  # ...
}
```
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

## Guaranteeing ADRs Are Used: required_adrs

Some ADRs are so critical that they must ALWAYS be included in code generation, regardless of whether vector search finds them. Use the `required_adrs` configuration in `.outcomeops.yaml`:

```yaml
# .outcomeops.yaml
required_adrs:
  # ADRs from THIS repository (searched in your repo first)
  repo:
    - "ADR-100-our-custom-pattern"
  # ADRs from organization-wide ADR repositories
  global:
    - "ADR-001-terraform-infrastructure-patterns"
    - "ADR-004-lambda-handler-standards"
```

**When to use `required_adrs`:**
- Critical patterns that must always be followed (Terraform versions, security standards)
- Organization-wide standards from a central ADR repository
- ADRs with specific values that vector search might miss (exact version numbers)

**repo vs global:**
- `repo`: ADRs in the target repository being worked on
- `global`: ADRs from organization-wide repositories (like a shared `company-adrs` repo)

## Writing Discoverable ADRs

Vector search works by finding semantically similar content. To ensure your ADRs are found:

### Add a Summary Section

Include a **Summary** section right after Status that describes what the ADR covers in natural language:

```markdown
# ADR-001: Terraform Infrastructure Patterns

## Status
Accepted

## Summary
This ADR defines the **infrastructure as code standards** for all Terraform
configurations. It covers module version pinning (use exact versions like
`8.1.2`), variable naming (`var.environment`, not `var.env`), and resource
naming patterns.

## Context
...
```

### Use Searchable Language

Write your ADR using terms people would search for:
- "infrastructure as code standards" (not just "IaC")
- "error handling patterns" (not just "exceptions")
- "testing best practices" (not just "QA")

### Include Specific Values

If your ADR defines specific values (versions, names, patterns), include them prominently:
- Module version: `8.1.2`
- Variable: `var.environment`
- Pattern: `${var.environment}-${var.app_name}-resource`

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

## Custom Standards Locations

By default, OutcomeOps looks for ADRs in `docs/adr/`. If your organization uses a different convention, configure it in `.outcomeops.yaml`:

```yaml
# .outcomeops.yaml
ingestion:
  # Paths containing standards (ingested as doc_type="adr")
  standards_paths:
    - "engineering-standards"    # /engineering-standards/*.md
    - "platform/guidelines"      # /platform/guidelines/*.md
    - "docs/adr"                 # Keep default if you also have docs/adr/

  # Paths containing general documentation (ingested as doc_type="doc")
  docs_paths:
    - "docs"
    - "wiki"
```

**Why this matters for code generation:**

Code generation only uses documents with `doc_type="adr"` for standards context. This prevents noise from general documentation (architecture diagrams, deployment guides, etc.) from polluting the standards prompts.

Files in `standards_paths` → `doc_type="adr"` → Used for code generation
Files in `docs_paths` → `doc_type="doc"` → Available for queries but not code gen standards

## Quick Start Checklist

- [ ] Create your standards directory (default: `docs/adr/`)
- [ ] If using a custom path, add `ingestion.standards_paths` to `.outcomeops.yaml`
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

<!-- Confluence sync -->
