# ADR-062: The Backlog Lives in GitHub Issues

## Status: Accepted

## Context

Work that was noticed but not done used to be tracked in two Markdown files,
`docs/backlog.md` and `docs/tech-debt.md`. That worked while the repository had
one maintainer and no outside contributors. It stopped working once the
project became open source:

- A Markdown entry cannot be assigned, labelled, discussed, linked to the pull
  request that fixes it, or closed by a merge. Contributors cannot see what is
  planned or pick something up.
- The two files drifted. By the time they were retired, two of the entries
  had already shipped, one entry appeared in both files, and the files linked
  into ADRs and code comments that went stale with them.
- Larger pieces of work were scoped in local, gitignored `plan-<name>/`
  directories. Those are the right tool for sequencing implementation, but
  they are invisible to everyone except the person who wrote them, so the
  intent behind them was not public either.

There is one constraint specific to this repository: GitHub Issues already
have a job here. The code-generation pipeline builds an issue when it carries
the `approved-for-generation` label (`.github/ISSUE_TEMPLATE/code-generation.md`).
Backlog issues must never trigger it.

## Decision

**The public backlog is the repository's GitHub Issues.** Every feature,
bug, follow-up and piece of technical debt that is not being done right now
is an issue. Nothing backlog-shaped is kept under `docs/`.

### Key Points

- **One template for backlog items.** `.github/ISSUE_TEMPLATE/backlog.md`
  asks for Problem, Proposal, Acceptance and Related (ADRs, PRs, issues). The
  code-generation template stays separate and is only for issues the
  pipeline should build.
- **Labels carry type, priority and area**, so the list can be filtered
  without reading every issue:

  | Kind | Labels |
  |---|---|
  | Type | `bug`, `enhancement`, `tech-debt`, `documentation` |
  | Priority | `priority: high`, `priority: medium`, `priority: low` |
  | Area | `area: pr-checks`, `area: ingestion`, `area: symbol-graph`, `area: models`, `area: terraform`, `area: integrations`, `area: dx` |
  | Contributor-friendly | `good first issue`, `help wanted` |

  An issue gets one type, one priority and at least one area. New areas are
  added when a part of the system has enough issues to need its own filter.
- **`approved-for-generation` is never applied to a backlog issue.** That
  label hands the issue to the code-generation pipeline.
- **Plans stay local and point at an issue.** When someone picks an issue up,
  they may scope it in a local, gitignored `plan-<name>/` directory. The
  plan's overview links the issue; the pull request says `Closes #N`, so the
  merge closes it. Plans never become the record of intent: the issue is the
  public record before the work, and the ADR and commit history are the
  record after it.
- **Decisions still go in ADRs.** An issue says what should be done; an ADR
  captures a decision that must outlive the work. A follow-up named in an ADR
  links its issue instead of promising "a later fix".
- **Nothing private in an issue.** Issues are public: no customer names,
  cloud account ids, workspace ids, private repository names or credentials.

### Example

```markdown
## Problem
BREAKING_CHANGES cannot compare signatures for Go functions, so every change
with a consumer in another repo needs manual review.

## Proposal
Capture parameters and results on Go `defines` facts.

## Acceptance
- [ ] A body-only Go change with a cross-repo consumer passes.

## Related
ADR-054 v1.2
```

Labels: `enhancement`, `priority: medium`, `area: symbol-graph`.

## Consequences

### Positive

- Contributors can see what is planned, discuss it and pick work up.
- Every fix closes its issue on merge, so the backlog cannot silently go stale
  the way the Markdown files did.
- Labels make the backlog filterable by area and priority.

### Tradeoffs

- Prioritisation across issues is by label only. If ordering within a
  priority starts to matter, add a GitHub Project board on top; this ADR does
  not require one.
- Two issue templates share one issue tracker, so a backlog issue labelled
  `approved-for-generation` by mistake would start code generation. The
  backlog template carries a warning; reviewers remove the label if it
  appears.

## Implementation

### Starting today

1. `docs/backlog.md` and `docs/tech-debt.md` are removed; their open entries
   are issues.
2. `.github/ISSUE_TEMPLATE/backlog.md` and the labels above exist.
3. `CONTRIBUTING.md` and the README point at the issue list.

## Related ADRs

- ADR-001: Creating ADRs
- ADR-007: Documentation-driven decisions

## Version History

- v1.0 (2026-11-21): Backlog moved from Markdown files to GitHub Issues.
