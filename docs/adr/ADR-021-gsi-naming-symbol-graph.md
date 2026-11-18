# ADR-021: GSI Naming Convention for the Symbol-Graph Table

## Status: Accepted

## Context

The symbol-graph feature introduced a new DynamoDB table — the
symbol-graph table — backed by two Global Secondary Indexes (GSIs):

- `target-kind-index` — GSI1 partition by `<workspace_id>#<target_symbol_id>`,
  range by `<kind>#<repo>#<file_path>#<line>`. Powers the
  caller / implementer / importer queries the chat, process-pr-check, and
  generate-code Lambdas issue against the graph.
- `target-prefix-index` — GSI2 partition by `<workspace_id>#<package_head>`,
  range by `<subpath>#<kind>#<repo>#<file_path>#<line>`. Powers the
  subpath-import queries (e.g. `@acme-corp/shared-utils/http2`).

The OutcomeOps platform's Terraform infrastructure standards ADR
(`ADR-001` in the KB workspace — distinct from this repo's `ADR-001`
which documents the ADR process itself) prescribes:

- snake_case Terraform file names
- exact module-version pins
- `hash_key = "PK"`, `range_key = "SK"`
- `BillingMode = "PAY_PER_REQUEST"`
- `${var.environment}-${var.app_name}-<purpose>` resource naming
- exact-action / exact-ARN IAM policies (no wildcards)

That ADR is silent on **GSI naming**. The symbol-graph table
shipped with a convention that reads cleanly at the query call site
(`reader.callers(workspace_id, target_symbol_id)` selecting the
`target-kind-index`). This ADR documents and ratifies that
convention so future graph-adjacent tables can extend it without
re-deriving the same shape from scratch.

## Decision

GSIs on the symbol-graph family of tables follow:

```
<noun>-<discriminator>-index
```

Where:

- `<noun>` — what the partition key resolves to from the caller's
  perspective. The two existing GSIs partition by symbol identity
  (`target` — i.e. the symbol the edge points at), so the noun is
  `target`. Future GSIs over the same family that partition by, e.g.,
  the source symbol would be `source-...-index`.

- `<discriminator>` — the secondary axis that distinguishes one GSI
  from another over the same noun. `kind` for the
  edge-kind-discriminated index (calls / imports / implements / refs
  all collapse onto one partition, the range key carries `<kind>#...`).
  `prefix` for the subpath-import index whose partition key is the
  package head and the range key is the package tail.

- Suffix `-index` — explicit, matches the implicit DynamoDB convention
  and reads correctly in CloudWatch dimensions
  (`Index = target-kind-index`).

Names are kebab-case (DynamoDB GSI names are case-sensitive but
DynamoDB itself prefers kebab-case for readability in the console).

### Existing names that conform

- `target-kind-index` (symbol-graph table, GSI1) — partitioned by
  `(workspace_id, target_symbol_id)`; backs callers / importers /
  implementers / definitions queries.
- `target-prefix-index` (symbol-graph table, GSI2) — partitioned by
  `(workspace_id, package_head)`; backs subpath-import wildcard
  queries.
- `target-name-index` (symbol-graph table, GSI3) — partitioned by
  `(workspace_id, bare_name)`; backs the chat-side fuzzy resolver
  that maps user-typed bare names ("require_auth") to canonical IDs
  without a full-table Scan. Bounded by actual matches per Query
  rather than by workspace size.

### Names a future contributor would derive

- `source-kind-index` if a future query needs "every edge where the
  source symbol is X" partitioned by the source side.
- `repo-commit-index` if a future query needs to enumerate every fact
  for a `(repo, commit_sha)` pair.

## Consequences

### Positive

- New graph-adjacent tables inherit a regular GSI-naming shape; the
  query-side code reads cleanly.
- CloudWatch dimensions (e.g. the symbol-graph p99 latency alarm in
  `terraform/cloudwatch_symbol_graph.tf` dimensions on
  `Index = target-kind-index` / `target-prefix-index`) stay
  self-documenting.
- Reviewers can spot a non-conforming GSI name at a glance — the
  shape is short enough to recognize.

### Tradeoffs

- The `<noun>-<discriminator>-index` shape only fits tables whose
  GSIs partition by an entity-shaped key. Tables whose GSIs partition
  by a category / status / type still fit by treating those as nouns
  (`status-<discriminator>-index`), but the analogy stretches.
- This convention is not retroactive to non-graph tables (audit,
  workspaces, conversations) that pre-date it; their existing
  `GSI1` / `GSI2` AttributeName conventions stay as-is to avoid
  thrashing live data.

## Implementation

### Starting today

1. Any new GSI on the symbol-graph family of tables (or future
   graph-adjacent tables) follows `<noun>-<discriminator>-index`.
2. The two existing names are grandfathered as conformant.
3. Reviewers reject net-new GSIs whose names don't fit the shape
   without a corresponding ADR update.

### Next phases

1. If a future feature needs a GSI shape this ADR can't naturally
   accommodate, **amend this ADR** rather than introducing an
   ad-hoc name — the cost of a one-paragraph amendment is far less
   than the cost of a second naming convention drifting in.

## Related ADRs

- ADR-018: Audit Logging Design — uses the older `GSI1` /
  `GSI2` AttributeName convention; not retro-renamed.
- `terraform/dynamodb_symbol_graph.tf` and
  `lambda/shared/symbol_graph_writer.py` — the schema and writer
  this naming convention serves.
- `docs/symbol-graph-validation.md` — operator runbook that uses the
  GSI names from this convention.

## References

- `terraform/dynamodb_symbol_graph.tf` — the original GSI
  declarations.
- `lambda/shared/symbol_graph_reader.py` — the query side that
  selects on these index names.
- `terraform/cloudwatch_symbol_graph.tf` — the p99 latency alarm
  dimensioned on `Index`.

Version History:
- v1.0 (2026-05-14): Initial decision. Ratifies the `target-kind-index`
  / `target-prefix-index` names the symbol-graph table shipped with,
  and locks in the `<noun>-<discriminator>-index` shape for future
  graph-table GSIs.
- v1.1 (2026-05-16): Adds `target-name-index` to the list of
  conforming names. Same shape, same table; partitions by
  `(workspace_id, bare_name)` to replace the chat handler's
  Scan-based fuzzy resolver with a Query bounded by actual matches.
  Naming convention itself is unchanged — this is a pure additive
  amendment, not a re-evaluation.
