# ADR-017: C# Parser Strategy for Code-Map Backend

## Status: Accepted

## Context

The code-map backend (`lambda/process-repo-summary/backends/`) discovers logical code units across languages. Adding C#/.NET support raised a recurring question: should the backend use a real C# parser (e.g., `tree_sitter_c_sharp`, Roslyn via subprocess, or the `clr-loader` bridge), or stick to the regex/filesystem heuristics used by every existing backend?

Existing backends use the following strategies:

| Backend | Strategy | Why |
|---------|----------|-----|
| Python | stdlib `ast` + filesystem | `ast` is free, in stdlib, fast |
| Java | regex + filesystem | ADR-009 boundaries are directory-driven; no AST needed |
| TypeScript | regex + filesystem | Same |
| React | regex + filesystem | Same |
| ABAP | filename suffix mapping | Each abapGit filename encodes the object type |

None of them deeply parse source. ADR-009 boundaries come from **directory layout** + **build markers** + **filename suffixes** — not from class/method-level analysis.

C# tempts a different choice because:

- Modern C# syntax is genuinely hard to regex (top-level statements, primary constructors, records, file-scoped namespaces, partial classes, expression-bodied members, multi-line attributes, raw string literals, generics with `<>` matching `<`/`>` operators)
- High-quality parsers exist: `tree_sitter_c_sharp` (Python bindings), Roslyn (Microsoft, requires .NET runtime)

But:

- Every backend ships in a Lambda zip. `tree_sitter_c_sharp` adds a native compiled grammar (~few MB) plus the `tree_sitter` runtime (~1 MB)
- Roslyn requires the .NET runtime — not viable inside a Python Lambda
- ADR-009 doesn't need AST data. The discovery questions are: "what directories form a project?" "what folders are conventional boundaries?" "is this a test project?" All filename-level.

This ADR is **repo-local** until lifted to global.

## Decision

**Use regex + filesystem heuristics for the C# code-map backend.** Do not introduce a parser library.

Specifically:

1. **Discovery uses filenames and directory names only.** No `.cs` content parsing for ADR-009 boundary decisions.

2. **Test framework detection uses regex over `.csproj` text** (per ADR-016). `.csproj` is XML, but the patterns we match (`<PackageReference Include="xunit" ...`) are sub-line and unambiguous — no XML parser warranted.

3. **Class/method counts (if added later) use regex.** When we eventually want per-file class/method counts for code-map metadata, use regex patterns scoped enough to avoid the worst pitfalls:
   - Match `class X`, `interface X`, `record X`, `struct X` only at line start after optional whitespace and modifiers
   - Match top-level methods only inside a class body — accept that perfection isn't reachable without an AST
   - Document known false-positive cases in comments

4. **Revisit if and only if** we add a deeper analysis layer (e.g., dependency-graph extraction, breaking-change detection). At that point, evaluate `tree_sitter_c_sharp` against measured benefit vs. Lambda zip impact.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| `tree_sitter_c_sharp` | Real AST, handles all modern C# | +few MB Lambda zip, native build dep, slower cold start | Rejected for v1 |
| Roslyn via subprocess | Microsoft-canonical, perfect accuracy | Requires .NET runtime in Lambda; cross-language process management; overkill for boundary discovery | Rejected |
| Hand-written tokenizer | No deps, full control | High maintenance burden, every C# version requires updates | Rejected |
| Regex + filesystem | Zero new deps, matches every other backend, sufficient for ADR-009 | Will mis-count edge cases (multi-line generics, raw strings) | **Accepted** |

## Consequences

### Positive

- Lambda zip stays small — no native dependencies
- Consistent with all five existing backends — one mental model for contributors
- Cold-start performance unaffected
- Onboarding contributors don't need C# parser expertise

### Tradeoffs

- Class/method counts (when added) will be approximate. This is acceptable because counts are metadata for prompts, not authoritative facts.
- If a customer's repo uses very unusual C# constructs (e.g., heavy preprocessor `#if` branching), regex may misclassify. The repo owner can fall back to manual `.outcomeops.yaml` overrides.

## Implementation

The decision is enforced by **not** adding any new dependency to `lambda/process-repo-summary/requirements.txt` for C# support. The C# backend is pure-Python, stdlib-only, matching `python_backend.py` and `java_backend.py`.

If a future contributor proposes adding `tree_sitter_c_sharp`, this ADR must be revisited and superseded.

## Related ADRs

- ADR-006: Python Dependency Management (don't add deps lightly)
- ADR-009: Code Unit Discovery Pattern (boundaries are directory-level)
- ADR-014: C# Code Unit Discovery (this repo)
- ADR-016: C# Test Discovery (this repo) — also regex-only

## References

- tree-sitter-c-sharp: https://github.com/tree-sitter/tree-sitter-c-sharp
- Roslyn: https://github.com/dotnet/roslyn
- AWS Lambda zip size limits: https://docs.aws.amazon.com/lambda/latest/dg/gettingstarted-limits.html

Version History:

- v1.0 (2026-04-30): Initial decision — regex/filesystem only, parity with peer backends

<!-- Confluence sync -->
