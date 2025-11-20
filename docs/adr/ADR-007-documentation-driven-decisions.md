# ADR-007: Documentation-Driven Decision Making

## Status: Accepted

## Context

OutcomeOps AI Assist is a platform for generating code using AI with organizational context (ADRs, code maps, documentation). A fundamental question arose: **When encountering a problem, should we fix it with code logic or with documentation?**

**Example scenario:** AI-generated tests fail because they use `from lambda.function import handler` (invalid Python syntax - `lambda` is a keyword).

**Two possible solutions:**

1. **Hardcode the fix** - Add logic to run-tests Lambda to detect and rewrite these imports
2. **Document the pattern** - Create ADR-006 explaining correct import patterns for tests

**The critical insight:** OutcomeOps is a **platform**, not just an application. The platform should be generic and adaptable. The **knowledge base** (ADRs, READMEs, docs) should encode domain-specific knowledge, not the code itself.

## Decision

### Principle: Documentation Guides Behavior, Code Executes It

**Primary Rule:** When a problem can be solved by improving documentation (ADRs, READMEs, guides), prefer that over hardcoding logic in the application code.

**Rationale:**
1. **Platform remains generic** - Code doesn't need Python-specific, Terraform-specific, or language-specific logic
2. **Knowledge is queryable** - AI can search ADRs and adapt to new patterns
3. **Easily extensible** - Adding new patterns doesn't require code changes
4. **Self-documenting** - Solutions are explicit in ADRs, not hidden in code
5. **Transferable** - Other organizations can adapt ADRs to their conventions
6. **Maintainable** - Changing standards = updating docs, not deploying code

### Decision Tree: Code vs Documentation

**When to use Documentation (ADRs/Docs):**

✅ **Pattern definition** - "This is how we structure tests"
✅ **Convention guidance** - "Use this naming pattern"
✅ **Best practices** - "Prefer X over Y because..."
✅ **Domain knowledge** - "In Python, `lambda` is a reserved keyword"
✅ **Architectural decisions** - "We use DynamoDB for storage"
✅ **Quality standards** - "Tests must achieve 80% coverage"
✅ **Examples** - "Here's a complete working example"

**When to use Code Logic:**

✅ **Workflow orchestration** - "After tests pass, create PR"
✅ **Error recovery** - "Retry failed operations 3 times"
✅ **Infrastructure** - "Deploy Lambda with these settings"
✅ **Integration** - "Call this API with these parameters"
✅ **Validation** - "Check if environment variables are set"
✅ **Automation** - "Trigger downstream processes"
✅ **Platform features** - "Run tests in isolated environment"

### When Both Are Needed

Many solutions require **both** documentation and code:

**Example: Test Auto-Fix Feature**

**Documentation (ADR-006):**
- ✅ Correct import pattern for Lambda tests
- ✅ Why `from lambda.X import Y` fails
- ✅ Complete template with working example
- ✅ Common pitfalls and solutions

**Code (run-tests Lambda):**
- ✅ Detect test failures
- ✅ Classify error types
- ✅ Attempt auto-fix using Claude
- ✅ Create PR if fixes fail
- ✅ Post failure comments

**Division of responsibility:**
- **Documentation** → Teaches AI how to generate correct tests
- **Code** → Handles workflow when tests fail anyway

### The Knowledge Base Query Loop

OutcomeOps operates in a loop:

```
1. User requests feature (GitHub issue)
   ↓
2. generate-code queries knowledge base
   - Reads ADRs for patterns and conventions
   - Reads code maps for existing implementations
   - Reads READMEs for component documentation
   ↓
3. generate-code uses knowledge to write code
   - Follows patterns from ADRs
   - Matches style from existing code
   - Respects conventions from docs
   ↓
4. run-tests validates generated code
   - Detects errors (syntax, logic, imports)
   - Classifies error types
   - Attempts fixes or creates PR
   ↓
5. Human reviews and merges
   ↓
6. If pattern emerges, update ADRs
   - Document new patterns
   - Explain why decisions were made
   - Provide examples for future generation
   ↓
7. Knowledge base updated → Better future generation
```

**The cycle improves over time:** More ADRs = Better generation = Fewer errors

### Examples of Documentation-Driven Solutions

**Problem:** Tests fail with `from lambda.X import Y`
**Wrong Approach:** Add regex in run-tests to rewrite these imports
**Right Approach:** Create ADR-006 with correct pattern; AI generates it correctly next time

**Problem:** PRs use inconsistent commit message format
**Wrong Approach:** Add logic to rewrite commit messages
**Right Approach:** ADR-003 defines format; AI follows it

**Problem:** Terraform files aren't formatted
**Wrong Approach:** Never noticed this should happen
**Right Approach:** Add terraform fmt layer; run after generation (we implemented both!)

**Problem:** DynamoDB table design varies across Lambdas
**Wrong Approach:** Template generator with hardcoded table patterns
**Right Approach:** ADR defining `PK`/`SK` conventions; AI follows pattern

### When Documentation Alone Isn't Enough

**Limitations of documentation-driven approach:**

1. **Stochastic AI behavior** - AI doesn't always follow docs perfectly
   - **Mitigation:** run-tests validates and attempts fixes
2. **Complex patterns** - Some patterns are hard to describe in text
   - **Mitigation:** Include complete working examples in ADRs
3. **Environmental constraints** - Lambda limits, AWS permissions, etc.
   - **Mitigation:** Combine ADRs with automation code
4. **Legacy compatibility** - Existing code may not match new ADRs
   - **Mitigation:** Document migration strategy in ADRs

**The solution:** Layered defense with both documentation and code:
- **Layer 1 (Generate):** ADRs guide generation → Prevents most issues
- **Layer 2 (Validate):** run-tests catches errors → Fixes simple issues
- **Layer 3 (Review):** Human review → Handles complex issues

## Consequences

### Positive

- **Platform stays generic** - No Python-specific or domain-specific hardcoded logic
- **Easy to extend** - New patterns = new ADR, not code deployment
- **Self-improving** - Better docs → Better generation → Fewer errors
- **Transparent** - Decisions documented explicitly, not hidden in code
- **Portable** - Other orgs can fork and adapt ADRs to their standards
- **Maintainable** - Changing standards doesn't require code changes
- **Teachable** - New developers/AI can learn from ADRs

### Tradeoffs

- **Requires discipline** - Developers must update ADRs when patterns change
- **Not foolproof** - AI doesn't always follow docs perfectly (stochastic)
- **Initial overhead** - Writing comprehensive ADRs takes time upfront
- **Knowledge drift** - ADRs can become stale if not maintained
- **Requires validation** - Still need code to catch when AI doesn't follow docs

### Negative (Mitigated)

- **Risk: ADRs become outdated** - Code evolves, docs lag behind
  - **Mitigation:** Include ADR review in PR checklist
- **Risk: AI ignores ADRs** - Generates code that doesn't follow patterns
  - **Mitigation:** run-tests validates and creates PR for human review
- **Risk: ADRs conflict** - Multiple ADRs contradict each other
  - **Mitigation:** ADR superseding process (mark old ADRs as superseded)
- **Risk: Over-documentation** - ADRs become too verbose to be useful
  - **Mitigation:** Keep ADRs focused, use examples over prose

## Implementation

### When to Create a New ADR

Create an ADR when you encounter:

1. **Recurring pattern** - Same issue appears multiple times
2. **Architectural decision** - Fundamental choice about structure
3. **Convention definition** - Standard way to do something
4. **Quality standard** - Measurable expectation
5. **Tool/Library choice** - Why we use X instead of Y
6. **Process definition** - How we handle a workflow

**Test:** If you're tempted to hardcode a pattern, ask: **"Could an ADR solve this?"**

### When to Update an Existing ADR

Update an ADR when:

1. **Pattern evolves** - Better approach discovered
2. **Context changes** - Original rationale no longer valid
3. **Consequences clarified** - Real-world impact differs from expected
4. **Examples needed** - Users struggling to apply pattern
5. **Conflicts found** - ADR contradicts another ADR

**Process:**
- Add new version section at bottom
- Update "Status" if superseding (e.g., "Superseded by ADR-XXX")
- Explain why change was made
- Keep old content for historical context

### When to Write Code Logic Instead

Write code logic when:

1. **Workflow automation** - Orchestrating steps (not defining how)
2. **Error handling** - Recovery from runtime failures
3. **Integration glue** - Connecting systems
4. **Performance optimization** - Caching, batching, parallelization
5. **Security enforcement** - Authentication, authorization, encryption
6. **Infrastructure** - Provisioning, configuration, deployment

**Test:** If the logic isn't about "how to do it right" but "what happens when", use code.

### ADR Maintenance Checklist

When reviewing PRs, check:

- [ ] Does this PR introduce a new pattern?
  - If yes, create ADR documenting it
- [ ] Does this PR violate an existing ADR?
  - If yes, update code OR update ADR with rationale
- [ ] Does this PR make an ADR obsolete?
  - If yes, update ADR status to "Superseded"
- [ ] Could this PR's complexity be reduced with better docs?
  - If yes, consider documentation-driven approach instead

### Knowledge Base Ingestion

For ADRs to guide AI generation, they must be in the knowledge base:

1. **Ingest ADRs** - Run `make ingest-docs` to add ADRs to vector DB
2. **Update code maps** - Run `make generate-code-maps` after structure changes
3. **Verify searchability** - Query KB to confirm ADRs are retrievable
4. **Monitor usage** - Check generate-code logs to see which ADRs are used

**Frequency:** After any ADR change, re-ingest docs before next code generation.

## Related ADRs

- **ADR-001: Create ADRs** - How to write ADRs
- **ADR-006: Python Testing Imports** - Example of documentation-driven solution
- **ADR-005: Testing Standards** - Example of quality standard in ADR

## Examples

### Example 1: Import Pattern Problem

**Scenario:** AI generates tests with `from lambda.X import Y` → SyntaxError

**Wrong Approach (Hardcoded):**
```python
# In run-tests/handler.py
def fix_lambda_imports(file_content: str) -> str:
    """Rewrite invalid lambda imports to sys.path pattern."""
    # Hardcoded Python-specific logic in platform code
    return file_content.replace(
        "from lambda.",
        "# sys.path manipulation here...\nfrom "
    )
```

**Right Approach (Documentation):**
```markdown
# In ADR-006
## Standard Test Import Pattern
```python
import sys
from pathlib import Path

lambda_dir = Path(__file__).parents[2] / "lambda" / "function-name"
sys.path.insert(0, str(lambda_dir))
from handler import handler  # ✅ Correct pattern
```
```

**Result:** AI reads ADR-006 and generates correct imports from the start.

### Example 2: Terraform Formatting

**Scenario:** Generated Terraform files aren't formatted

**Hybrid Approach (Both):**

**Documentation (ADR-004):**
- Terraform files must be formatted with `terraform fmt`
- Formatting happens before human review
- Format command: `terraform fmt <file>`

**Code (generate-code Lambda):**
```python
# After writing .tf files, format them
if terraform_files:
    format_terraform_files(terraform_files)  # Calls terraform fmt
```

**Result:** Documentation explains WHY, code automates the HOW.

### Example 3: DynamoDB Key Design

**Scenario:** Different Lambdas use different DynamoDB key patterns

**Documentation Approach (ADR):**
```markdown
## DynamoDB Key Standards

**ALWAYS use `PK` and `SK` for DynamoDB keys:**
- Partition key: `PK` (string)
- Sort key: `SK` (string)
- NEVER use descriptive names like `userId` or `documentId` as keys

**Rationale:** Generic names allow table pattern evolution without schema changes.
```

**Result:** All generated Lambdas follow same key pattern automatically.

## Frequently Asked Questions

**Q: Don't we need both code and docs?**
A: Yes! Docs guide WHAT to generate, code handles workflow and automation. The line is: Docs = patterns/conventions, Code = orchestration/integration.

**Q: What if AI ignores the ADR?**
A: This happens due to AI stochasticity. run-tests acts as a safety net, validating and attempting fixes. Humans ultimately review PRs.

**Q: Won't this create ADR sprawl?**
A: Possible, but preferable to code sprawl. ADRs are easier to search, update, and remove than scattered code logic. Be judicious about what deserves an ADR.

**Q: How do we know which ADRs the AI used?**
A: generate-code logs which knowledge base documents it retrieved. Check CloudWatch logs for KB query results.

**Q: What if ADRs conflict?**
A: Mark older ADR as "Superseded by ADR-XXX" and link to the new one. Keep old ADR for historical context but make supersession clear.

## Version History

- v1.0 (2025-11-20): Initial ADR on documentation-driven decision making
