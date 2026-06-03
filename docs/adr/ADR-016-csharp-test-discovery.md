# ADR-016: C# Test Project Discovery

## Status: Superseded by ADR-017 (C# parser strategy) + ADR-028 (graph-first code-map generation)

> The C# test-project discovery heuristic this ADR defined was the
> pre-symbol-graph approach to identifying test boundaries in C#
> repos. The current platform uses the C# tree-sitter extractor
> + the tests-summary structural batch type to identify test
> projects via graph facts. Retained for historical context; new
> C# test work should reference ADR-017 and ADR-028.

## Original Status: Accepted

## Context

C#/.NET test code lives in **dedicated test projects** rather than colocated test directories (the way Python uses `tests/` next to the source). A typical F500 .NET solution has parallel projects:

```
src/
  MyApp.Domain/MyApp.Domain.csproj
  MyApp.Api/MyApp.Api.csproj
tests/
  MyApp.Domain.Tests/MyApp.Domain.Tests.csproj          # xUnit
  MyApp.Api.IntegrationTests/MyApp.Api.IntegrationTests.csproj  # NUnit
```

The code-map backend has to:

1. Recognize a project as a test project (vs production code)
2. Group tests by category (unit / integration / e2e)
3. Identify the test framework (xUnit / NUnit / MSTest) for downstream prompt context

Three test frameworks dominate F500 .NET:

| Framework | Marker package | Share in F500 .NET |
|-----------|----------------|---------------------|
| xUnit | `xunit` + `xunit.runner.visualstudio` | ~50% (modern default) |
| NUnit | `NUnit` + `NUnit3TestAdapter` | ~30% (legacy enterprise) |
| MSTest | `MSTest.TestFramework` + `MSTest.TestAdapter` | ~20% (older Microsoft shops) |

This ADR is **repo-local** until lifted to global.

## Decision

### Test Project Identification

A project is classified as a test project if **any** of these are true (ordered by signal strength):

1. **Project filename matches a test pattern.** The project's `.csproj` filename matches a case-insensitive regex:
   ```
   ^.+\.(Tests?|UnitTests?|IntegrationTests?|E2ETests?|EndToEndTests?|AcceptanceTests?|SystemTests?|FunctionalTests?)\.csproj$
   ```

2. **Directory name matches the same suffix family** (covers the rare case where the `.csproj` filename diverges from the directory).

3. **Project file content references a known test framework package.** Heuristic match (regex over the raw `.csproj` text — no XML parser needed):
   - `<PackageReference Include="xunit"`
   - `<PackageReference Include="NUnit"`
   - `<PackageReference Include="MSTest.TestFramework"`
   - `<PackageReference Include="Microsoft.NET.Test.Sdk"` (necessary but not sufficient — combined with one of the above)

   Heuristic 3 is opt-in: only consulted if heuristics 1 and 2 don't decide. For repos where teams omit `Tests` from project names, content matching is the fallback.

### Test Group Categorization

After a project is classified as a test project, it is grouped by category using the suffix that matched in heuristic 1 or 2:

| Suffix matched | Group |
|----------------|-------|
| `.Tests`, `.UnitTests`, `.Test` | `unit` |
| `.IntegrationTests`, `.IntegrationTest` | `integration` |
| `.E2ETests`, `.EndToEndTests` | `e2e` |
| `.AcceptanceTests`, `.SystemTests`, `.FunctionalTests` | `functional` |
| (content-only match) | `unit` (default) |

If a `Tests/` or `test/` directory contains files that don't belong to any project (rare but happens in old solutions), they are grouped as `other`.

### Storage Keys

Aligns with existing test storage convention used by all backends:

```
summary#tests#unit
summary#tests#integration
summary#tests#e2e
summary#tests#functional
summary#tests#other
```

C# test units do **not** get a `csharp` infix in the storage key, because the test categorization is language-agnostic (Python, Java, TS all share `summary#tests#unit`). The `metadata` dict on the `CodeUnit` carries `language: "csharp"` and `framework: "xunit" | "nunit" | "mstest" | "unknown"` so downstream consumers can prompt-tune.

### Excluded From Test Discovery

- `*.Benchmarks.csproj` — benchmark projects (BenchmarkDotNet) are not test code, treat as a regular `csharp-project`
- Sample/demo projects (`Samples/`, `Examples/`, `Demo/`) — treated as regular projects unless they also match a test suffix
- Generated test project scaffolding (`obj/`, `bin/`) — already excluded per ADR-014

## Consequences

### Positive

- Same `summary#tests#<group>` storage convention as Python/Java/TS — downstream consumers don't need C#-specific routing
- Framework metadata threaded through `CodeUnit.metadata` keeps the discovery layer language-neutral while preserving signal for code generation
- Filename heuristic catches >95% of F500 test projects without needing to parse `.csproj` XML

### Tradeoffs

- A test project with a non-conventional name AND no recognizable test package will be missed. Acceptable: such projects are rare and the repo owner can rename them or add a custom config.
- Combined unit + integration + e2e tests in a single project (anti-pattern but seen in legacy code) all map to `unit`. Acceptable trade for simplicity.

## Implementation

In `csharp_backend.py`:

```python
TEST_PROJECT_SUFFIX_RE = re.compile(
    r'\.(Tests?|UnitTests?|IntegrationTests?|E2ETests?|EndToEndTests?|'
    r'AcceptanceTests?|SystemTests?|FunctionalTests?)$',
    re.IGNORECASE,
)

TEST_FRAMEWORK_PATTERNS = {
    "xunit":  re.compile(r'PackageReference\s+Include="xunit"', re.IGNORECASE),
    "nunit":  re.compile(r'PackageReference\s+Include="NUnit"', re.IGNORECASE),
    "mstest": re.compile(r'PackageReference\s+Include="MSTest\.TestFramework"',
                         re.IGNORECASE),
}

def _classify_test_project(self, csproj_path: str, csproj_content: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return (group, framework) if csproj is a test project, else None."""
    name = csproj_path.rsplit("/", 1)[-1].rsplit(".csproj", 1)[0]
    suffix_match = TEST_PROJECT_SUFFIX_RE.search(name)

    framework = "unknown"
    if csproj_content:
        for fw, pattern in TEST_FRAMEWORK_PATTERNS.items():
            if pattern.search(csproj_content):
                framework = fw
                break

    if not suffix_match and framework == "unknown":
        return None

    group = self._suffix_to_group(suffix_match.group(1) if suffix_match else None)
    return group, framework
```

Project-file content (`csproj_content`) is fetched lazily via the GitHub raw content API only when the filename heuristic is inconclusive — this keeps API call counts bounded.

## Related ADRs

- ADR-005: Testing Standards (parent quality standard)
- ADR-009: Code Unit Discovery Pattern (parent algorithm)
- ADR-014: C# Code Unit Discovery (this repo)
- ADR-015: C# Backend Naming (this repo)

## References

- xUnit: https://xunit.net/
- NUnit: https://nunit.org/
- MSTest: https://learn.microsoft.com/dotnet/core/testing/unit-testing-with-mstest

Version History:

- v1.0 (2026-04-30): Initial decision — filename + content heuristic, three frameworks

<!-- Confluence sync -->
