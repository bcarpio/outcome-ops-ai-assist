# ADR-039: Azure DevOps Boards is a separate integration, not a Repos extension

## Status: Accepted

## Context

Azure DevOps has two knowledge surfaces the platform can consume:

- **Repos** — source code, PRs, branches. Shipped in ADR-036.
- **Boards** — work items (bugs, user stories, tasks, epics, features)
  and their comments.

Repos ingests through the same code-map + symbol-graph + retrieval
path used by GitHub. Boards ingests through the same document-retrieval
path used by Jira. The two surfaces have different content shapes
(structured code vs. structured narrative), different customer
consent conversations ("give the platform read on our code" vs.
"give it read on our work-tracking system"), and different license-
cost intuitions ("per repo" vs. "per project").

The question at the design stage was whether to bundle Boards into
the existing ADO Repos integration (one connection, one workspace-
settings tab, one license bucket) or ship it as a sibling of Repos
(two connections, two tabs, two license buckets). This ADR captures
the decision to ship as a sibling.

## Decision

**Boards is a distinct integration.** It reuses everything that isn't
customer-facing (same Entra ID app registration, same tenant/client
config, same Function URL infrastructure, same shared
`entra_id_oauth` helper) but presents itself as a separate connection
type end-to-end: its own OAuth consent, its own workspace-settings
section, its own license-counted identifier prefix, its own DDB
row family, its own Lambda functions.

### Concretely

- **Separate OAuth scope.** Boards requests exactly `vso.work`
  (read-only) at token exchange. Repos requests `.default` (the union
  of everything the customer admin granted, including `vso.code` /
  `vso.code_write` for PR-comment posting). The customer's Entra ID
  app registration grants API permissions for both.
- **Separate DDB namespace.** `AZDO_BOARDS_CONNECTION#{connection_id}`,
  `AZDO_BOARDS_PROJECT#{org}/{project}`, `AZDO_BOARDS_TOKEN#{connection_id}`
  are distinct from Repos's `AZDO_CONNECTION#`, `AZDO_REPO#`,
  `AZDO_TOKEN#` families. A workspace admin can disconnect one
  integration without touching the other.
- **Separate license-counted identifier.** Repos projects (repos)
  count as `azure_devops:{org}/{project}/{repo}` per source. Boards
  projects count as `azure_devops_boards:{org}/{project}` — a
  distinct namespace. A customer with the same org/project connected
  for both consents counts twice (each is a separately-consented
  ingest surface with independent knowledge shape). See
  `lambda/shared/license_repos.py`.
- **Separate Lambda triple.** `azure-devops-boards-integration`,
  `azure-devops-boards-sync`, `azure-devops-boards-scheduler` each
  ship as their own zip, own Function URL / event-source, own IAM.
  Repos's three Lambdas stay decoupled.
- **Separate secondary-region deployment.** Every Boards Lambda
  duplicates under `aws.secondary` with the same both-MRK-ARN
  KMS grant pattern used across ADR-030 v1.1, chunk-05 Boards
  primary, and the OAuth-KMS fix in commit `389c1e7`. See
  `terraform/lambda-azure-devops-boards-secondary.tf`.
- **Separate workspace-settings UI section.** The Integrations tab
  shows "Azure DevOps (Repos)" and "Azure DevOps (Boards)" as two
  cards with sub-labels so operators can tell them apart at a
  glance.

### Why not bundle

- **Scope isolation.** Some customers grant code access but not
  work-item access (compliance constraint: work items contain
  customer PII the platform shouldn't ingest). Bundled would mean
  either denying such customers Repos, or shipping a "half-connected"
  state that's confusing to explain to auditors.
- **Independent disconnect.** A workspace admin can revoke Boards
  without losing Repos, and vice versa. Bundled would either force
  losing both together or introduce fine-grained disconnect state
  that duplicates most of the complexity of two connections.
- **Different sync cadence potential.** Work items change more
  frequently than code in some customers' workflows. Bundled would
  either lock both to the same cadence or introduce per-content-type
  cadence knobs on the connection record. Ship simple: hourly for
  both today; either one can move without dragging the other.
- **Different connection model.** Boards operates at project
  granularity; Repos at repo granularity. Bundled would produce an
  awkward "do you want repos or work items from this project?"
  picker.
- **Mirrors Jira pattern.** Customers who use both GitHub and Jira
  today experience them as two integrations. Putting Boards under
  Repos would break that mental model for ADO customers.

### Shared

- **One Entra ID app registration per customer** with BOTH `vso.code`
  and `vso.work` API permissions granted at admin-consent time.
- **Same tenant/client tfvars** (`azure_devops_tenant_id`,
  `azure_devops_client_id`, `azure_devops_client_secret_ssm_param`).
- **Same `shared.entra_id_oauth` primitives** for token save /
  refresh / KMS wrapping.
- **Same `OAUTH_KMS_KEY_ARN`** for at-rest token encryption.

## Non-goals

- **Work-item comment posting from chat actions.** Boards is
  read-only in v1. `vso.work_write` upgrade path is a v2 plan when
  a customer surfaces the requirement.
- **Bidirectional sync.** Platform reads work items; customers
  update them in ADO. Same model as Jira today.
- **Custom-field expansion.** v1 ingests a fixed set of 16 standard
  fields (Title, State, WorkItemType, AssignedTo, Description, Repro
  Steps, Acceptance Criteria, Priority, Severity, Tags, Area,
  Iteration, Created, Changed, Reason, Id). Custom-field ingest is a
  v2 knob per customer-specific field mapping.
- **Historical backfill of closed items pre-connect.** The initial
  WIQL sync filters to open items only within the
  `INITIAL_SYNC_LOOKBACK_DAYS` window (default 90). Closed items
  that predate the connection are never ingested. Once an ingested
  work item closes, its last-known state stays in the KB forever
  (never purged). Customers wanting deep historical backfill get a
  one-shot operator script (v2 or on-demand).
- **Per-team filtering.** Boards syncs whole projects in v1. The
  `list_teams` endpoint on the API client is exposed for forward
  compatibility but not consumed by v1's sync worker.
- **Attachments.** Work-item attachments (binary blobs) are not
  ingested. Same non-goal as the Jira integration handles binary
  attachments only optionally.

## Consequences

- **Cross-region parity per CLAUDE.md HA rule.** Every primary-region
  Boards resource has a secondary counterpart. IAM changes must land
  on both sides simultaneously.
- **Double OAuth consent for customers using both.** A workspace
  admin who connects Repos and Boards goes through Microsoft's OAuth
  screen twice — once per integration — but their tenant admin only
  registers ONE Entra ID app. The runbook (`docs/runbooks/azure-devops-setup.md`
  §12) explains this trade-off.
- **License-cost implication.** A customer with `contoso/widgets`
  consented for both Repos (one or more repos) and Boards (the
  project) counts:
  - One `azure_devops_boards:contoso/widgets` unit
  - One `azure_devops:contoso/widgets/repo-name` unit per Repos repo
  Both count against the customer's source limit. This is the
  intended pricing model per ADR-019 -- Repos and Boards are
  separately-consented knowledge surfaces with independent risk
  profiles for the customer's compliance review.
- **Runbook complexity.** Customer onboarding is one more section
  (`docs/runbooks/azure-devops-setup.md` §12), but reuses the app-
  registration steps (§3-§5) rather than duplicating them.

## Related ADRs

- **ADR-007** — Documentation-Driven Decisions. This ADR captures a
  design choice (sibling-not-child) that would otherwise need to be
  re-derived every time a future contributor asked "should we bundle
  another ADO surface into Repos?".
- **ADR-019** — License layer + per-source counting. The
  `azure_devops_boards:` namespace registration in
  `lambda/shared/license_repos.py` is how this ADR intersects with
  ADR-019's counting model.
- **ADR-030 v1.1 / commit 389c1e7** — Both-MRK-ARN KMS grant
  pattern. Boards' IAM policies use the same pattern for
  future-proof HA.
- **ADR-036** — ADO Repos integration. Boards is the sibling.

## References

- `lambda/azure-devops-boards-integration/handler.py` — 10 endpoints
  (authorize, callback, connections, orgs, org-projects, projects
  add/list/update/delete, sync trigger, disconnect).
- `lambda/azure-devops-boards-integration/token_manager.py` —
  explicit `vso.work` scope, `AZDO_BOARDS_TOKEN#` prefix.
- `lambda/azure-devops-boards-integration/azure_devops_work_items_api.py` —
  `list_projects`, `list_teams`, `run_wiql`, `build_incremental_wiql`,
  `get_work_items`, `get_work_item_comments`,
  `STANDARD_WORK_ITEM_FIELDS`.
- `lambda/azure-devops-boards-sync/handler.py` — per-project WIQL
  worker, 200-ID batches, watermark advance.
- `lambda/azure-devops-boards-scheduler/handler.py` — hourly
  EventBridge fan-out.
- `lambda/file-ingestion/handler.py::process_azure_devops_boards_work_item` —
  HTML document serialization + S3 write under
  `workspaces/{ws}/azure_devops_boards/{org}/{project}/WI-{id}.html`.
- `lambda/shared/license_repos.py::_azure_devops_boards_project_ids` —
  license-snapshot identifier builder.
- `terraform/lambda-azure-devops-boards.tf` — primary Lambda triple +
  SQS + EventBridge.
- `terraform/lambda-azure-devops-boards-secondary.tf` — secondary
  region duplication.
- `docs/runbooks/azure-devops-setup.md` §12 — customer setup for
  adding Boards to an existing Repos integration.

Version History:

- v1.0 (2026-07-04): Initial decision. All nine plan chunks shipped.
