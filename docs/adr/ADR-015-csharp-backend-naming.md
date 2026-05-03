# ADR-015: C# Backend Naming and Registration

## Status: Accepted

## Context

ADR-010 established that backends are named by **language**, not framework. Existing registrations:

| Language | Registry key | Class | File |
|----------|-------------|-------|------|
| Python | `python` | `PythonBackend` | `python_backend.py` |
| Java/Kotlin | `java` | `JavaBackend` | `java_backend.py` |
| TypeScript | `typescript` | `TypeScriptBackend` | `typescript_backend.py` |
| React | `react` | `ReactBackend` | `react_backend.py` |
| ABAP | `abap` | `ABAPBackend` | `abap_backend.py` |

Adding C#/.NET requires picking a name that obeys ADR-010 ("language, not framework"), reads naturally in `.outcomeops.yaml`, and avoids collisions with C, C++, or `.NET` (multi-language CLR).

This ADR is **repo-local** until lifted to global.

## Decision

### Registry Key

Use **`csharp`** as the backend registry key.

```yaml
# .outcomeops.yaml
code_generation:
  backend: "csharp"
```

Rejected alternatives:

| Candidate | Why rejected |
|-----------|--------------|
| `c#` | `#` is shell/URL/YAML hostile |
| `cs` | Collides with `cs` as in "Computer Science" and is overloaded with `.cs` extension shorthand |
| `dotnet` | Violates ADR-010 — describes a runtime, not a language; would also need to cover F#/VB.NET |
| `net` | Collides with the `net` TLD prefix in package metadata; ambiguous |

`csharp` is the same convention used by GitHub Linguist, the `tree-sitter-c-sharp` package, and the `csharp-language-server-protocol` project.

### Class Name

`CSharpBackend` (PascalCase, matching `JavaBackend`, `ABAPBackend`). Not `CsharpBackend` and not `CSharpCodeMapBackend` — the suffix is implied by the module location.

### File Name

`csharp_backend.py` (snake_case, matching all peers).

### Storage Key Prefix

All C# code unit storage keys begin with `summary#csharp#`:

```
summary#csharp#project#<project-name>
summary#csharp#controllers#<project-name>
summary#csharp#services#<project-name>
summary#csharp#views#<project-name>
summary#csharp#models#<project-name>
```

This isolates C# units from `summary#handler#`, `summary#frontend#`, etc., so DynamoDB queries by prefix scope cleanly.

### Registration

Auto-register at module import time, matching the pattern in `python_backend.py`:

```python
# At end of csharp_backend.py
try:
    from .factory import register_backend
except ImportError:
    from factory import register_backend  # noqa: F401
register_backend("csharp", CSharpBackend)
```

And add to `lambda/process-repo-summary/backends/__init__.py`:

```python
from . import csharp_backend  # noqa: F401
# (or absolute import in the ImportError fallback block, matching peers)
```

### Backend Type

`get_backend_type()` returns `"csharp"`. `get_backend_name()` returns `"C# / .NET Backend"`.

### Test File

`lambda/tests/unit/test_csharp_backend.py` — matches ADR-013 import-structure pattern (importlib-based loading of the backend module).

## Consequences

### Positive

- Predictable naming for all five existing backends + the new one
- `csharp` is unambiguous in YAML, shell, URLs, and config
- Storage-key prefix enables clean DynamoDB query patterns
- Mirrors industry convention (GitHub Linguist, tree-sitter, LSP)

### Tradeoffs

- If we ever add F# or VB.NET they will need their own keys (`fsharp`, `vbnet`) — consistent with ADR-010 but slightly more work than a single `dotnet` backend
- Customers migrating from a hypothetical earlier `dotnet`-named backend would have to update config — not a concern today (no prior backend exists)

## Implementation

1. Create `lambda/process-repo-summary/backends/csharp_backend.py` with class `CSharpBackend(CodeMapBackend)`
2. Auto-register as `csharp` at end of file
3. Add `from . import csharp_backend` to `__init__.py` in both try/except branches
4. Update `lambda/process-repo-summary/backends/__init__.py` module docstring to list C# backend
5. Add `csharp` to any backend-consistency tests (`test_backend_consistency.py`) and factory tests (`test_backend_factory.py`)

## Related ADRs

- ADR-010: Backend Naming Conventions (parent rule)
- ADR-014: C# Code Unit Discovery (this repo)
- ADR-016: C# Test Discovery (this repo)
- ADR-017: C# Parser Strategy (this repo)

Version History:

- v1.0 (2026-04-30): Initial decision

<!-- Confluence sync -->
