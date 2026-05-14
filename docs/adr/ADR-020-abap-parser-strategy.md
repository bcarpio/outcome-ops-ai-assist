# ADR-020: ABAP Parser Strategy for the Symbol-Graph Extractor

## Status: Accepted

## Context

The symbol-graph extractor introduced in plan-gkg chunk 01 produces
typed structural facts (`defines`, `imports`, `calls`, `implements`,
`extends`) for every language the platform supports. For Python,
TypeScript, Java, and Go the choice was straightforward: Tree-sitter
grammars on PyPI cover the languages cleanly. For C# the precedent
was already set by ADR-017 — regex-only, no parser library.

ABAP was the open question. The extractor needs to read two very
different shapes of ABAP source that the same customer often ships
side-by-side in one repo:

| Shape | Markers | Examples |
|-------|---------|----------|
| **Object-Oriented** | `CLASS ... DEFINITION`, `CLASS ... IMPLEMENTATION`, `METHOD` | Modern Z-class implementations, ABAP Cloud code, BAdI implementations |
| **Procedural** | `REPORT`, `FUNCTION-POOL`, `START-OF-SELECTION`, `FORM`, event blocks | Classic Z-reports, function-group includes, customer-form routines, dialog-program top includes |

A single OO grammar that errors on procedural ABAP is not acceptable —
the extractor would silently drop facts from the older code that
typically dominates a regulated customer's actual surface area.

### Spike (2026-05-14)

We evaluated the available ABAP grammars:

1. **Upstream `tree-sitter-abap`** — bit-rotted; multiple class
   constructs error.
2. **FrankRogalski/tree-sitter-abap** (fork, HEAD `7dcb098a358a00701fcb9de964593898515efb8a`)
   — parses OO ABAP cleanly. **Errors on procedural ABAP** (FORM /
   FUNCTION-POOL / event blocks). Footprint 3.7 MB total (runtime +
   grammar), MIT-licensed via `pyproject.toml`.
3. **Pure regex** — handles every shape uniformly but lacks the
   namespace correctness Tree-sitter gives on OO code.
4. **Roslyn-style proprietary parser** — not viable; no equivalent
   exists for ABAP and a customer-deployed Lambda cannot depend on
   SAP Cloud parsing services.

## Decision

**Use a hybrid Tree-sitter + regex extractor for ABAP.** The dispatcher
sniffs the file header and routes per file:

1. Files containing `CLASS ... DEFINITION` or
   `CLASS ... IMPLEMENTATION` → **Tree-sitter backend** via the
   FrankRogalski fork pinned to commit `7dcb098`.
2. Files containing `FUNCTION-POOL`, `START-OF-SELECTION`, `REPORT`,
   `FORM`, or other event-block markers → **regex backend** following
   the ADR-017 C# pattern.
3. Files matching neither → regex backend (safe default).

The dispatcher lives in `lambda/process-batch-summary/extractor/abap_extractor.py`,
with the two backends in private helpers (`_extract_via_tree_sitter`,
`_extract_via_regex`) so each is testable in isolation.

### Dependency pin

```
tree-sitter-abap @ git+https://github.com/FrankRogalski/tree-sitter-abap.git@7dcb098
```

The pin is intentional, not floating. If Frank Rogalski force-pushes
or rewrites history, the build fails loudly rather than silently
upgrading; the v1 contract documented here assumes the grammar shape
at SHA `7dcb098`.

### Alternatives Considered

| Option | Pros | Cons | Verdict |
|--------|------|------|---------|
| FrankRogalski tree-sitter only | Best fidelity on OO ABAP | Errors on procedural ABAP — silent fact loss | Rejected |
| Regex only | One mental model, parity with ADR-017 (C#) | Loses namespace correctness on OO methods | Rejected (rich OO call-graph queries the highest-leverage v2 unlock) |
| Hybrid (this ADR) | OO accuracy + procedural coverage | Two code paths to maintain | **Accepted** |
| Author a new grammar | Long-term clean fix | Multi-quarter effort; no community to inherit it | Rejected for v1 |

## Consequences

### Positive

- OO ABAP gets namespace-correct facts (`abap:Z_CL_BOOKING::CONFIRM`)
  that the symbol-graph query layer can index by class.
- Procedural ABAP still emits facts — no silent gap on regulated-
  customer Z-code that has not been migrated to classes.
- The wire contract (the `Fact` dataclass) is identical across both
  backends, so chunks 02–04 do not need to know which path produced
  the facts.

### Tradeoffs

- The Tree-sitter grammar is a community fork pinned to a specific SHA.
  If the fork goes dormant for an extended period, we accept the same
  risk every grammar dependency carries.
- The regex backend will mis-call edge cases (string literals
  containing keywords, nested method names embedded in identifiers).
  These match the documented C# regex trade-offs in ADR-017.

### Dormancy contingency

If the FrankRogalski fork becomes unmaintained or develops a defect
we cannot patch, the contingency is **fall back to pure regex on every
file** — the regex backend handles both shapes and the namespace-
correctness regression is the same gap the C# backend already lives
with under ADR-017. The dispatcher in `abap_extractor.py` already does
this fallback at runtime when the Tree-sitter grammar fails to import.

## Implementation

1. `lambda/process-batch-summary/extractor/abap_extractor.py` implements
   `extract()`, `_extract_via_tree_sitter()`, and
   `_extract_via_regex()`.
2. `lambda/process-batch-summary/requirements.txt` pins the
   FrankRogalski fork at SHA `7dcb098`.
3. The Tree-sitter backend is imported lazily and falls back to the
   regex backend if the grammar wheel is unavailable in the build
   environment — keeps the unit-test path deps-light.
4. Test fixtures live in `lambda/tests/fixtures/extractor/abap/` and
   include one OO file and one procedural file so both backends are
   exercised by `make test-unit`.

If a future contributor proposes lifting OO-ABAP fact emission to
parity with the Java/Python backends (full method-body call-graph
walks), this ADR must be revisited and the dependency contract on
the FrankRogalski grammar must be re-validated.

## Related ADRs

- ADR-007: Documentation-Driven Decisions (Pattern definitions live in
  docs, not in code — informs why this ADR exists)
- ADR-009: Code Unit Discovery Pattern (the exclude-path list the
  extractor reuses)
- ADR-014: C# Code Unit Discovery (regex precedent for backend code)
- ADR-017: C# Parser Strategy (the structural template this ADR
  mirrors)

## References

- FrankRogalski/tree-sitter-abap: https://github.com/FrankRogalski/tree-sitter-abap
- Upstream tree-sitter-abap (bit-rotted reference):
  https://github.com/sap-data-services/tree-sitter-abap
- plan-gkg/01-symbol-extraction.md (the spec this ADR backs)

Version History:

- v1.0 (2026-05-14): Initial decision — hybrid Tree-sitter + regex,
  FrankRogalski fork pinned to commit `7dcb098`
