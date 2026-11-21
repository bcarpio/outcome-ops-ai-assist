# ADR-037 — Workspace Scope is a Bidirectional Primitive

## Status: Accepted

## Context

Every OutcomeOps retrieval turn (chat, PR-check, code-gen, MCP
`query_knowledge_base`) is bounded by a workspace scope: which
workspaces' ingested content the query sees. Prior to this ADR the
scope model was implicit and inconsistent:

- **Chat and code-gen** honored per-workspace membership plus an
  explicit `search_allowed_workspaces` share-graph (workspace A
  admin grants search access to workspace B by name).
- **PR-check handlers** called `query_knowledge_base` with **no
  workspace filter at all** — the S3 Vectors index is
  workspace-scoped on write but the read walked the whole index.
  Whichever workspace's docs ranked highest globally drove the
  check result, regardless of which workspace the PR belonged to.
  Filed as `docs/tech-debt.md` "PR-check handlers query the KB
  workspace-blind" and reproduced live 2026-07-01 (identical
  handler file, same Claude model, ADR-004 violations found in the
  workspace where the standards were ingested and missed in the
  workspace where they weren't).

The literal fix — "just add a workspace filter to PR checks" — is
wrong on its own. It scopes the check to the repo's own workspace
only, cutting off the enterprise / security / compliance standards
that live in *other* workspaces. The right fix has to model
**which workspaces apply to a query**, not just isolate one.

Customer scenarios that drive the model:

| Scenario | Where standards live | How they should apply |
|---|---|---|
| Solo dev | Standards workspace + per-repo ADRs | Repo's workspace + the standards workspace |
| Fortune 500 pattern | Enterprise Arch → Org Arch → Team ADRs | Every PR walks all three tiers; local wins on conflict |
| Confluence-only security team | Security standards in Confluence (via Confluence integration), no Git ADRs | Security team's workspace applies to every PR; team doesn't want to hand-share |

The Confluence case breaks pure "share with each workspace"
manually — security teams won't do the administrative work, and
ad-hoc shares drift. The share-graph model (`search_allowed_workspaces`)
handles pairwise sharing correctly but doesn't scale to "this
workspace applies to everyone" without N pairwise operations.

## Decision

Introduce **`is_global: bool` on the workspace row** as a
bidirectional scope primitive. When true:

1. **Content push.** The workspace's ingested content is included
   in every user's chat retrieval pool (opt-in via the workspace
   picker; not auto-selected) and in every PR check's retrieval
   pool for the standards checks (per the per-check-type table
   below).
2. **Members' pull.** Members of a global workspace get implicit
   read access on every other workspace's content when querying
   from the global workspace. Security / EA / Compliance need
   cross-portfolio visibility to do their job; this eliminates the
   "beg every team to hand-share their workspace" administrative
   tax.

### Key points

- **Bidirectional is deliberate.** Splitting into two flags
  (`content_push` and `cross_workspace_read`) doubles the
  admin-decision cost across every global-workspace provisioning
  event and serves ~5% of edge cases (typically IT teams that
  publish policies but don't need to read app code). Those teams
  already have cross-org visibility on Jira / ServiceNow /
  wiki / etc.; adding a knowledge-base surface is not the security
  frontier for them. Escape hatch is cheap to add later: a
  `content_only: bool` sub-flag defaults false; every existing
  global workspace stays behavior-compatible.

- **Provisioning-time, org-admin-only.** `is_global` is set at
  workspace creation by an org admin. There is no in-product
  propose/approve state machine. Companies gate with their own
  ticketing (ServiceNow, etc.). The workspace-create UI surfaces
  a two-step confirmation with both consequences spelled out so
  the admin sees the bidirectional-read implication before
  submitting. Flag changes are audit-logged via a dedicated
  `WORKSPACE_GLOBAL_FLAG_SET` action distinct from the generic
  `WORKSPACE_UPDATED` so compliance auditors can filter for
  scope-affecting changes.

- **Per-check-type PR retrieval scope.** Not every check should
  broaden identically:

  | Check | Scope |
  |---|---|
  | `ADR_COMPLIANCE`, `TEST_COVERAGE`, `LICENSE_COMPLIANCE` | Read scope |
  | `ARCHITECTURAL_DUPLICATION` | Read scope |
  | `BREAKING_CHANGES` | Consumer scope (ADR-056 §5) |
  | `README_FRESHNESS` | Repo's workspace(s) only |

  **Read scope** is the set of workspaces the repo's workspace may read:
  the repo's workspace(s), plus every `is_global` workspace, plus every
  workspace that allowed the repo's workspace to search it (that
  workspace's `search_allowed_workspaces`, set on its Sharing tab).
  Check types not in the table fall back to the repo's workspace(s)
  only.

  Why one read scope for every retrieval check (v1.2):
  - Standards can live in any workspace and come from any connector
    (Confluence, Git, and so on). An org may publish them in a global
    workspace, or share a workspace with specific teams. Both must
    apply to a PR check, so the checks honor `is_global` and sharing
    the same way chat does.
  - `ARCHITECTURAL_DUPLICATION` used to scan every workspace in the
    deployment, which ignored access control: a PR could be compared
    against content its own workspace has no right to read. It now
    sees exactly what the repo's workspace may read.
  - No workspace id is configured or hard-coded anywhere. Scope comes
    only from workspace rows (`is_global`, `search_allowed_workspaces`,
    and the repo rows).

  `README_FRESHNESS` is about *this* repo's README; broadening to org
  standards would drag in irrelevant docs. `BREAKING_CHANGES` asks a
  different question (who depends on this repo, not what this repo may
  read), so it uses the consumer direction from ADR-056 §5.

- **Repo lookup is provider-aware.** The repo's workspace(s) are found
  from the repo row for the PR's code host: GitHub, GitLab, or Azure
  DevOps, each with its own repo-row key, using the same per-provider
  keys as the repo-presence helper (ADR-052). A GitLab or Azure DevOps
  PR finds its own workspace the same way a GitHub PR does, instead of
  getting only the global workspaces.

- **Precedence on conflict.** When retrieval walks multiple
  workspaces and standards contradict, local (repo's workspace)
  wins over global — matching the pattern the code-generation
  path already implements. For v1 this rides on ranker + prompt
  discipline (no explicit score biasing). Add scoring bias only
  if a customer surfaces a specific miss.

- **No opt-out in v1.** A workspace-side "excluded_from_global_read"
  flag would let (say) a Legal-contracts workspace opt out of
  being pulled into a security-team member's cross-workspace
  read. Real for a Salesforce-contracts connector world but out
  of scope for the current engineering-focused ICP
  (Product / Arch / Security / Eng / IT + coding agents). Ship
  the opt-out at the same time as the Salesforce connector when
  the specific risk shape justifies the field; existing global
  workspaces stay behavior-compatible.

### Example implementation

The primitive lives on the workspace metadata row:

```python
# lambda/workspace-management/handler.py
class CreateWorkspaceRequest(BaseModel):
    name: str
    description: Optional[str] = None
    privacy_mode: Optional[str] = None
    is_global: bool = Field(default=False)
```

Chat scope resolution:

```python
# lambda/chat/handler.py
def resolve_chat_workspace_scope(
    user_email: str,
    requested_workspace_ids: List[str],
) -> List[str]:
    """
    1. Standard access check passes through.
    2. is_global workspaces readable by anyone.
    3. Global-member querying *from* the global workspace ->
       broaden to every workspace in the deployment.
    """
```

PR-check scope resolution:

```python
# lambda/shared/pr_check_scope.py
def resolve_pr_check_workspace_scope(
    check_type: str, repo_name: str, table=None, *, provider: str = "github",
) -> List[str]:
    if check_type in READ_SCOPE_CHECKS:
        # repo ws + is_global + workspaces that shared with the repo ws
        return resolve_repo_definition_scope(repo_name, table, provider=provider)
    if check_type == "BREAKING_CHANGES":
        return resolve_consumer_query_scope(repo_name, table, provider=provider)
    # README_FRESHNESS + unknown fallback
    return find_workspaces_for_repo(repo_name, table, provider=provider)
```

Retrieval on the query-kb side accepts the resolved list:

```python
# lambda/query-kb/handler.py
def query_s3_vectors(
    query_embedding, top_k=5,
    repo_filter=None, doc_types=None,
    workspace_ids=None,  # <-- new
):
    filter_dict = {"workspace_id": {"$in": workspace_ids}} if workspace_ids else None
    ...
```

## Consequences

### Positive

- **Closes the workspace-blind bug.** PR checks now walk the right
  workspaces per check type; the reproduced ADR-004-missed-on-ADO
  live-repro can no longer happen (an admin who's ingested standards
  into an `is_global` workspace has them applied to every repo's
  PR checks regardless of which workspace owns the repo).
- **Solves the Confluence-security-team case.** Security /
  Compliance / Enterprise Architecture publish once into their
  `is_global` workspace; no per-team hand-shares. Content flows
  everywhere; their members query cross-portfolio without begging.
- **Consistent scope model across every surface.** Chat, PR
  check, code-gen, and MCP now resolve scope through the same
  primitive. External agents (Harness partner integration, etc.)
  reason about scope by reading `is_global` on `list_workspaces`
  responses.
- **Auditor traceability.** Source citations returned by
  `query-kb` now carry the source workspace's name
  (`ADR-014 (Enterprise Architecture)`) so a compliance auditor
  scanning PR-check comments can see which tier of standards
  drove the finding. Dedicated audit action on flag flips.
- **HA parity built in.** `is_global` writes go through
  `dual_write.ddb_update_item` (same shape as every other
  workspace-row write); secondary picks up the field with no
  extra work.

### Tradeoffs

- **Scan cost on PR-check retrieval.** The scope resolver runs
  filtered DynamoDB scans (repo to workspace, the `is_global` set,
  and, for the read scope, the workspaces that shared with the repo's
  workspace) on each check invocation. Not a hot path (once per check
  per workspace), but at F500 scale (10^3 workspaces × 10^3 checks
  per day) this warrants a follow-up to add a GSI on `is_global`
  and cache the repo→workspace lookup (tracked in GitHub issue #28).
- **Global membership grants broad read.** Members of an
  `is_global` workspace see every other workspace's content when
  querying from the global workspace. For most orgs
  Security/EA/Compliance are already trusted with that
  visibility (Jira/ServiceNow/wiki). For the rare org that
  wants to restrict, the escape hatch is (a) don't grant
  membership casually, and (b) v2 `content_only` sub-flag.
- **`ARCHITECTURAL_DUPLICATION` no longer sees every workspace.**
  Since v1.2 it is bounded by the read scope, so duplication in a
  workspace that neither is global nor shared with the repo's
  workspace is not reported. That is the access-control boundary
  working as intended; an org that wants a cross-portfolio view
  marks the relevant workspaces global or shares them.
- **Bidirectionality is a coarse primitive.** The Fortune 500
  three-tier model (Enterprise → Org → Team) flattens to
  two-tier (global vs local). Enterprise gets `is_global=true`
  and Org-level tier collapses into either global (over-broad)
  or workspace-local (under-scoped). Real hierarchical scoping
  is a v2 conversation.
- **Ranker fairness across workspaces.** When retrieval walks
  N workspaces, a docs-heavy workspace can systematically
  out-rank a code-map-heavy workspace on prose questions. The
  dual-channel primitive (ADR-033) handles the docs-vs-code-map
  lane split within a workspace but not across workspaces. If
  this becomes a real drift signal, add per-workspace budget
  quotas at rerank time.

## Implementation

### Starting today (shipped 2026-07-03)

1. **`is_global` field on the workspace row.** Both
   `CreateWorkspaceRequest` and `UpdateWorkspaceRequest` accept it;
   the create + update handlers persist via `dual_write`. Org-admin
   gate on flips. Dedicated `ACTION_WORKSPACE_GLOBAL_FLAG_SET`
   audit action. `list_workspaces` (both web and MCP) returns
   every `is_global` workspace tagged `role="global_reader"` to
   non-members. `docs/mcp-toolset-spec.md` §5.1 widened.
2. **UI Global badge + create-modal toggle** with a two-step
   confirmation modal spelling out the bidirectional-read
   consequence in copy.
3. **Chat scope resolver** (`resolve_chat_workspace_scope`) —
   pass-through / global-content push / bidirectional-broaden
   rules from the design table.
4. **PR-check per-check retrieval scope**
   (`shared/pr_check_scope.py`) — standards / local-only /
   cross-portfolio routing table. All 5 KB-reading check
   handlers wired. `query-kb` accepts `workspaceIds`. `process-pr-check`
   IAM widened for the scope resolver's `dynamodb:Scan`. Every
   handler fails soft to workspace-blind on scope-resolver
   error — the fix can never regress a working check.
5. **Citation polish.** `format_source` and `query_s3_vectors`
   surface workspace name per source; `WORKSPACES_TABLE` +
   `dynamodb:BatchGetItem` grant added to query-kb Lambda.
6. **MCP per-tool access gates.** Chunk 01 widened the
   `list_workspaces` enumeration, but every per-tool call
   still gated on direct membership via
   `_check_symbol_graph_workspace_access` (used by all 8
   symbol-graph tools including `read_source`) and the inline
   check in `handle_list_workspace_documents`. Both now
   pass through when the target workspace is `is_global=true`,
   via a shared `_workspace_is_global` DDB helper. Fail-soft:
   any DDB error collapses back to membership-only.
   Discovered when Claude Code hit "You do not have access"
   invoking `read_source` on a Global workspace it could see
   in `list_workspaces`. Fourth per-surface fix after chat
   resolver, searchable-workspaces, and MCP `list_workspaces`.

### Next phases

1. **v2 `content_only` sub-flag** if an IT-team-shaped customer
   surfaces a concrete request. Every existing global workspace
   defaults to bidirectional (unchanged); IT admins can flip
   theirs. No migration.
2. **v2 `excluded_from_global_read`** paired with a Salesforce or
   similar customer-data connector when that connector's risk
   shape justifies the field. Ships alongside the connector, not
   as a standalone workspace-primitive extension.
3. **v2 hierarchical parent-workspace** for the F500 three-tier
   (Enterprise → Org → Team) case. Only if a real prospect asks;
   premature abstraction bites here.
4. **Ranker cross-workspace fairness.** If PR-check retrieval
   consistently loses code-maps to docs across-workspaces, extend
   ADR-033's per-lane budget with per-workspace quotas.
5. **`is_global` GSI.** Replaces the filtered scans in
   `pr_check_scope.py` with an index lookup once per-check
   retrieval frequency justifies the extra cost of index writes.

## Related ADRs

- **ADR-007** — Documentation-Driven Decisions. Workspace scope
  is architectural (a customer-facing model), so it belongs in
  the ADR record, not just plan/ notes.
- **ADR-025** — Code Wins Over Docs on Precedence. Cross-workspace
  retrieval can amplify docs-drift; local-over-global precedence
  echoes the same posture at the workspace-scope layer.
- **ADR-033** — Dual-Channel Retrieval. The lane split (code-maps
  vs docs) is per-workspace; cross-workspace fairness is the
  follow-up if this ADR's cross-workspace retrieval trips the
  same starvation pattern.

## References

- `lambda/workspace-management/handler.py::create_workspace` +
  `update_workspace` — MCP-server `is_global` field on
  `CreateMcpServerRequest` (the pattern this ADR extends to
  workspaces themselves).
- `lambda/chat/handler.py::resolve_chat_workspace_scope`.
- `lambda/mcp-server/handler.py::_workspace_is_global` +
  `_check_symbol_graph_workspace_access`.
- `lambda/shared/pr_check_scope.py`.
- `docs/mcp-toolset-spec.md` §5.1.

Version History:

- v1.0 (2026-07-03): Initial decision. Chunks 01-05 shipped.
  Closes the workspace-blind tech-debt entry.
- v1.1 (2026-07-03): MCP per-tool access gates widened to
  honor `is_global` (fourth per-surface fix). Covers all 8
  symbol-graph tools plus `read_source` and
  `list_workspace_documents`.
- v1.2 (2026-11-21): PR retrieval checks (`ADR_COMPLIANCE`,
  `TEST_COVERAGE`, `LICENSE_COMPLIANCE`, `ARCHITECTURAL_DUPLICATION`)
  use the read scope (repo workspace(s) + `is_global` + workspaces
  shared with the repo's workspace). `ARCHITECTURAL_DUPLICATION` no
  longer scans every workspace. Repo-to-workspace lookup is
  provider-aware (GitHub, GitLab, Azure DevOps).

<!-- Confluence sync -->
