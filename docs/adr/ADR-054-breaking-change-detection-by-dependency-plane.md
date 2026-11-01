# ADR-054: Breaking-Change Detection by Dependency Plane (Symbol Graph + Infra Topology)

## Status: Proposed

## Context

The BREAKING_CHANGES PR check answers one question: *did this change break
something that depends on it?* "Something that depends on it" is a **consumer** —
and a consumer depends on the changed code through one of several different
*contracts*, not one. The original implementation collapsed all of them into a
single fuzzy signal: it vector-searched `handler-group-summary` documents for the
changed handler's directory-name and flagged a HIGH-confidence dependency when a
returned summary's prose contained that literal token next to a word like
"invoke" or "queue".

That approach has proven both unsound and imprecise:

- **It was silently non-functional for a period** — it invoked a `vector-query`
  Lambda that had been folded into `query-kb`, so every lookup failed and the
  check always reported "No dependencies found" (fail-open). That specific defect
  is fixed, but it exposed how little the check could be trusted.
- **The confidence rule is literal-token based.** Real `handler-group-summary`
  text describes a handler in prose ("the scheduler", an internal function name),
  so a genuine consumer relationship is missed whenever the summary does not
  happen to contain the exact directory-name token.
- **The retrieval signal is the wrong source of truth.** A `handler-group-summary`
  is a generated description, not a record of who actually calls whom or who reads
  a given queue. Document-type filtering on the retrieval also leaks (a query
  scoped to `handler-group-summary` still returns markdown and test summaries),
  so ranking noise decides whether a real dependency is seen.

The root problem is conceptual: **"dependency" is not one relationship.** A
consumer is coupled to a change through a specific *contract*, and each contract
has a different, authoritative source of truth. Detecting all of them with one
prose-similarity heuristic is why the check is unreliable.

This matters disproportionately at the target scale (an enterprise with hundreds
or thousands of repositories): the governance question a reviewer actually needs
answered is "did this PR break another team's consumer," and that answer must be
precise enough to trust.

## Decision

**Detect breaking changes per dependency plane, each from its own authoritative
source of truth, rather than approximating all of them with document similarity.**
Internal (service-to-service within one deployment) and cross-repository
dependencies are both in scope — the planes are the same either way.

### The dependency planes

1. **Code-symbol contract.** A changed function/class/constant signature, and the
   code that calls or imports it. **Source of truth: the symbol graph.** The
   consumers are exactly the callers and importers of the changed symbol
   (`shared.symbol_graph_reader` — `callers`, `importers`, resolved from the
   changed lines via `definitions` / `symbol_at_line`). This is precise, requires
   no inference, and is **cross-repository** because the symbol graph already
   spans every ingested repo.

2. **Message / stream contract.** A change to the shape of what a producer writes
   onto an SQS queue, Kinesis stream, SNS topic, or EventBridge bus. The consumer
   is whoever **reads** that queue/stream — and this is **not a code call edge**,
   so the symbol graph does not contain it. **Source of truth: infrastructure
   topology**, derived from the ingested Terraform: producers are principals with
   send/publish/put grants (e.g. `sqs:SendMessage`, `kinesis:PutRecord`,
   `sns:Publish`) on the resource, consumers are the event-source mappings /
   subscriptions bound to it, keyed by the resource ARN/name. A change to a
   producer's payload-shaping code flags the resource's consumers.

3. **API-endpoint contract.** A changed REST/HTTP route request or response shape,
   called over the wire by another application. The consumer is a cross-repo HTTP
   client. **Deferred** (see below): precise detection needs a route/OpenAPI
   registry plus cross-repo client analysis, which neither the symbol graph nor
   the Terraform topology provides today.

### Key Points

- **Planes 1 and 2 are in scope now; plane 3 is explicitly deferred.** Shipping
  precise detection for code-symbol and message/stream contracts is strictly
  better than the status quo; the API plane is a later, separately-scoped
  capability and MUST NOT be faked with the old heuristic.
- **Fail closed, never fail open.** Consistent with the ADR-compliance principle:
  if the symbol graph or the Terraform topology cannot be read for a changed
  component, the check reports an explicit "could not determine consumers —
  manual review" rather than silently declaring the change safe.
- **Advisory severity.** A real consumer of a changed contract produces a WARN
  with the consumer(s) named, line-anchored per ADR-031 (anchor on the changed
  definition/producer line in the diff; never guess a line). It does not hard-fail
  the PR — the author has the context to decide.
- **The retrieval heuristic is retired as the primary signal.** It may remain only
  as a clearly-labelled, low-confidence *supplement* for components the structured
  signals cannot cover, and only while it cannot produce a false "safe" verdict.
- **Parity is mandatory (ADR-036 / ADR-050).** The detection logic is
  provider-agnostic; the finding/anchor plumbing must work for GitHub, GitLab, and
  Azure DevOps in the same change, per the existing inline-comment contract
  (ADR-031).

## Consequences

- **Precision up, noise down.** Plane 1 becomes deterministic (graph edges, not
  prose), and plane 2 stops inventing "consumers" out of documentation and
  workflow YAML.
- **New capability required for plane 2:** a producer/consumer topology derived
  from the ingested Terraform (IAM send/publish grants + event-source mappings /
  subscriptions, keyed by resource). Its accuracy is bounded by Terraform
  ingestion completeness; gaps fail closed to "manual review", not to "safe".
- **Coverage is bounded by the symbol graph and ingestion.** A consumer in a repo
  that has not been ingested, or a symbol the graph does not resolve, will not be
  found — this is a known, explicit limit, surfaced as reduced confidence rather
  than a false "safe".
- **Cross-repo detection is available for plane 1 now** because the symbol graph is
  multi-repo; plane 2 is cross-repo wherever both sides' Terraform is ingested.
- **The API plane remains a gap** until a route/contract registry exists; that is
  stated honestly to buyers rather than approximated.

## Alternatives Considered

- **Keep the vector-similarity + literal-token heuristic.** Rejected: it is the
  source of both the misses and the false positives; it is a proxy for a signal we
  can obtain directly.
- **Symbol graph only.** Rejected: it captures plane 1 but has no edge for
  message/stream or API contracts, which are the dependencies an enterprise most
  wants caught.
- **A full contract registry up front (AsyncAPI / OpenAPI + declared
  dependencies).** Deferred: it is the eventual home for planes 2 and 3, but it is
  a larger program; deriving plane-2 topology from existing Terraform delivers most
  of the value now without a new authoring burden on customers.
