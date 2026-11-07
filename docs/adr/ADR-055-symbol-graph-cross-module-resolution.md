# ADR-055: Symbol-Graph Cross-Module Reference Resolution

## Status: Proposed

## Context

ADR-054 Plane 1 (the code-symbol contract) detects a breaking change by asking
the symbol graph a precise question: *who calls or imports the symbol this PR
changed?* It relies on `shared.symbol_graph_reader.callers(definition_id)` and
`importers(definition_id)` returning the real consumers across files and repos.

Today that question returns **nothing across files**, because the extractors
never bind a cross-module reference to the callee's *definition*. A `defines`
fact is keyed off the defining file's module/namespace, but a `calls`/`imports`
fact is keyed off the **reference as written in the caller's own namespace**.
The two forms never line up, so a reference in file A cannot be found from a
definition in file B. Only intra-file references resolve (caller and callee
share a module), and Plane 1 deliberately excludes same-file references as
self-references — so it sees nothing.

**This is language-general, not Python-specific.** Every extractor builds the
`calls` target the same way — `make_symbol_id(<lang>, <caller module/package>,
callee)`:

| Language | Call-target construction | Reference site |
|---|---|---|
| Python | `make_symbol_id("python", <caller module>, callee)` | `python_extractor.py:312,371` |
| Go | `make_symbol_id("go", <caller module>, callee)` | `go_extractor.py:180` |
| Java | `make_symbol_id("java", <caller package>, callee)` | `java_extractor.py:244` |
| C# | `make_symbol_id("csharp", <caller module>, callee)` | `csharp_extractor.py:340` |
| TypeScript | `make_symbol_id("typescript", <caller module>, callee)` | `typescript_extractor.py:307,342,387` |
| ABAP | class-qualifies where it can; caller module for unqualified refs | `abap_extractor.py:249-254` |

The defect was **verified live for Python** against the deployed graph
(`prd-outcome-ops-ai-assist-symbol-graph`): ~47k Python `calls` edges for this
repo, yet the only edges targeting
`python:lambda.shared.pr_metadata::fetch_pr_metadata` are intra-file calls from
`pr_metadata.py` itself — every real consumer is recorded under its own caller
namespace, unlinked. The other extractors emit targets by the identical
mechanism, so the same gap exists wherever those languages are ingested.

The resolution **framework** is language-agnostic: name binding is "build a
definitions index for the repo, build a per-file import/scope map, rewrite each
reference's target to the definition it binds to, and never guess." What differs
per language is only the **binding rules** — how imports/usings/packages work and
how a module or namespace maps to the defining file. That split mirrors how
extraction is already organized: one common `extractor.base` plus per-language
extractors registered via `register_language`.

This is the same class of defect ADR-046 named ("extractor completeness … a
separate, parallel fix"; the decorated-definition skip was one instance). Like
that fix, correcting it is ingest-time and requires a one-time full re-extract
(ADR-047) to take effect on an existing graph. Without it, BREAKING_CHANGES
cannot detect code-coupling breakage in any language — it silently reports
"safe" for a change to a widely-imported symbol.

## Decision

Introduce a **language-agnostic reference-resolution pass** plus a **per-language
resolver contract**, and implement the **Python resolver first** as the reference
implementation. Resolution rewrites a `calls`/`imports`/`extends` edge's
`target_symbol_id` to the callee's *definition* id when it binds unambiguously
within the repo.

### 1. Ingest-time, language-agnostic two-phase pass

Baked into the graph at build time (not read time — see Alternatives), hosted in
the ADR-047 symbol-graph-only regeneration, which already enumerates every
tracked file:

1. **Collect**: extract raw facts for all files; build a repo **definitions
   index** from the `defines` facts — `(module/namespace, name) → definition_id`,
   plus `name → {definition_id}` for disambiguation, and the set of file
   modules/namespaces.
2. **Bind + rewrite**: for each file, the file's **per-language resolver**
   produces the binding context (its imports/usings/package/relative-path roots)
   and binds each raw reference to a definition id; the pass sets
   `target_symbol_id` to that id when the binding is unambiguous, else leaves the
   raw target unchanged.

The two-phase pass, the definitions index, the rewrite, and the never-guess rule
are all language-agnostic and live in the shared resolution layer.

### 2. Per-language resolver contract (registered beside extractors)

A resolver is registered per language (`register_resolver(lang, resolver)`,
mirroring `register_language`) and supplies only the language-specific binding
rules:

- (a) extract a file's binding context (import/using/package statements, alias
  map, relative-path or source roots);
- (b) map an import/namespace token to a repo module/file;
- (c) bind a reference (bare / dotted / qualified name) to a candidate definition
  given that context.

**A language with no registered resolver keeps today's raw behavior** — no
regression, and languages are enabled incrementally.

### 3. Resolution scope v1 = within-repo; cross-repo deferred

Resolve a reference to a definition in the same repo's file set. Cross-repo
resolution (repoB importing repoA's symbol) needs a global cross-repo
definitions index; deferred and named as a gap. (Cross-*service* runtime
coupling has no static edge at all and is ADR-054 Plane 2.) Language-agnostic.

### 4. Never guess; resolution only adds linkage

A reference that does not bind to exactly one known definition keeps its raw
target (external libraries, dynamic dispatch, ambiguous names). For a
breaking-change WARN, **over-resolution is worse than under-resolution** — a
wrong binding produces a false "you broke this consumer." When in doubt, stay
raw. Language-agnostic.

### 5. Rollout: Python first, then one resolver per language

The Python resolver ships first — it proves the contract and unblocks this repo's
Plane 1. Each additional language (TypeScript, Java, C#, Go, and an ABAP
refinement) implements the **same contract** as a follow-on **code** change,
**not a new ADR**, ordered by consumer value / ingested footprint.

### 6. One-time full re-extract after deploy

Because resolution is ingest-time, existing edges stay raw until the graph is
rebuilt. A one-time ADR-047 symbol-graph-only regeneration per repo is required
after each resolver lands — the same operational step ADR-046/047 already
mandate for extractor-completeness changes. Facts key on the **bare repo name**
in both the PK (`{workspace_id}#{repo}`) and the purge scope.

### Per-language binding notes (informative — each resolver's real work)

- **Python**: module ≈ file path; `from x import y` / `import x.y as z`; map a
  source-root import (`shared.*`, re-rooted by the Lambda bundler) to a file
  module by suffix match.
- **TypeScript/JavaScript**: `import {x} from './rel/path'` — **relative file
  paths** plus `tsconfig` path aliases; module ≈ file; the hardest mapping.
- **Java**: `import a.b.C`; package = directory, class = file; the FQN maps
  directly to a file.
- **C#**: `using Ns` — namespace is **decoupled** from file/directory; bind by
  namespace + type across files.
- **Go**: package = directory (many files, one namespace); same-package
  references need no qualifier; an import path maps to a package directory.
- **ABAP**: global classes / function modules; already partially class-qualified,
  so the resolver mostly refines unqualified references.

## Consequences

**Positive:**
- Plane 1, the MCP `find_*` tools, and chat retrieval gain correct cross-file
  linkage for **every language that has a resolver** — one framework, incremental
  language rollout, no per-language ADR churn.
- BREAKING_CHANGES can finally detect code-coupling breakage, enabling the
  Plane-1 file-scope widening to `shared/` (separate change) to find something.
- Additive to the edge model (no new DynamoDB attribute, no key change);
  reversible by regenerating from the prior extractor.

**Negative:**
- The per-language binding rules are where the complexity lives — TypeScript
  relative paths and C# namespace decoupling especially; each resolver is real
  work.
- Incremental (per-PR) resolution is best-effort and may lag until the next full
  regen.
- Suffix / relative mapping can mis-bind on ambiguous cases; mitigated by the
  never-guess rule (ambiguous → raw), trading recall for precision.

**Neutral:**
- A one-time full re-extract is required on every deployment carrying an existing
  graph, exactly as ADR-046/047 already require.
- Cross-repo resolution and any language without a resolver yet remain named
  gaps — those references keep raw behavior until addressed.

## Alternatives Considered

- **Python-only fix.** The original draft of this ADR. Rejected: the defect is
  identical across all extractors, so a Python-only decision would force ~5
  near-duplicate ADRs and re-litigate the same framework per language. A general
  contract with per-language resolvers decides the shared parts once.
- **Read-time resolution in `symbol_graph_reader`.** Rejected: high blast radius
  (the reader is shared by MCP, chat, and the PR check) and re-resolves on every
  query.
- **Fuzzy name-only matching (callers-by-bare-name).** Rejected: name collisions
  across modules produce false breaking-change WARNs; over-reporting is the wrong
  failure for a governance signal (§4).
- **Full per-language semantic resolvers (jedi / tsc / Roslyn / javac).**
  Rejected for v1: heavy, environment-specific, and fragile in a bundled monorepo.
  The import-context + mapping heuristic is cheap and sufficient; a real resolver
  can supersede a given language's heuristic later if it proves inadequate.

## Examples

Before (today) — a Python call in `process-pr-check/handler.py`:

```
kind:   calls
source: python:lambda.process-pr-check.handler::process_check_job
target: python:lambda.process-pr-check.handler::fetch_pr_metadata   # caller namespace — unresolvable
```

`callers("python:lambda.shared.pr_metadata::fetch_pr_metadata")` → `[]`.

After resolution rewrites the target in phase 2:

```
kind:   calls
source: python:lambda.process-pr-check.handler::process_check_job
target: python:lambda.shared.pr_metadata::fetch_pr_metadata         # the definition
```

`callers("python:lambda.shared.pr_metadata::fetch_pr_metadata")` →
`[process-pr-check/handler.py, analyze-pr/handler.py, …]`, each a real consumer
Plane 1 can name and anchor (ADR-031). The same before/after shape applies in
every language once its resolver lands; an unresolved reference (external library,
dynamic dispatch, or ambiguous name) keeps its raw target — never guessed.
