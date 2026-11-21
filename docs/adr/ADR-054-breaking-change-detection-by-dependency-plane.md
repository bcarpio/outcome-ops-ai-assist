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

### Plane 1: which symbols changed and who consumes them

Plane 1 has two questions to answer before it can name a consumer: which symbols
did the PR actually change, and which callers are really calling *this* symbol.
Both have rules.

- **Changed symbols are resolved on the base side of the diff.** The symbol graph
  reflects the base (default) branch, so a PR head line number cannot be looked up
  in it: every inserted line shifts the lines below it, and a head line number
  lands on whatever symbol happened to sit there on the base branch. The check
  instead derives contiguous change blocks from the base/head diff and resolves
  them in base coordinates:
  - Removed or replaced lines resolve on their own base line numbers.
  - A pure insertion (new lines with nothing removed) counts only when the base
    lines immediately before and after the insertion point resolve to the same
    symbol, meaning the new lines landed inside that symbol's body. The graph
    stores only a symbol's start line, so looking at one side alone would blame
    the preceding function for lines added between two functions.
  - Findings still anchor on a real added head line, per ADR-031. A deletion-only
    change with no added line in the file is listed in the check summary without
    an inline finding, because there is no head line to anchor on.
- **A repo that defines its own copy of a symbol is not its consumer.** Symbol ids
  are path-based and not repo-qualified, so two repos that contain the same file
  path share symbol ids. For example, another repo with its own
  `lambda/github-integration/handler.py` produces callers of
  `python:lambda.github-integration.handler::lambda_handler` that have nothing
  to do with the PR's repo. A caller in another repo counts as a consumer only if that
  repo does not itself define the symbol; if it does, it is calling its own copy.
- **Fail closed, unchanged.** If the diff blocks for a file or the definitions
  lookup are unavailable, the file is reported as "could not determine consumers,
  manual review" rather than as safe.
- **One verdict.** When plane 1 reports for a PR, the knowledge-base pass's "safe
  to merge" text is not shown. A reviewer sees the plane-1 result, not two
  verdicts that can contradict each other.

### Plane 1: contracts that cross a deployment boundary (v1.2)

Plane 1 reports contracts between deployable units, not every call site.

- **Same-repo callers and importers are not consumers.** They ship in the same
  deploy as the changed symbol, so no contract between services can break, and
  the repo's own tests cover them. A caller or importer counts as a consumer only
  when it lives in another repo (and, per the rule above, that repo does not
  define its own copy of the symbol).
- **Signature changes WARN; body-only changes do not.** For a symbol with a
  consumer in another repo, the check compares the symbol's signature on the base
  branch, as recorded in the symbol graph, with its signature on the head branch,
  produced by running the same extractor that builds the graph on the head
  version of the file.
  - A changed signature (parameters, return type) or a removed or renamed symbol
    is a WARN that names the consuming repos.
  - A body-only change is listed in the details as a behaviour change with its
    consuming repos, without a WARN. The contract still holds; the reviewer sees
    who might notice the new behaviour.
  - A signature that cannot be compared (no extractor for the language, a graph
    row written before signatures were recorded, a parse failure on either side)
    fails closed: WARN "could not compare signature, manual review".
- **Plane 2 and the fail-closed paths are unchanged.** The knowledge-base pass and
  the "could not determine consumers" result behave as described above.

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
- **Known limits of base-side resolution.** Lines inserted at the very end of a
  file have no base line after them, so they are not attributed to the preceding
  symbol and do not produce a plane-1 finding. The graph may also lag the PR base:
  it is built from the last ingested default-branch commit, so a symbol added or
  moved on the default branch since that ingest may resolve to a stale location
  or not at all.

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

## Version History

- v1.0: initial decision
- v1.1 (2026-11-20): base-side changed-symbol resolution, own-definition
  consumer filter, single verdict
- v1.2 (2026-11-21): plane 1 reports only consumers in other repos; base/head
  signature comparison (signature change WARNs, body-only change is listed
  without a WARN, uncomparable signature fails closed)
