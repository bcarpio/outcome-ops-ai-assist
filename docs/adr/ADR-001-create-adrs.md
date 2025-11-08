# ADR-001: Creating Architecture Decision Records (ADRs)

## Status: Accepted

## Context

OutcomeOps AI Assist uses Architecture Decision Records (ADRs) to document architectural decisions and engineering standards. These records serve as the knowledge base for AI-assisted code generation—Claude uses your ADRs to understand your patterns, conventions, and architectural intent.

When you use OutcomeOps as a template for your project, you should create your own ADRs documenting:
- Domain models and business logic patterns
- Infrastructure standards specific to your application
- API contracts and data formats
- Technology choices and their rationale
- Development workflow and conventions

This ADR explains the purpose of ADRs in OutcomeOps and provides a template for creating your own.

## Decision

### 1. Purpose of ADRs in OutcomeOps

ADRs are the primary input to the knowledge base. When you run the ingestion pipeline:

```
docs/adr/*.md
    ↓
[ingest-docs Lambda]
    ↓
Generate embeddings via Bedrock
    ↓
Store in DynamoDB with metadata
    ↓
Available for Claude queries + code generation
```

Your ADRs guide Claude when you ask:
- "Create a new Lambda handler for..."
- "Generate Terraform for a new..."
- "Write tests for..."

**Better ADRs = Better generated code.**

### 2. ADR Template

Use this template for all new ADRs in your project:

```markdown
# ADR-NNN: Title of Decision

## Status: Proposed | Accepted | Deprecated | Superseded by ADR-XXX

## Context

Describe the issue or problem this decision addresses.
- What's the background?
- Why does this decision matter?
- What constraints or requirements drive this choice?

## Decision

Clearly state what you decided and why.

### Key points:
- Decision 1 with explanation
- Decision 2 with explanation
- Pattern or example code

**Example:**

\`\`\`python
def handler(event, context):
    # Your pattern here
    pass
\`\`\`

## Consequences

### Positive
- Benefit 1
- Benefit 2

### Tradeoffs
- Tradeoff 1
- Tradeoff 2

## Implementation

### Starting today
1. Do this first
2. Then do this

### Next phases
1. Future improvement 1
2. Future improvement 2

## Related ADRs

- ADR-X: Related decision
- ADR-Y: Another related decision

## References

- Link to documentation
- Link to examples

Version History:
- v1.0 (YYYY-MM-DD): Initial decision
```

### 3. ADR Naming Convention

```
ADR-NNN-kebab-case-title.md
```

**Examples:**
- ADR-001-terraform-infrastructure-patterns.md
- ADR-002-lambda-handler-standards.md
- ADR-003-domain-model-definitions.md
- ADR-004-subscription-payment-flow.md

### 4. Where ADRs Live

```
your-project/
├── docs/
│   ├── adr/                           # All ADRs go here
│   │   ├── ADR-001-your-first-adr.md
│   │   ├── ADR-002-your-second-adr.md
│   │   └── ...
│   └── architecture.md                # Optional: high-level overview
└── README.md
```

All markdown files in `docs/adr/` are automatically ingested into the knowledge base.

### 5. ADR Best Practices

**Be specific:**
- ❌ "We use lambdas"
- ✅ "Lambda handlers follow this structure with Pydantic validation and this error handling pattern"

**Include examples:**
- Show code that exemplifies the decision
- Include actual patterns people should follow
- Link to working implementations

**Document tradeoffs:**
- Why you chose this over alternatives
- What you gave up
- When you might reconsider

**Keep them focused:**
- One decision per ADR
- Cross-reference related ADRs
- Update existing ADRs rather than creating duplicates

**Use consistent format:**
- All ADRs follow the template above
- Consistent structure helps Claude understand patterns
- Makes ADRs easier to skim and search

### 6. Updating ADRs

When you discover a better pattern:

1. **Update the existing ADR** with new information
2. **Mark as Superseded** if you're replacing it entirely:
   ```markdown
   ## Status: Superseded by ADR-007
   ```
3. **Reference the new ADR** from the old one
4. **Commit with message**: `docs(adr): update ADR-NNN with new pattern`

### 7. Ingesting Your ADRs

ADRs are ingested automatically when you deploy:

```bash
# In your infrastructure
cd terraform
terraform apply -var-file=dev.tfvars
```

The ingest-docs Lambda:
1. Scans `docs/adr/` in your repository
2. Reads all markdown files
3. Generates embeddings via AWS Bedrock
4. Stores in DynamoDB knowledge base
5. Makes available for Claude queries

To manually re-ingest:

```bash
aws lambda invoke \
  --function-name dev-outcome-ops-ai-assist-ingest-docs \
  response.json
```

### 8. Example: Starting Your ADRs

When you fork OutcomeOps for your project, start with ADRs for:

**1. Domain Model (ADR-001 or ADR-002)**
```
# ADR-001: Domain Model

## Context
Our application is a [description]. We need to define how our core entities work.

## Decision
Our domain model consists of:
- User: represents authenticated users
- Account: billing and subscription
- [Your entities]: [description]

Each entity has these fields:
- [List them]
```

**2. Infrastructure Standards (ADR-002 or ADR-003)**
```
# ADR-002: Infrastructure Patterns

## Decision
We follow these patterns:
- Lambda handlers use Pydantic for validation
- DynamoDB tables use PK/SK pattern
- All resources prefixed with {environment}-{app_name}
```

**3. API Contract (ADR-003 or ADR-004)**
```
# ADR-003: API Standards

## Decision
All endpoints return:
- 200/201 for success
- 400 for validation errors
- 401 for auth errors
- [etc]

Response format:
\`\`\`json
{ "status": "success", "data": {...} }
\`\`\`
```

And so on for your specific needs.

## Consequences

### Positive
- Claude has your patterns encoded in the knowledge base
- Generated code matches your conventions
- New team members (or yourself later) can understand decisions
- ADRs become searchable documentation
- Patterns improve iteratively as you refine them

### Tradeoffs
- Takes time to write ADRs upfront (pays off immediately)
- Discipline to keep ADRs updated (worth it for consistency)
- Requires thinking through decisions clearly (good discipline)

## Implementation

### Starting today
1. Copy this template to `docs/adr/TEMPLATE.md`
2. Create ADR-001 for your domain model or core infrastructure
3. Create 2-3 more ADRs for your key decisions
4. Deploy infrastructure (ADRs get ingested automatically)
5. Test by querying: `outcome-ops-assist "What's our standard for...?"`

### Next phases
1. Add more ADRs as you make new decisions
2. Update ADRs when patterns evolve
3. Reference ADRs in code reviews
4. Let Claude generate code based on your ADRs

## Related ADRs

This is the only ADR in the OutcomeOps framework itself. Your project will have its own ADRs.

See [fatacyai-adrs](https://github.com/bcarpio/fatacyai-adrs) for example ADRs in a real implementation.

## References

- [Markdown ADR Template](https://github.com/joelparkerhenderson/architecture_decision_record)
- [ADR Best Practices](https://adr.github.io/)
- [OutcomeOps Knowledge Base](https://github.com/bcarpio/outcome-ops-ai-assist#knowledge-base-ingestion)

Version History:
- v1.0 (2025-01-02): Initial ADR explaining ADRs and providing template
