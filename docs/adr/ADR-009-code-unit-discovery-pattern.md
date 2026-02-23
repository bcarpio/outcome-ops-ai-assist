# ADR-009: Code Unit Discovery Pattern for Code Map Generation

## Status: Accepted

## Context

OutcomeOps generates code maps by analyzing repository structures and creating summaries for logical "code units" - cohesive groups of files that represent a single concern (a service, module, component, etc.).

The challenge is determining **where to draw boundaries** between code units across different languages and architectures:

- **Python Lambda**: `lambda/function-name/` directories are obvious boundaries
- **Java**: Package depth varies - `com/company/service/` vs `com/company/common/core/model/application/`
- **Node.js**: Mix of `src/handlers/`, flat structures, or monorepo patterns
- **Go**: `cmd/`, `pkg/`, `internal/` conventions

We need a generic algorithm that works across languages without hardcoding specific package names or company conventions.

## Decision

### Code Unit Discovery Algorithm

A directory becomes a **code unit** when it meets these criteria:

1. **Contains source files directly** - The directory has source files (`.java`, `.py`, `.ts`, etc.), not just subdirectories
2. **Meets minimum file threshold** - Contains 2+ source files, OR
3. **Matches known boundary patterns** - Directory name matches known suffixes (`service`, `model`, `controller`, `handler`, `api`, `client`, etc.), OR
4. **Is a build module boundary** - Contains build files (`pom.xml`, `build.gradle`, `package.json`, `pyproject.toml`)

### Traversal Strategy

```
1. Start at source root (e.g., src/main/java/, lambda/, src/)
2. For each directory:
   a. Count source files directly in this directory
   b. Check if directory name matches boundary patterns
   c. Check for build module markers
   d. If criteria met -> create CodeUnit, stop descending
   e. If not met -> continue descending into subdirectories
3. Directories with only namespace containers (0-1 files) are traversed through
```

### Boundary Pattern Suffixes

These directory name patterns indicate a logical code unit boundary:

**Service/Business Logic:**
- `service`, `services`
- `handler`, `handlers`
- `controller`, `controllers`
- `api`, `apis`
- `client`, `clients`
- `worker`, `workers`
- `processor`, `processors`
- `listener`, `listeners`

**Data/Models:**
- `model`, `models`
- `entity`, `entities`
- `dto`, `dtos`
- `schema`, `schemas`
- `domain`

**Infrastructure:**
- `repository`, `repositories`
- `dao`, `daos`
- `adapter`, `adapters`
- `gateway`, `gateways`

**Utilities:**
- `util`, `utils`, `utility`, `utilities`
- `helper`, `helpers`
- `common`
- `shared`
- `core`
- `lib`

**Configuration:**
- `config`, `configuration`
- `security`
- `auth`, `authentication`

**Implementation Patterns (common in Java):**
- `impl` - implementation classes
- `internal` - internal/private APIs
- `legacy` - legacy code modules

### Language-Specific Source Extensions

| Language | Source Extensions |
|----------|------------------|
| Python | `.py` |
| Java | `.java` |
| Kotlin | `.kt`, `.kts` |
| JavaScript | `.js`, `.mjs`, `.cjs` |
| TypeScript | `.ts`, `.tsx` |
| Go | `.go` |
| Ruby | `.rb` |
| C# | `.cs` |
| Rust | `.rs` |

### Example: Java Package Discovery

Given this structure:
```
src/main/java/com/apriori/common/
├── applicationkernel/
│   ├── Application.java
│   ├── Kernel.java
│   └── Config.java
├── core/
│   └── model/
│       └── application/
│           ├── AppModel.java
│           └── AppState.java
├── notification/
│   └── email/
│       ├── EmailService.java
│       ├── EmailTemplate.java
│       └── SmtpClient.java
└── restapiclient/
    ├── RestClient.java
    └── HttpConfig.java
```

Discovery results:
- `applicationkernel/` -> CodeUnit (3 files >= 2 threshold)
- `core/` -> traverse (0 files)
- `core/model/` -> traverse (0 files)
- `core/model/application/` -> CodeUnit (2 files >= 2 threshold)
- `notification/` -> traverse (0 files)
- `notification/email/` -> CodeUnit (3 files >= 2 threshold)
- `restapiclient/` -> CodeUnit (2 files >= 2 threshold)

### Build Module Detection

Build module markers that create automatic code unit boundaries:

| Build System | Marker Files |
|--------------|-------------|
| Maven | `pom.xml` |
| Gradle | `build.gradle`, `build.gradle.kts`, `settings.gradle`, `settings.gradle.kts`, `gradle.properties` |
| Node.js | `package.json` (with `main` or `exports`) |
| Python | `pyproject.toml`, `setup.py` |
| Go | `go.mod` |
| Rust | `Cargo.toml` |
| .NET | `*.csproj`, `*.fsproj` |

### Excluded Paths

These paths are always excluded from source code unit discovery (tests are discovered separately):

- Build outputs: `target/`, `build/`, `dist/`, `out/`, `bin/`
- Dependencies: `node_modules/`, `vendor/`, `.venv/`, `venv/`
- IDE/tooling: `.git/`, `.idea/`, `.vscode/`, `__pycache__/`
- Test directories: `src/test/java/`, `src/test/kotlin/`, `src/test/resources/`, `src/integrationTest/`, `src/e2e/`
- Test resources: `test-resources/`, `testdata/`
- Generated code: `generated/`, `gen/`

### Namespace Container Handling

For deep Java package hierarchies like `com/company/project/service/impl/deep/nested`, directories are treated as namespace containers (traversed through, not made into code units) when:

1. They have 0 source files directly in them
2. Their depth relative to source root is > 4
3. All path parts are simple identifiers (alphanumeric)

This prevents creating empty code units for namespace-only directories while still discovering the actual code at the leaf level.

### Special Unit Types

Beyond source code units, backends should also discover:

1. **Infrastructure** - `.tf`, `.yaml` (k8s), `Dockerfile`
2. **Tests** - Grouped by type (unit, integration, e2e)
3. **Documentation** - `.md` files
4. **Configuration** - Application configs, CI/CD

## Consequences

### Positive

- Works across Java, Python, Node.js, Go, Ruby, .NET without hardcoding
- Respects natural code boundaries (packages, modules)
- Handles variable-depth package structures automatically
- Build modules are always respected as boundaries
- Easy to extend with new languages or patterns

### Tradeoffs

- May create too many small code units in deeply nested structures (mitigated by file threshold)
- May miss boundaries in unconventional project structures
- Suffix matching is heuristic - edge cases may need configuration overrides

## Implementation

### Starting today

1. Extract generic `_discover_source_units()` method to `base.py`
2. Refactor `lambda_backend.py` to use generic discovery
3. Create `java_backend.py` using the generic algorithm
4. Add language configuration (extensions, source roots)

### Configuration Options

Backends should support these config options:

```python
config = {
    # Source roots to scan (language-specific defaults)
    "source_roots": ["src/main/java", "src"],

    # File extensions for this language
    "source_extensions": [".java", ".kt"],

    # Minimum files to form a code unit
    "min_files_threshold": 2,

    # Additional boundary patterns (beyond defaults)
    "custom_boundary_patterns": ["kernel", "facade"],

    # Paths to exclude
    "exclude_patterns": ["generated/", "target/"],
}
```

### Next phases

1. Add Kubernetes backend with manifest-based discovery
2. Add monorepo support (multiple source roots)
3. Add `.outcomeops.yaml` for per-repo configuration overrides
4. Consider ML-based boundary detection for complex cases

## Related ADRs

- ADR-004: Terraform Workflow (infrastructure code maps)

## References

- `lambda/generate-code-maps/backends/base.py` - Backend abstraction
- `lambda/generate-code-maps/backends/lambda_backend.py` - Current Python implementation
- `lambda/generate-code-maps/backends/java_backend.py` - Java implementation (this ADR)

Version History:

- v1.0 (2025-01-26): Initial decision

<!-- Confluence sync -->
