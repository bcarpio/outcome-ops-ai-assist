# ADR-010: Backend Naming Conventions

## Status: Accepted

## Context

OutcomeOps uses pluggable backends to support different programming languages and frameworks. Two Lambda functions use these backends:

- **generate-code-maps**: Discovers code units and generates summaries
- **generate-code**: Generates code from plans using LLM

Users configure which backend to use via `.outcomeops.yaml`:

```yaml
code_generation:
  backend: "python"  # or "java", "typescript"
```

Initially, backend naming was inconsistent between the two systems:

| Language | generate-code-maps | generate-code |
|----------|-------------------|---------------|
| Python | `lambda` | `python-lambda` |
| Java | `java` | `java-spring` |
| TypeScript | `typescript` | `typescript-express` |

This inconsistency caused:
1. **User confusion** - Different config values needed for different operations
2. **Framework coupling** - Names like `java-spring` assumed Spring Boot, but backends should be language-agnostic
3. **Maintenance burden** - Alias mappings or translation layers required

## Decision

Use **short, language-based names** for all backends:

| Language | Backend Name |
|----------|-------------|
| Python | `python` |
| Java | `java` |
| TypeScript | `typescript` |

### Rationale

1. **Language, not framework**: Backends should represent the language, not a specific framework. Framework-specific patterns (Spring Boot, Express, Lambda) should be configured via the repository's ADRs, not hardcoded in backend names.

2. **User-owned standards**: Users define their coding standards in their own ADRs (e.g., "ADR-005: Java Spring Boot Conventions"). The backend queries these standards from the knowledge base rather than assuming a framework.

3. **Single config value**: Users can use `backend: "python"` and it works for both code map generation and code generation.

4. **Extensibility**: Future frameworks (FastAPI, Quarkus, Next.js) don't require new backend names - they're all covered by the language backend plus user ADRs.

## Implementation

### File Structure

```
generate-code/codegen_backends/
├── __init__.py
├── base.py
├── factory.py
├── python.py      # was python_lambda.py
├── java.py        # was java_spring.py
└── typescript.py  # was typescript_express.py

generate-code-maps/backends/
├── __init__.py
├── base.py
├── factory.py
├── python_backend.py   # was lambda_backend.py
├── java_backend.py
└── typescript_backend.py
```

### Registration

```python
# generate-code/codegen_backends/__init__.py
register_backend("python", PythonCodeGenerator)
register_backend("java", JavaCodeGenerator)
register_backend("typescript", TypeScriptCodeGenerator)

# generate-code-maps/backends/python_backend.py
register_backend("python", PythonBackend)
```

### Configuration

```yaml
# .outcomeops.yaml
code_generation:
  backend: "python"  # Works for both systems
```

### Default Backend

The default backend is `python` when no `.outcomeops.yaml` is present:

```python
def get_backend_for_repo(
    repo_config: Optional[Dict[str, Any]] = None,
    default: str = "python",
) -> CodeGeneratorBackend:
```

## Consequences

### Positive

- **Consistent UX**: One backend name works everywhere
- **Decoupled from frameworks**: Backend handles the language; ADRs handle the patterns
- **Simpler testing**: `test_backend_consistency.py` verifies all backends exist in both systems
- **Future-proof**: New frameworks don't require new backend names

### Negative

- **Migration required**: Existing `.outcomeops.yaml` files using old names (`python-lambda`, `java-spring`) need updating
- **Less explicit**: `java` is less descriptive than `java-spring`, but this is intentional - framework patterns come from ADRs

## Migration

Users with existing `.outcomeops.yaml` files should update:

```yaml
# Before
code_generation:
  backend: "python-lambda"

# After
code_generation:
  backend: "python"
```

## Related ADRs

- **ADR-009**: Code Unit Discovery Pattern - Defines how backends discover code units
- **ADR-006**: Python Dependency Management - Example of framework-specific standards in user ADRs

<!-- Confluence sync -->
