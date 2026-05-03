# Generate Code: C# / .NET Backend

This document describes the C# / .NET backend for code generation, which handles
.NET applications of any vintage - .NET Framework 4.x services, modern .NET
(Core / 5 / 6 / 7 / 8) microservices, ASP.NET MVC web apps, ASP.NET Core Web
APIs, ASP.NET Web Forms legacy modules, and WCF service hosts.

## Overview

The C# backend (`csharp`) is **language-focused, not framework-focused**. It
handles:
- File and project discovery (`.csproj`, `.sln`, `Directory.Build.props`)
- Test patterns (xUnit / NUnit / MSTest conventions)
- Multi-project solution layouts (5-50 projects per repo)

**Framework-specific patterns** (ASP.NET Core controllers, MediatR handlers,
EF Core DbContexts, MassTransit consumers) come from your repository's ADRs and
the knowledge base - not from hardcoded prompts.

Per ADR-017, this backend uses regex and filesystem heuristics only. There is
no Roslyn or `tree-sitter` dependency. Discovery decisions are filename-driven.

## How It Works

1. **Knowledge Base Queries**: The backend queries the KB for generic .NET
   patterns
2. **ADR-Driven**: Your repo's ADRs teach Claude your specific framework
   conventions
3. **Project Context**: `.outcomeops.yaml` provides project-specific structure
   information
4. **Code Maps**: The generate-code-maps Lambda discovers your existing
   patterns

This means the same C# backend works for:
- ASP.NET Core Web APIs
- ASP.NET Core MVC + Razor applications
- ASP.NET MVC 5 / Web API 2 (legacy Framework)
- ASP.NET Web Forms (`.aspx`, `.ascx`, `.ashx`)
- WCF service hosts (`.svc`)
- Console / Worker Service applications
- Class libraries targeting .NET Standard 2.0
- Any C# / .NET project

## Supported Scope

### Frameworks

- .NET Framework 4.x (legacy)
- .NET Core / .NET 5 / 6 / 7 / 8 (modern)
- .NET Standard 2.0 libraries

### Project formats

- Old-style `.csproj` (with `<Compile Include="..." />`)
- SDK-style `.csproj` (`<Project Sdk="Microsoft.NET.Sdk">`)
- Solutions (`.sln`)
- Central package management (`Directory.Packages.props`)
- Shared MSBuild props (`Directory.Build.props`)
- Legacy NuGet (`packages.config`)
- SDK pinning (`global.json`)

### Web stacks

- ASP.NET MVC
- ASP.NET Core MVC + Razor (`.cshtml`)
- ASP.NET Web API
- ASP.NET Web Forms (`.aspx`, `.ascx`, `.ashx`)
- WCF (`.svc`)

### Test frameworks

- xUnit
- NUnit
- MSTest

## Out of Scope (v1)

- F# (`.fs`)
- Blazor (`.razor`)
- WPF / WinForms (`.xaml`)
- VB.NET

These represent <20% of F500 .NET surface area. Customers using them should
file an issue so we can prioritize follow-up discovery rules.

## Configuration

### .outcomeops.yaml

Configure OutcomeOps in your repository. The config file serves two purposes:

1. **Code Map Discovery** (generate-code-maps): Where to find code units
2. **Code Generation** (generate-code): How to generate new code

```yaml
version: "1.0"

code_generation:
  # Backend selection (used by both lambdas)
  backend: "csharp"

  # === Code Map Discovery Options ===
  # These control how generate-code-maps discovers your code structure
  # C# uses .csproj-driven boundaries by default - usually no config needed

  # Override default source roots
  # Default: ["src", "source", "server", "web", "Source", "Src", ""]
  # The empty string represents the repository root - many older
  # .NET Framework repos place projects directly at the top level.
  # source_roots:
  #   - "src"
  #   - "Source"
  #   - "Server"

  # === Code Generation Options ===
  # These control how generate-code creates new code

  # Custom guidelines appended to prompts
  custom_guidelines: |
    Use constructor injection (no service locator pattern).
    Use Microsoft.Extensions.Logging for structured logs.
    Follow async/await all the way - no blocking .Result calls.

  # Additional KB queries for every plan
  additional_kb_queries:
    - "EF Core migration patterns"
    - "ASP.NET Core minimal API patterns"

  # File patterns to exclude from generation
  exclude_patterns:
    - "*.Designer.cs"
    - "*.g.cs"

# Project context - THIS IS KEY for framework-specific generation
# Tell Claude what kind of project this is
project_context: |
  This is an ASP.NET Core 8.0 application following Clean Architecture.

  Solution layout:
  - src/MyApp.Api/             - ASP.NET Core Web API (composition root)
  - src/MyApp.Application/     - Use cases, MediatR handlers
  - src/MyApp.Domain/          - Entities, value objects, domain events
  - src/MyApp.Infrastructure/  - EF Core, external clients
  - tests/MyApp.Api.Tests/             - xUnit unit tests
  - tests/MyApp.Application.Tests/     - xUnit unit tests
  - tests/MyApp.Api.IntegrationTests/  - xUnit + WebApplicationFactory
  - terraform/                 - ECS Fargate infrastructure

  We use SQL Server via EF Core for persistence and Redis for caching.

# Testing configuration
testing:
  framework: "xunit"
  test_directory: "tests"
  min_coverage: 80

# Infrastructure configuration
infrastructure:
  tool: "terraform"
  directory: "terraform"

# ADRs to always include in context
required_adrs:
  - "ADR-004"
```

### ASP.NET Core MVC + Razor Example

```yaml
version: "1.0"

code_generation:
  backend: "csharp"

  custom_guidelines: |
    Use tag helpers in Razor views, not HTML helpers.
    Use action filters for cross-cutting concerns.
    Return ActionResult<T> from controllers, not raw IActionResult.

project_context: |
  This is an ASP.NET Core 8.0 MVC application with Razor views.

  Solution layout:
  - src/MyApp.Web/Controllers/   - MVC controllers
  - src/MyApp.Web/Views/         - Razor (.cshtml) views
  - src/MyApp.Web/Models/        - View models
  - src/MyApp.Web/Services/      - Application services
  - src/MyApp.Domain/            - Domain entities
  - tests/MyApp.Web.Tests/       - xUnit + Moq

  We use SQL Server via EF Core 8.

testing:
  framework: "xunit"
  test_directory: "tests"

infrastructure:
  tool: "terraform"
  directory: "terraform"
```

### Legacy .NET Framework 4.8 Web API Example

```yaml
version: "1.0"

code_generation:
  backend: "csharp"

  source_roots:
    - ""           # legacy repo with projects at root

  custom_guidelines: |
    This is a legacy .NET Framework 4.8 application.
    Use ASP.NET Web API 2 conventions (ApiController, IHttpActionResult).
    Use Unity for dependency injection.
    Do not introduce async APIs without explicit approval.

project_context: |
  This is a legacy .NET Framework 4.8 ASP.NET Web API application.

  Solution layout:
  - MyLegacyApp.Web/              - ASP.NET Web API 2 host
  - MyLegacyApp.Services/         - Business logic
  - MyLegacyApp.Data/             - Entity Framework 6 + repositories
  - MyLegacyApp.Tests/            - NUnit unit tests
  - packages.config               - Legacy NuGet (one per project)
  - Web.config                    - Connection strings, IIS settings

  We use SQL Server via Entity Framework 6.

testing:
  framework: "nunit"
  test_directory: ""

infrastructure:
  tool: "terraform"
  directory: "terraform"
```

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `BACKEND_TYPE` | Backend to use | `csharp` |

## Code Units Produced

| `unit_type` | Source | Storage key |
|-------------|--------|-------------|
| `csharp-project` | One per `.csproj` | `summary#csharp#project#<project>` |
| `csharp-controllers` | `Controllers/` per project | `summary#csharp#controllers#<project>` |
| `csharp-services` | `Services/` per project | `summary#csharp#services#<project>` |
| `csharp-views` | `Views/` `Pages/` `Areas/` per project | `summary#csharp#views#<project>` |
| `csharp-models` | `Models/` `Entities/` `DTOs/` per project | `summary#csharp#models#<project>` |
| `tests` | Test projects (see below) | `summary#tests#<group>` |
| `infrastructure` | `*.tf`, `*.tfvars` | `summary#infrastructure` |
| `docs` | `*.md` | `summary#docs` |
| `kubernetes` | `k8s/`, `helm/`, etc. YAML | `summary#kubernetes` |

Project-level units **dominate**: when a `.csproj` is present in a directory,
the entire project tree (excluding subprojects) becomes one or more `csharp-*`
units rooted at that project. Subdirectory units (`Controllers/`, etc.) are
scoped to their parent project so multi-project solutions don't collapse into
a single namespace.

## Code Unit Discovery (ADR-009 / ADR-014)

The C# backend layers C#-specific rules onto the generic ADR-009 algorithm.

### Discovery Criteria

A directory becomes a code unit when it meets ANY of these:

1. **Build-Module Marker**: Contains `*.csproj`, `*.sln`,
   `Directory.Packages.props`, `Directory.Build.props`, `packages.config`,
   or `global.json`
2. **Boundary Pattern Match**: Directory name matches a known boundary pattern
3. **File Count Threshold**: Contains 2+ source files directly

### Source Extensions

The following are treated as C# source files for discovery:

| Extension | Purpose |
|-----------|---------|
| `.cs` | Primary C# source (classes, interfaces, records, enums) |
| `.cshtml` | Razor views (ASP.NET MVC, ASP.NET Core MVC) |
| `.aspx` | ASP.NET Web Forms pages |
| `.aspx.cs` | Web Forms code-behind |
| `.ascx` | Web Forms user controls |
| `.ashx` | Generic HTTP handlers |
| `.svc` | WCF service host markers |

### Boundary Patterns

In addition to the inherited ADR-009 patterns, these directory names indicate
a C#-specific boundary:

| Category | Patterns |
|----------|----------|
| MVC / Web | `controllers`, `viewmodels`, `views`, `pages`, `areas` |
| Layered architecture | `infrastructure`, `persistence`, `repositories`, `domain`, `application`, `presentation` |
| Real-time / pipeline | `hubs`, `middleware`, `filters`, `attributes` |
| Cross-cutting | `extensions`, `abstractions`, `contracts` |
| Data | `migrations`, `seeders` |

### MVC Sub-Unit Folders

Inside each `csharp-project`, these conventional folder names emit dedicated
sub-units when they meet the file-count threshold:

| Folder | Sub-unit kind | Allowed extensions |
|--------|---------------|--------------------|
| `Controllers/` | `csharp-controllers` | `.cs` |
| `Services/` | `csharp-services` | `.cs` |
| `Views/`, `Pages/`, `Areas/` | `csharp-views` | `.cshtml`, `.cs` |
| `Models/`, `Entities/`, `DTOs/` | `csharp-models` | `.cs` |
| `Repositories/` | `csharp-repositories` | `.cs` |
| `Domain/` | `csharp-domain` | `.cs` |

### Namespace Container Handling

Deep `Company.Product.Module.Submodule.Internal` directory hierarchies
produced by mirroring C# namespaces into folders are handled by the existing
namespace-container logic in `base.py` (depth > 4 with no source files →
traverse through). No C#-specific change is needed.

### Multi-Project Solution Example

```
MySolution.sln
src/
  MyApp.Web/MyApp.Web.csproj            -> csharp-project "MyApp.Web"
  MyApp.Api/MyApp.Api.csproj            -> csharp-project "MyApp.Api"
  MyApp.Domain/MyApp.Domain.csproj      -> csharp-project "MyApp.Domain"
  MyApp.Infrastructure/...              -> csharp-project "MyApp.Infrastructure"
tests/
  MyApp.Web.Tests/...                   -> tests group "unit"
  MyApp.Api.IntegrationTests/...        -> tests group "integration"
```

Four `csharp-project` units plus two `tests` units, plus per-project MVC
sub-units where the threshold is met (e.g., a `csharp-controllers` unit for
`MyApp.Web` if `Controllers/` has two or more `.cs` files).

### Single Old-Style Project Example

```
MyLegacyApp/
  MyLegacyApp.csproj      (old-style, .NET Framework 4.8)
  Web.config
  packages.config
  Default.aspx
  Default.aspx.cs
  Controllers/
    HomeController.cs
    AccountController.cs
  Views/
    Home/Index.cshtml
    Account/Login.cshtml
```

Produces one `csharp-project` unit (`MyLegacyApp`), one `csharp-controllers`
sub-unit (two controllers meets the threshold), and one `csharp-views`
sub-unit.

## Test Project Recognition (ADR-016)

C# test code lives in **dedicated test projects** rather than colocated test
directories (the way Python uses `tests/` next to source).

### Test Project Identification

A `.csproj` is classified as a test project when **any** of these are true,
ordered by signal strength:

1. **Project filename matches a test suffix** (case-insensitive):

   ```
   *.Tests.csproj, *.Test.csproj
   *.UnitTests.csproj, *.UnitTest.csproj
   *.IntegrationTests.csproj
   *.E2ETests.csproj, *.EndToEndTests.csproj
   *.AcceptanceTests.csproj, *.SystemTests.csproj, *.FunctionalTests.csproj
   ```

2. **Directory name matches the same suffix family** (covers the rare case
   where the `.csproj` filename diverges from the directory).

3. **`.csproj` content references a known test framework package**
   (regex over the raw `.csproj` text - no XML parser needed):

   - `<PackageReference Include="xunit" />`
   - `<PackageReference Include="NUnit" />`
   - `<PackageReference Include="MSTest.TestFramework" />`

   Heuristic 3 is opt-in: only consulted if 1 and 2 don't decide.

### Test Group Categorization

| Suffix matched | Group |
|----------------|-------|
| `Tests`, `Test`, `UnitTests`, `UnitTest` | `unit` |
| `IntegrationTests`, `IntegrationTest` | `integration` |
| `E2ETests`, `EndToEndTests` | `e2e` |
| `AcceptanceTests`, `SystemTests`, `FunctionalTests` | `functional` |
| (content-only match) | `unit` (default) |

Framework (`xunit`, `nunit`, `mstest`, or `unknown`) is carried in the unit's
`metadata.framework` for downstream prompt context. When a single group
contains multiple frameworks, the metadata value is `mixed`.

### Excluded From Test Discovery

- `*.Benchmarks.csproj` - BenchmarkDotNet projects, treated as regular
  `csharp-project`
- Sample/demo projects (`Samples/`, `Examples/`, `Demo/`) - treated as regular
  projects unless they also match a test suffix
- Generated test scaffolding (`obj/`, `bin/`) - already excluded per ADR-014

## Excluded Paths

`bin/`, `obj/`, `packages/`, `.vs/`, `TestResults/`, `artifacts/`,
`Properties/`, `_ReSharper.Caches/`, plus the standard exclusions
(`node_modules/`, `vendor/`, `.git/`, etc.).

`Properties/` is excluded because it contains `AssemblyInfo.cs` and
`launchSettings.json`, neither of which represent code units.

## DynamoDB Storage

Code maps are stored with this key schema:

| Code Unit | PK | SK |
|-----------|----|----|
| Project | `repo#<repo_name>` | `summary#csharp#project#<project>` |
| Controllers | `repo#<repo_name>` | `summary#csharp#controllers#<project>` |
| Services | `repo#<repo_name>` | `summary#csharp#services#<project>` |
| Views | `repo#<repo_name>` | `summary#csharp#views#<project>` |
| Models | `repo#<repo_name>` | `summary#csharp#models#<project>` |
| Tests | `repo#<repo_name>` | `summary#tests#<group>` |
| Infrastructure | `repo#<repo_name>` | `summary#infrastructure` |
| Kubernetes | `repo#<repo_name>` | `summary#kubernetes` |
| Docs | `repo#<repo_name>` | `summary#docs` |

C# test units do **not** get a `csharp` infix in the storage key, because the
test categorization is language-agnostic (Python, Java, TS all share
`summary#tests#unit`). The `metadata` dict on the `CodeUnit` carries
`language: "csharp"` and `framework: "xunit" | "nunit" | "mstest" | "unknown"`
so downstream consumers can prompt-tune.

## Incremental Updates

The C# backend supports incremental updates via git-based change detection:

1. **State Tracking**: Stores last processed commit SHA per repository
2. **Change Detection**: Compares current commit to last processed via the
   GitHub `compare` API
3. **Selective Processing**: Only regenerates code maps for changed projects

**Example Flow:**

```
Last processed: abc123
Current commit: def456

Changed files:
  - src/MyApp.Api/Controllers/OrdersController.cs
  - src/MyApp.Api/Services/OrderService.cs

Result: Regenerate
  - summary#csharp#project#MyApp.Api
  - summary#csharp#controllers#MyApp.Api
  - summary#csharp#services#MyApp.Api
```

## Stale Code Map Cleanup

When files are deleted, stale code maps are removed:

| Deleted | Deleted Code Map |
|---------|------------------|
| `src/<Project>/Controllers/*.cs` (all) | `summary#csharp#controllers#<Project>` |
| `src/<Project>/<Project>.csproj` | `summary#csharp#project#<Project>` and all sub-units |
| `tests/<Project>.Tests/...` (all) | `summary#tests#unit` (regenerated) |

## Parser Strategy

The C# backend uses regex and filesystem heuristics only - **no AST parsing**.

- Discovery uses filenames and directory names only. No `.cs` content parsing
  for boundary decisions.
- Test framework detection uses regex over `.csproj` text. The patterns we
  match (`<PackageReference Include="xunit" ...`) are sub-line and
  unambiguous - no XML parser warranted.
- Class/method counts (when added later) will be regex-approximated.
- See ADR-017 for the full rationale.

## Example Workflow

### Initial Setup

```bash
# Generate code maps for all repos
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps

# Generate for single repo
ENVIRONMENT=prd ./scripts/outcome-ops-assist generate-code-maps my-csharp-app
```

### Trigger Code Generation

Add the `approved-for-generation` label to a GitHub issue:

```markdown
## User Story

As a developer, I want to add a new REST endpoint for managing customer
preferences.

## Requirements

- Create `PreferencesController` in `MyApp.Api/Controllers`
- Create `PreferencesService` with CRUD operations
- Create `Preference` entity with EF Core configuration
- Include xUnit unit tests using FluentAssertions and Moq
```

The system will:

1. Query the knowledge base for ASP.NET Core / EF Core patterns
2. Find repo-specific code maps for existing controllers and services
3. Generate code following the established project structure
4. Create xUnit tests in the matching test project

## Limitations

- `.csproj` XML is not parsed - boundaries are filename-driven only
- Class / method counts (when added later) are regex-approximated
- Razor / Blazor pages don't get their own unit type yet
- F# files coexisting with C# in a polyglot solution are skipped
- Combined unit + integration + e2e tests in a single project (anti-pattern
  but seen in legacy code) all map to `unit`

## Related Documentation

- [Lambda: Generate Code](lambda-generate-code.md) - Main code generation
  documentation
- [Lambda: Generate Code Maps](lambda-generate-code-maps.md) - Code map
  generation
- [ADR-009: Code Unit Discovery Pattern](adr/ADR-009-code-unit-discovery-pattern.md)
- [ADR-010: Backend Naming Conventions](adr/ADR-010-backend-naming-conventions.md)
- [ADR-014: C# Code Unit Discovery](adr/ADR-014-csharp-code-unit-discovery.md)
- [ADR-015: C# Backend Naming](adr/ADR-015-csharp-backend-naming.md)
- [ADR-016: C# Test Discovery](adr/ADR-016-csharp-test-discovery.md)
- [ADR-017: C# Parser Strategy](adr/ADR-017-csharp-parser-strategy.md)

<!-- Confluence sync -->
