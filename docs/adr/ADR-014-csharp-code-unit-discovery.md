# ADR-014: C# Code Unit Discovery for Code Map Generation

## Status: Accepted

## Context

ADR-009 defines a generic code-unit discovery algorithm based on directory boundaries, build-module markers, and minimum file thresholds. Each language backend in `lambda/process-repo-summary/backends/` extends that algorithm with language-specific source extensions, source roots, and boundary patterns.

Adding C#/.NET support requires concrete answers for what counts as a code unit in a typical Fortune 500 .NET repository. F500 .NET repos are heterogeneous: a single repo may mix .NET Framework 4.x services, .NET 6/7/8 microservices, ASP.NET MVC web apps, ASP.NET Core Web APIs, ASP.NET Web Forms legacy modules, and WCF service hosts. The discovery pattern has to handle all of these without bespoke per-customer configuration.

This ADR is **repo-local** to the OutcomeOps platform. It will be lifted to a global ADR once stable so that customer repos do not have to redefine these conventions.

## Decision

### Source Extensions

Treat the following as C# source files for discovery purposes:

| Extension | Purpose |
|-----------|---------|
| `.cs` | Primary C# source (classes, interfaces, records, enums) |
| `.cshtml` | Razor views (ASP.NET MVC, ASP.NET Core MVC) |
| `.aspx` | ASP.NET Web Forms pages |
| `.aspx.cs` | Web Forms code-behind |
| `.ascx` | Web Forms user controls |
| `.ashx` | Generic HTTP handlers |
| `.svc` | WCF service host markers |

`.razor` (Blazor), `.fs` (F#), `.xaml` (WPF/UWP) are **out of scope** for v1 because they represent <20% of F500 .NET repository surface area.

### Source Roots (Default)

Default source roots, in priority order:

```
["src", "source", "server", "web", "Source", "Src"]
```

Plus the repository root itself, because many older .NET Framework repos place projects directly at the top level (e.g., `/MyApp.Web/`, `/MyApp.Services/`).

Override via `.outcomeops.yaml` `code_generation.source_roots`.

### Build-Module Markers

A directory becomes a code unit automatically when it contains any of:

| Marker | Indicates |
|--------|-----------|
| `*.csproj` | C# project (SDK-style or old-style) |
| `*.sln` | Solution root |
| `Directory.Packages.props` | Central package management root |
| `Directory.Build.props` | Shared MSBuild props root |
| `packages.config` | Legacy NuGet project root |
| `global.json` | .NET SDK version pin (solution-level) |

These are added to `BUILD_MODULE_MARKERS` in `process-repo-summary/backends/base.py` (or matched per-backend if we keep base.py language-neutral — see Implementation).

### Boundary Pattern Suffixes

In addition to `DEFAULT_BOUNDARY_PATTERNS` from `base.py`, the C# backend recognizes these directory names as natural boundaries:

```
controllers, viewmodels, views, pages, areas,
infrastructure, persistence, repositories,
domain, application, presentation,
hubs, middleware, filters, attributes,
extensions, abstractions, contracts,
migrations, seeders,
```

These map to MVC, Clean Architecture, DDD, and SignalR conventions used widely across F500 codebases.

### Excluded Paths

Add to `DEFAULT_EXCLUDED_PATTERNS`:

```
bin/, obj/, packages/, .vs/,
TestResults/, coverage/, artifacts/,
Properties/, _ReSharper.Caches/,
*.user, *.suo,
node_modules/  (already excluded — ASP.NET Core projects often colocate frontend)
```

`Properties/` is excluded because it contains `AssemblyInfo.cs` and `launchSettings.json`, neither of which represent code units.

### Code Unit Types Produced

The C# backend produces these `CodeUnit` types:

| `unit_type` | Source | Storage key prefix |
|-------------|--------|--------------------|
| `csharp-project` | One per `.csproj` | `summary#csharp#project#<name>` |
| `csharp-controllers` | `Controllers/` per project | `summary#csharp#controllers#<project>` |
| `csharp-services` | `Services/` per project | `summary#csharp#services#<project>` |
| `csharp-views` | `Views/`, `Pages/`, `Areas/` per project | `summary#csharp#views#<project>` |
| `csharp-models` | `Models/`, `Entities/`, `DTOs/` per project | `summary#csharp#models#<project>` |
| `tests` | Test projects (per ADR-016) | `summary#tests#<group>` |
| `infrastructure` | Existing — unchanged | `summary#infrastructure` |
| `docs` | Existing — unchanged | `summary#docs` |
| `kubernetes` | Existing — unchanged | `summary#kubernetes` |

Project-level units **dominate**: when a `.csproj` is present in a directory, the entire project tree (excluding subprojects) becomes one or more `csharp-*` units rooted at that project. Subdirectory units (`Controllers/`, etc.) are scoped to their parent project so multi-project solutions don't collapse into a single namespace.

### Multi-Project Solutions

A typical F500 .NET solution has 5-50 projects:

```
MySolution.sln
src/
  MyApp.Web/MyApp.Web.csproj
  MyApp.Api/MyApp.Api.csproj
  MyApp.Domain/MyApp.Domain.csproj
  MyApp.Infrastructure/MyApp.Infrastructure.csproj
tests/
  MyApp.Web.Tests/MyApp.Web.Tests.csproj
  MyApp.Domain.Tests/MyApp.Domain.Tests.csproj
```

Each `.csproj` becomes a code unit boundary. Test projects are routed through the test-discovery path (ADR-016), not the project path.

### Old-Style vs SDK-Style csproj

Both formats are detected the same way (filename match). The backend MUST NOT attempt to parse XML schema differences — that level of detail belongs to deeper analysis layers, not unit discovery.

### Namespace Containers

Deep `Company.Product.Module.Submodule.Internal` directory hierarchies produced by mirroring C# namespaces into folders are handled by the existing `is_namespace_container` logic in `base.py` (depth > 4 with no source files → traverse through). No C#-specific change needed.

## Consequences

### Positive

- F500 .NET coverage from day one: legacy Framework + modern Core, MVC + Web API + Web Forms + WCF
- One code unit per `.csproj` matches how .NET developers reason about boundaries
- Reuses existing ADR-009 algorithm — no duplication of traversal logic
- Test projects are a separate concern (ADR-016) — clean separation

### Tradeoffs

- Razor/Blazor/WPF deferred — customers using those will need follow-up discovery rules
- We do not parse `.csproj` XML; if a customer has unusual project layouts (e.g., shared `Compile Include=`), boundaries may be wider than intended. Acceptable for v1.
- F# excluded means polyglot .NET repos (rare in F500) get partial coverage

## Implementation

See `lambda/process-repo-summary/backends/csharp_backend.py` (chunk-1).

Markers and boundary patterns are added in the C# backend file via `get_excluded_patterns()` and `get_boundary_patterns()` overrides — `base.py` is **not** modified, keeping it language-neutral. Build module markers specific to C# (`.csproj`, `.sln`, etc.) are checked in a backend-local override of `has_build_module_marker()`.

## Related ADRs

- ADR-009: Code Unit Discovery Pattern (parent algorithm)
- ADR-010: Backend Naming Conventions (drives `csharp` as the registered name)
- ADR-015: C# Backend Naming and Registration (this repo)
- ADR-016: C# Test Discovery (this repo)
- ADR-017: C# Parser Strategy (this repo)

## References

- abapGit equivalent: `lambda/process-repo-summary/backends/abap_backend.py` (closest analogue — file-extension-driven discovery)
- Microsoft docs on `.csproj` formats: https://learn.microsoft.com/dotnet/core/project-sdk/overview

Version History:

- v1.0 (2026-04-30): Initial decision — F500-realistic .NET coverage, regex/filesystem only

<!-- Confluence sync -->
