# ADR-056: Cross-Repo Symbol Resolution

## Status: Accepted

v1 (consumer-pull) and v2 (producer-push) are both implemented and live-verified
(2026-11-08). See §5 for the two mechanisms and the v2 operational contract.

## Context

ADR-055 introduced cross-MODULE reference resolution within a single repo: a
`calls`/`imports`/`extends` edge's `target_symbol_id` is rebound to the callee's
*definition* id when it binds unambiguously **within that repo**. ADR-055 §3
explicitly deferred the cross-REPO case — a consumer repo that imports a producer
repo's symbol — and named it a gap.

That gap is what a real platform needs for the highest-value question: *if a
shared-library repo changes a symbol, which downstream consumer repos break?*
ADR-054 Plane 1 answers "who calls/imports the changed symbol" from the symbol
graph, and the graph spans every ingested repo — so the capability is in reach,
but only if cross-repo references actually bind to the producer's definition.

**Live baseline (verified 2026-11-08).** Two synthetic repos in one workspace:
`shared-lib` (producer — `shared_lib.math_utils.{add,multiply}`,
`shared_lib.string_utils.{shout,whisper}`) and `consumer-app` (consumer —
`app/main.py` and `app/report.py` do `from shared_lib.math_utils import add` and
call `add(...)`). After ingest:

| Query on `python:shared_lib.math_utils::add` | Result |
|---|---|
| `find_importers` | **2 cross-repo sites** in `consumer-app` — already works |
| `find_callers`   | **empty** — cross-repo calls do not resolve |

The asymmetry is the crux and it is not an accident:

- **Importers already cross repos, for free.** An `import` fact records the
  symbol *as imported* — `from shared_lib.math_utils import add` yields a target
  `python:shared_lib.math_utils::add`, which is **byte-identical to the producer's
  `defines` target id**. The GSI indexes edges by `target_symbol_id` across every
  repo in the workspace, so `importers(producer_def)` returns the consumer with no
  new code.
- **Calls do not.** A `calls` fact records the callee *in the caller's own
  namespace* — `app.main::add` — never bound to the producer's definition. The
  ADR-055 within-repo resolver cannot fix this because it binds against the
  **consumer's** definitions index, which does not contain `shared_lib.math_utils`.

So the work is narrow and well-defined: bind cross-repo `calls`/`extends`/
`implements` targets (imports are already covered), and the mechanism is already
present in the ADR-055 framework — the consumer's own import fact already tells
the resolver `imported["add"] = "shared_lib.math_utils"`; it only fails because
`resolve_module_suffix` runs against a per-repo definitions index.

## Decision

Resolve cross-repo references at ingest time by resolving each repo against a
**workspace-wide definitions index** — the union of every repo's `defines` within
the same workspace — reusing the ADR-055 per-language resolvers unchanged.

### 1. The workspace is the cross-repo boundary

A **workspace** (a team's set of related repos) is the unit of cross-repo
resolution. A repo's references resolve against definitions in its own repo **plus
every other repo in the same workspace**. Cross-*workspace* resolution is out of
scope: a repo in workspace A never binds to a definition that exists only in
workspace B. (Globally-shared library repos are addressed in §6.)

### 2. Importers are already cross-repo; the work is calls/extends/implements

No change is needed for cross-repo *import* detection — it falls out of the
matching canonical + the target GSI (see Context). The new work binds cross-repo
`calls`, `extends`, and `implements` targets, whose ids live in the caller's
namespace and must be rewritten to the producer's definition id.

### 3. Workspace-wide definitions index (language-agnostic)

The ADR-055 resolution pass builds a `DefinitionsIndex` from the repo's own
`defines` facts and asks each file's per-language resolver to rebind references
against it. Cross-repo resolution extends **only the index**, not the resolvers:

1. When resolving repo R, build R's own definitions index as today.
2. **Merge in the `defines` of every other repo in R's workspace(s)**, read from
   their stored symbol graphs (the DDB symbol-graph table). The merged index is
   workspace-wide.
3. Run the registered per-language resolver for each file against the merged
   index. A consumer's `app.main::add`, via its own `from shared_lib.math_utils
   import add` fact, now resolves `shared_lib.math_utils` against the producer's
   module in the merged index and binds to `python:shared_lib.math_utils::add`.

This is language-agnostic: it lives in the shared resolution layer, and every
existing per-language resolver (Python, TypeScript, Java, C#, Go) benefits without
modification.

### 4. Never guess — across repos too

A reference binds cross-repo only when it matches **exactly one** definition
across the workspace. If the same (module, name) is defined in two producer repos,
or a bare name is non-unique workspace-wide, the reference stays **raw**.
Over-resolution is worse than under-resolution: a wrong cross-repo binding
produces a false "you broke this consumer repo" WARN. Ambiguity → raw.

### 5. Ingest ordering and re-resolution (the operational contract)

Cross-repo bindings are computed at a repo's resolution time against the
**producer's definitions as they exist then**. Two consequences must be handled:

- **Ordering.** A consumer ingested before its producer cannot bind — the
  producer's defines are not in the index yet. Binding happens on the next
  resolution of the consumer.
- **Staleness on producer change.** When a producer repo changes (its defines
  move/rename/disappear), its consumers' cross-repo bindings become stale until
  the consumers are re-resolved.

The contract: **a producer change triggers re-resolution of its workspace
consumers.** Two mechanisms, in rollout order:

- **v1 (consumer-pull, ordering-managed):** resolve a repo against the current
  workspace index at its own ingest. Correct whenever the producer is already
  ingested. Operationally, re-ingesting the consumer after the producer picks up
  the binding. Sufficient to ship and verify the capability.
- **v2 (producer-push re-resolution):** when a producer completes a full-regen,
  enqueue a cross-repo re-resolution of the repos that import it (the importer→repo
  set is already queryable via the target GSI — see §2). This closes the staleness
  window without re-ingesting every consumer. Higher operational complexity;
  separate rollout.

  **v2 mechanism (as built).** After the producer's own resolution post-pass
  fires at its last-close, the producer enqueues a single `xrepo_reresolve`
  message onto the code-maps queue (the same queue the ingest Lambda already
  consumes — no new queue, no new IAM beyond the existing `SendMessage` grant).
  Consuming that message, the Lambda:

  1. **Narrows to importers via the GSI.** It reads the producer's own
     `defines`, then queries `target-kind-index` (`imports#`) for each — the same
     index that powers cross-repo `find_importers` — to get exactly the repos
     that import this producer. A producer nobody imports does ~zero work.
  2. **Re-runs the v1 post-pass per importer consumer**, unchanged, against the
     now-current workspace-wide index. This reuse is exact, not a coincidence:

     - A consumer ingested **before** its producer has *raw* cross-repo calls
       (`app.main::add`); with the producer's defines now in the index they bind
       to `shared_lib.math_utils::add`. **Closing this ordering gap is the
       primary value of v2.**
     - An **already-bound** edge re-resolves to the same id (`bound == target` →
       no rewrite): **idempotent**, so a stable workspace writes nothing.
     - An edge bound to a producer symbol the producer has since **removed** does
       not resolve, so it is **left in place, not reverted to raw**. v2
       deliberately does **not** un-bind: that dangling edge *is* the breaking
       signal surfaced on the producer's own PR (BREAKING_CHANGES still finds the
       consumer as a caller of the now-deleted id), and it self-heals on the
       consumer's next regen (a fresh raw extraction). Un-binding would hide the
       break.

  Re-resolution is triggered only from a producer's own full-regen last-close; a
  consumer re-resolution does not itself enqueue, so there is no cascade.
  Mutual imports (A↔B) are safe: regenerating A re-resolves B and stops.
  Cost note: each importer consumer re-scans the workspace's sibling defines (the
  v1 post-pass does this internally); for large importer sets this is the
  dominant cost and a precomputed shared index is the obvious optimization, left
  as a follow-on since v2 runs off the producer's critical path.

### 6. Globally-shared library repos (deferred decision)

A library used across many workspaces (e.g. an org-wide shared package) does not
fit the single-workspace boundary. Options — resolve `is_global` library repos
into every workspace, or require the library to be a member of each consuming
workspace — are deferred to a follow-on once the single-workspace case ships.

## Consequences

- BREAKING_CHANGES on a producer repo flags downstream **consumer repos** in the
  same workspace, for both imports (today) and calls/extends (after this ADR) —
  the cross-repo "break ServiceA → flag ServiceB" goal for code coupling. (Runtime
  coupling across a queue remains ADR-054 Plane 2.)
- Resolution cost grows with workspace size: the index now spans every repo's
  `defines`, and v2 re-resolution fans out to importers on each producer change.
  Bounded by workspace repo count × definitions; large workspaces need the index
  loaded efficiently (projected `defines` scan, not full facts).
- Ordering is observable: until v2, a consumer bound against a stale producer
  shows stale cross-repo callers until re-resolved. Documented, not silent.
- Never-guess caps the blast radius: ambiguous cross-repo names stay raw, so the
  failure mode is a missed consumer (under-report), never a false one.

## Alternatives Considered

- **Query-time cross-repo resolution** (resolve on `find_callers`, don't bake
  edges): rejected. BREAKING_CHANGES reads consumers via the target GSI, which
  needs the edges to exist; and query-time name-matching reintroduces the
  ambiguity never-guess is meant to prevent at write time.
- **Global (all-repos) definitions index:** rejected. The workspace is the right
  isolation boundary — teams, tenancy, and relevance. A global index would bind a
  consumer to an unrelated repo that happens to share a module name.
- **Require an explicit dependency manifest** (consumer declares producer repos):
  rejected as the primary mechanism — the import facts already name the producer
  module, so binding is inferable; a manifest may later *narrow* scope for large
  workspaces but is not needed to resolve.

## Examples

Producer (`shared-lib`): `python:shared_lib.math_utils::add` (a `defines`).

Consumer (`consumer-app`), before this ADR:
- `imports`: `python:app.main::<module>` → `python:shared_lib.math_utils::add`
  (already resolves cross-repo; `find_importers` returns it).
- `calls`: `python:app.main::compute_total` → `python:app.main::add`
  (raw, consumer namespace; `find_callers` returns nothing).

After this ADR (consumer resolved against the workspace-wide index):
- `calls`: `python:app.main::compute_total` → `python:shared_lib.math_utils::add`
  — `find_callers(python:shared_lib.math_utils::add)` returns
  `consumer-app app/main.py` and `app/report.py`, and a BREAKING_CHANGES check on a
  `shared-lib` PR that edits `add` WARNs, naming `consumer-app` as a downstream
  consumer.
