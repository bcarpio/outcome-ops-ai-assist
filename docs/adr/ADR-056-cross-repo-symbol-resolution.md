# ADR-056: Cross-Repo Symbol Resolution

## Status: Accepted

Within a single workspace, both resolution mechanisms (§5 — v1 consumer-pull and
v2 producer-push) are implemented and live-verified. The resolution scope spanning
`is_global` and granted workspaces (§1.2, §1.3, §6) is the decided design and
reuses the platform's existing cross-workspace authorization; its wiring into the
resolution and producer-push paths is the remaining build.

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
**scope-wide definitions index** — the union of the `defines` of every repo the
resolving repo is authorized to see (§1) — reusing the ADR-055 per-language
resolvers unchanged.

### 1. The resolution scope is the workspace plus what it is authorized to see

A repo's references resolve against definitions in its own repo plus every other
repo in its **resolution scope**. The scope is not a single workspace; it is the
set the platform *already* uses to decide what a repo's standards checks may read
(`shared.pr_check_scope` / `chat.resolve_chat_workspace_scope`), reused here so the
symbol graph and the checks agree on visibility:

1. **The repo's own workspace(s)** — a repo may belong to more than one.
2. **Every `is_global` workspace** — admin-created, org-wide shared repos
   (shared libraries, Terraform modules, security/encryption modules). See §6.
3. **Every workspace that has granted the repo's workspace(s) access** via the
   `search_allowed_workspaces` list on that workspace's `METADATA` row — the
   existing admin-controlled, target-consent cross-workspace grant.

Cross-workspace binding is therefore governed entirely by the customer's own
admin-set grants and `is_global` flags, **not** by the resolver and **not** by
anything a consumer repo can assert about itself. A repo in workspace A binds to a
definition in workspace B only if B is `is_global` or B has granted A. This keeps
one governance path for both retrieval and resolution, and it respects
intra-org information barriers: a team that has not shared its graph is never
read by another team's resolution.

### 2. Importers are already cross-repo; the work is calls/extends/implements

No change is needed for cross-repo *import* detection — it falls out of the
matching canonical + the target GSI (see Context). The new work binds cross-repo
`calls`, `extends`, and `implements` targets, whose ids live in the caller's
namespace and must be rewritten to the producer's definition id.

### 3. Scope-wide definitions index (language-agnostic)

The ADR-055 resolution pass builds a `DefinitionsIndex` from the repo's own
`defines` facts and asks each file's per-language resolver to rebind references
against it. Cross-repo resolution extends **only the index**, not the resolvers:

1. When resolving repo R, build R's own definitions index as today.
2. **Merge in the `defines` of every other repo in R's resolution scope** (§1),
   read from their stored symbol graphs. Because the symbol-graph partition key is
   `{workspace_id}#{repo}`, each sibling is scanned under **its own owning
   workspace's** partition, not R's — the scope enumeration yields
   `(owning_workspace, repo)` pairs, and the loaded `defines` are merged into one
   index regardless of which workspace they came from.
3. Run the registered per-language resolver for each file against the merged
   index. A consumer's `app.main::add`, via its own `from shared_lib.math_utils
   import add` fact, now resolves `shared_lib.math_utils` against the producer's
   module in the merged index and binds to `python:shared_lib.math_utils::add` —
   whether the producer sits in the consumer's own workspace, an `is_global`
   workspace, or a workspace that granted the consumer access.

This is language-agnostic: it lives in the shared resolution layer, and every
existing per-language resolver (Python, TypeScript, Java, C#, Go) benefits without
modification.

### 4. Never guess — across repos too

A reference binds cross-repo only when it matches **exactly one** definition
across the whole resolution scope (§1). If the same (module, name) is defined in
two repos in scope, or a bare name is non-unique scope-wide, the reference stays
**raw**. Widening the scope to `is_global` and granted workspaces widens the union
the uniqueness test runs over, so a name that was unique within one workspace but
collides with a global repo's symbol correctly declines to bind. Over-resolution
is worse than under-resolution: a wrong cross-repo binding produces a false "you
broke this consumer repo" WARN. Ambiguity → raw.

### 5. Ingest ordering and re-resolution (the operational contract)

Cross-repo bindings are computed at a repo's resolution time against the
**producer's definitions as they exist then**. Two consequences must be handled:

- **Ordering.** A consumer ingested before its producer cannot bind — the
  producer's defines are not in the index yet. Binding happens on the next
  resolution of the consumer.
- **Staleness on producer change.** When a producer repo changes (its defines
  move/rename/disappear), its consumers' cross-repo bindings become stale until
  the consumers are re-resolved.

The contract: **a producer change triggers re-resolution of the consumers that
can see it** — the repos whose resolution scope (§1) includes the producer. Two
mechanisms, in rollout order:

- **v1 (consumer-pull, ordering-managed):** resolve a repo against the current
  scope-wide index at its own ingest. Correct whenever the producer is already
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
     that import this producer. A producer nobody imports does ~zero work. The GSI
     partition is `{workspace_id}#{target_symbol_id}`, so when the producer is
     visible beyond its own workspace (it is `is_global`, or it has granted other
     workspaces) the narrowing queries the GSI **once per consuming workspace** —
     every workspace for an `is_global` producer, or the reverse of
     `search_allowed_workspaces` (the workspaces the producer's workspace granted)
     for a shared one. Within a single workspace this is one query, the common
     case.
  2. **Re-runs the v1 post-pass per importer consumer**, unchanged, against the
     now-current scope-wide index. This reuse is exact, not a coincidence:

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
  Cost note: each importer consumer re-scans its scope's sibling defines (the v1
  post-pass does this internally); for large importer sets this is the dominant
  cost and a precomputed shared index is the obvious optimization, left as a
  follow-on since v2 runs off the producer's critical path.

### 6. Globally-shared library repos (`is_global`)

A library used across many teams — an org-wide shared package, a set of Terraform
modules, a security/encryption/observability module every service must adopt —
belongs in an **`is_global` workspace**. Its `defines` are in every repo's
resolution scope (§1.2), so any repo that calls or imports it binds to it, and a
PR that changes it flags the consumers across the whole portfolio. This is the
mechanism behind the motivating case: if an org-wide Lambda+Datadog module
exists, a team re-creating the same integration shows up against it rather than
silently duplicating it.

`is_global` is **admin-gated by design**: only org/platform admins create global
workspaces and place repos in them, so a team cannot unilaterally make its code
authoritative for everyone. Global workspaces are expected to hold mostly docs and
standards with a small, curated set of shared modules — low volume, high reuse —
which keeps the scope-wide index cost bounded even though every repo sees them.

Two directions to be clear about, because they are not symmetric:

- **A consumer reaching a non-global producer in another team's workspace**
  requires that producer's workspace to grant the consumer's workspace
  (`search_allowed_workspaces`, §1.3). Consent lives on the side being read.
- **`is_global` is the admin asserting a repo is shared with everyone** — no
  per-consumer grant needed, because the assertion is made by an authority the
  whole org trusts, not by the consumer.

A repo does **not** declare its own cross-workspace dependencies (e.g. in
`.outcomeops.yaml`): that would let a consumer read a workspace that never
consented. Governance stays with the workspace admins and the `is_global` flag.

### 7. Language support

Cross-repo binding works for a language only when the consumer's **import id**
is byte-identical to the producer's **definition id** — i.e. the id is built from
something repo-independent (a package / namespace / module path), not a
repo-local file path. Live-verified cross-repo (synthetic producer/consumer
pairs):

- **Python** (`python:<package>::<name>`), **Java** (`java:<package>::<Class>`),
  **C#** (`csharp:<namespace>::<Type>`), **Go** (`go:<dir>::<name>`, bound by
  package directory basename): **supported** — these ids are repo-independent, so
  a consumer in one repo resolves against a producer's defines in another.
- **TypeScript**: **supported via a package-specifier basename match.** TS
  definition ids use the file path with the extension stripped
  (`typescript:src/mathx::add`), so a cross-repo import — always a *package*
  specifier (`import { add } from "ts-shared/mathx"`) — can never be byte-identical
  to the producer's definition id. The resolver instead binds a non-relative
  import's name to the UNIQUE in-scope definition whose module *basename* equals
  the specifier's last segment (`mathx`), the same directory-basename idea Go uses.
  Both the call and the import fact are rebound (the latter is what makes
  `importers(definition)` return a cross-repo TS consumer). Residual limitation: a
  bare specifier with no subpath (`from "ts-shared"`) binds only if the producer's
  entry module basename matches; general `package.json` `exports` resolution is a
  follow-on. Within-repo (relative-import) resolution is unchanged.
- **ABAP**: no resolver (intentionally deferred), so no cross-repo binding.

The resolvers' within-repo carve-outs still apply across repos (e.g. Go/C#
receiver-method calls, aliased/namespace imports stay raw) — never-guess holds.

## Consequences

- BREAKING_CHANGES on a producer repo flags downstream **consumer repos** across
  its resolution scope — the same workspace, an `is_global` library's consumers
  portfolio-wide, or a workspace it granted — for both imports and calls/extends,
  the cross-repo "break ServiceA → flag ServiceB" goal for code coupling. (Runtime
  coupling across a queue remains ADR-054 Plane 2.)
- Cross-repo visibility and the standards-check retrieval scope are governed by
  **one** mechanism (`is_global` + `search_allowed_workspaces`), so the symbol
  graph never binds across a boundary the checks would not also read across. No
  second authorization surface to keep in sync.
- Resolution cost grows with scope size: the index spans every in-scope repo's
  `defines`, and v2 re-resolution fans out to importers across the consuming
  workspaces on each producer change. Bounded by scope repo count × definitions;
  `is_global` workspaces are deliberately small and curated, and large scopes need
  the index loaded efficiently (projected `defines` scan, not full facts).
- Ordering is observable: until v2, a consumer bound against a stale producer
  shows stale cross-repo callers until re-resolved. Documented, not silent.
- Never-guess caps the blast radius: ambiguous cross-repo names stay raw, so the
  failure mode is a missed consumer (under-report), never a false one.

## Alternatives Considered

- **Query-time cross-repo resolution** (resolve on `find_callers`, don't bake
  edges): rejected. BREAKING_CHANGES reads consumers via the target GSI, which
  needs the edges to exist; and query-time name-matching reintroduces the
  ambiguity never-guess is meant to prevent at write time.
- **Ungoverned global (all-repos) definitions index:** rejected. Binding every
  repo to every other, with no authorization, would link a consumer to an
  unrelated repo that merely shares a module name and would cross team boundaries
  silently. The accepted design is the *governed* version: the scope is the repo's
  workspace(s) plus admin-controlled `is_global` and consent grants (§1), not all
  repos.
- **Consumer-declared cross-workspace dependencies** (e.g. a repo's
  `.outcomeops.yaml` lists other workspaces to resolve against): rejected. It
  bypasses the target workspace's consent — a consumer could name, and read, a
  workspace that never granted it. The existing `search_allowed_workspaces` grant
  and `is_global` flag already express cross-workspace trust with the consent on
  the correct side, so resolution reuses them rather than introducing a second,
  weaker opt-in.
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

After this ADR (consumer resolved against the scope-wide index):
- `calls`: `python:app.main::compute_total` → `python:shared_lib.math_utils::add`
  — `find_callers(python:shared_lib.math_utils::add)` returns
  `consumer-app app/main.py` and `app/report.py`, and a BREAKING_CHANGES check on a
  `shared-lib` PR that edits `add` WARNs, naming `consumer-app` as a downstream
  consumer.
