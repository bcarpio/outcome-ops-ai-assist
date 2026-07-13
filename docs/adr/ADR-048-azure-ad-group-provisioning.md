# ADR-048: Azure AD group-driven workspace provisioning

## Status: Accepted

- v1.0 (2026-07-10) — OIDC `groups` claim → group→workspace bindings →
  on-login reconciliation with offboarding revocation. Shipped + live-verified.
- v1.1 (2026-07-12) — group selection UX: tfvars **pattern allow-list** for
  eligibility/visibility; **explicit per-group** bindings chosen from a
  pattern-filtered picker (no wildcard bindings); a **mixed** name/object-ID
  matcher for hybrid tenants; an **optional, toggleable** app-only Graph read
  (env-SSM) that also subsumes the >200-group fallback.
- v1.2 (2026-07-13) — the app-only Graph read **reuses the existing OIDC login
  App Registration by default** (add `Group.Read.All` Application perm + admin
  consent to it); client id / tenant come from the `oidc_*` tfvars and the
  secret from the derived house path `/<env>/<app>/oidc/client-secret` — no
  second app and no SSM path in tfvars. `azure_ad_graph_client_id` /
  `azure_ad_graph_secret_ssm_path` become optional overrides for a dedicated
  read-only app. See §4 "Which app registration."

## Context

Workspace membership was 100% hand-written DynamoDB rows created through the
workspace-management admin endpoints (`PK=USER#<email>`,
`SK=WS_MEMBER#<workspace_id>`, `role` in `workspace_admin | member`). There was
no identity-provider-driven path: an offboarded employee kept workspace access
until a human deleted their row. For the Azure-AD/Microsoft-shop ICP, "we
removed them in Entra but they still had access" is exactly what a regulated
buyer's security team flags.

The stated contract (CLAUDE.md) is Azure AD for identity. The ALB already
authenticates the UI via OIDC; the OIDC token can carry a `groups` claim.

Two facts about real Entra tenants shape the group-selection design (v1.1):

- **The `groups` claim carries group object IDs (GUIDs), not names** — except
  for groups **synced from on-premises AD** (via Entra Connect), where the app
  can be configured to emit the on-prem `sAMAccountName` instead. Cloud-only
  groups always emit as GUIDs. A hybrid enterprise is therefore **mixed**: one
  token's claim can contain both names and GUIDs.
- **OutcomeOps admins are not assumed to be Entra-privileged.** Neither
  workspace admins nor org admins can be assumed to hold directory-read rights.
  That rules out *delegated* Graph (a delegated call as a non-privileged admin
  403s). The only ways to put a human-readable group in front of an admin are
  an *app-only* Graph credential or no Graph at all.

## Decision

**Workspace access is provisioned and de-provisioned from Azure AD group
membership, reconciled on login, without a provisioning endpoint (no SCIM).
Groups are bound to workspaces explicitly, one at a time, from a picker scoped
by an org-wide naming convention.**

1. **OIDC `groups` claim.** The ALB listener rules keep the standard OIDC
   `scope = "openid email profile"`. **`groups` is NOT an OAuth scope in Azure
   AD** — requesting it (`scope = "... groups"`) makes Entra reject the whole
   auth flow with `AADSTS650053: scope 'groups' ... doesn't exist on the
   resource` (Microsoft Graph). Instead the claim is emitted via the app's
   **Token configuration → `groupMembershipClaims`**, and the ALB passes it
   through in `x-amzn-oidc-data` regardless of scope. Recommended enterprise
   config is `groupMembershipClaims = ApplicationGroup` (emit only groups
   **assigned to the OutcomeOps app**) rather than `SecurityGroup`/`All` — this
   curates the claim to the intended set and largely retires the >200-group
   overage. For hybrid tenants, also configure the optional `groups` claim to
   emit `sam_account_name` so synced groups arrive as names (enabling the
   credential-free path). Operator prerequisite (deployment guide).

2. **Eligibility via a tfvars pattern allow-list.** A configurable list of
   case-insensitive glob patterns (`azure_ad_group_patterns`, e.g.
   `["outcomeops-*", "*-outcomeops-*"]`) is the org-wide scope of which groups
   are *eligible and visible* for binding. This is the OutcomeOps-side analog of
   the Okta `aws-*` group-push convention; it is deliberately a list of globs so
   region/division-first naming (`emea-outcomeops-eng`) is expressible. The
   patterns are **not load-bearing for correctness** — the on-login match is
   identifier-to-binding — they scope (a) which token values are treated as
   "ours" for name-matching, (b) the Graph fallback query, and (c) what the
   admin's picker shows.

3. **Bindings are explicit and per-group — no wildcard bindings.** An admin sees
   the pattern-filtered group list and **picks the specific group(s)** to bind
   to their workspace, each at a role. A wildcard only ever *filters what is
   offered*; it is never *stored* as a binding. This keeps "who can access this
   workspace" a concrete, auditable list with no "a future group might silently
   match" surprise — the governance posture is the point. Binding row is
   unchanged from v1.0: `PK=GROUP#<gid>`, `SK=WORKSPACE#<wid>`, GSI1 mirror
   (`GSI1PK=WS#<wid>`, `GSI1SK=GROUP#<gid>`); the stored identifier is the object
   ID when known, or the on-prem name for synced-name-only groups.

4. **Picker population tracks the deployment's posture.**
   - **Credential-free** (no Graph): the picker offers the groups in the
     **admin's own current token claim**, filtered by the patterns — which
     covers the normal case, because the admin binding a group is almost always
     a member of it (a manager binding their own `outcomeops-myteam-admins`).
     Paste-a-group-id remains the gap-filler for binding a group the admin is
     not in and that has not been observed.
   - **Graph fallback on**: the picker calls app-only Graph with the pattern
     filter (`$filter=startswith(displayName, …)`, `ConsistencyLevel: eventual`)
     and offers the **complete** eligible list by name, whether or not the admin
     is a member. The query is bounded by the pattern and auditable; the
     permission (`Group.Read.All`, app-only) is a standing read grant the
     customer's security team consents to in their own tenant. Secret in SSM
     SecureString, per the env-SSM integration pattern.
   - **Which app registration.** By default the app-only Graph read reuses the
     deployment's **existing OIDC login App Registration** — the customer adds
     the `Group.Read.All` (Application) permission + admin consent to that same
     app. A single registration legitimately carries both the delegated perms
     that back the ALB sign-in and this one application perm; there is no reason
     to stand up a second app for a single read scope. Consequently the picker's
     client id / tenant are the `oidc_*` values already in tfvars, and its secret
     is the login app's own secret at the **derived** house path
     `/<env>/<app>/oidc/client-secret` — an SSM path is **never** placed in
     tfvars for the common case (matches `ui-fargate.tf`). A security team that
     insists on separating the user-facing login credential from the app-only
     Graph reader may override with a dedicated `Group.Read.All`-only app via
     `azure_ad_graph_client_id` (+ optionally `azure_ad_graph_secret_ssm_path`
     for a distinct secret); the overrides win when set, otherwise the login app
     is used. Either way the workspace-management Lambda's `ssm:GetParameter`
     grant is scoped to exactly the effective secret ARN.

5. **Mixed name/object-ID matcher (login-time sync).** For each value in the
   user's `groups` claim: if it is a name matching the allow-list, match it
   against name-keyed bindings; if it is a GUID, match it against ID-keyed
   bindings, resolving display only when Graph is available. This is built in
   from the start so a single deploy handles hybrid tenants without
   reconfiguration.

6. **On-login reconciliation.** The Fargate proxy, at most once per user per
   hour (matching the ALB OIDC token-refresh cadence), fires a fire-and-forget
   `POST /sync-memberships` carrying the user's current group identifiers. The
   workspace-management Lambda reads the bindings for those groups + the user's
   existing `WS_MEMBER#` rows, computes a delta
   (`shared.group_workspace_sync.compute_delta`), and applies it:
   - grants tagged `source = "group:<gid>"` are added for newly-eligible
     workspaces (and re-written on role drift);
   - group-owned rows whose backing group left the claim (offboarding) or whose
     binding was removed are revoked.

7. **Ownership invariant (load-bearing).** The sync OWNS only rows tagged
   `source = "group:<gid>"`. **Manual** memberships -- rows with no `source`
   (legacy) or `source = "manual"` -- are NEVER written or revoked by the sync.
   Admin `add_member` stamps `source = "manual"` so the boundary holds. There is
   exactly one `WS_MEMBER#` row per (user, workspace); where a manual row already
   grants a workspace the sync defers to it (no duplicate, no downgrade).

8. **Roles + bootstrapping.** Roles use the live vocabulary
   `workspace_admin | member`; when several of a user's groups bind the same
   workspace, the highest role wins. There is no separate "owner" role — the
   **designated `workspace_admin` is the de facto owner**. Bootstrapping is by
   delegation: an org admin creates a workspace and **manually** adds the
   Director/Manager/Team-Lead as `workspace_admin` (the `source=manual` path);
   that owner — who is a member of the relevant groups — then binds them from
   their own token claim. So neither org admins nor owners need to be
   Entra-privileged, and the credential-free posture is fully self-service.

9. **Independent toggles.** `azure_ad_group_sync_enabled` (master),
   `azure_ad_group_name_claim_enabled` (expect names in the token → name path),
   `azure_ad_group_graph_fallback_enabled` (allow the app-only Graph read).
   These yield four honest postures from one build: names-only (credential-free),
   names+Graph (mixed default), Graph-only (cloud-only shop), neither
   (air-gapped, paste-only). The Graph fallback also resolves the >200-group
   overage (below).

10. **>200-group overage.** When a user is in >200 groups Azure AD emits
    `_claim_names` + `hasGroups` INSTEAD of `groups`. The proxy detects this and
    **skips the sync** for that request (syncing with an empty group list would
    wrongly revoke every group-derived membership). When the Graph fallback is
    enabled, the same app-only credential resolves the true list via
    `GET /users/{id}/memberOf`; when it is not, >200-group users keep their
    memberships (no false revocation) and simply are not re-synced. Recommended
    mitigation is `ApplicationGroup` scoping (§1), which keeps the claim small.

## Consequences

- Offboarding in Entra propagates to workspace access on the user's next login
  (within the hourly token-refresh window) with no human action -- the
  governance win.
- Manual grants and IdP-driven grants coexist safely via the `source` tag.
- The naming convention removes the "someone maintains a catalog" burden: teams
  self-service create prefixed groups and they appear automatically (by name, or
  by the live pattern-scoped Graph query). Nobody hand-maintains a group list
  inside OutcomeOps.
- The system is **credential-free by default**: a customer emitting on-prem
  names never grants OutcomeOps any Graph permission. The Graph read is opt-in
  for cloud-only coverage and completeness of the picker.
- Bindings stay an explicit, auditable per-group list — no stored wildcard can
  broaden access as new groups are created.
- No SCIM: enterprise IdPs vary and a provisioning endpoint carries operational
  burden; the group-claim covers the offboarding requirement. SCIM can come
  later behind a flag if a customer needs full lifecycle provisioning.
- HA parity: when HA returns, the OIDC scope stays `openid email profile` on the
  secondary listener rules (the `groups` claim rides `groupMembershipClaims`, not
  scope); the derived rows already dual-write; the Graph SSM secret must be
  replicated per the secrets-parity rule.

## Alternatives considered

- **SCIM provisioning endpoint.** Rejected for now -- operational burden and
  IdP variance; group-claim + on-login reconcile covers offboarding.
- **Delegated Graph (call as the signed-in admin).** Rejected -- OutcomeOps
  admins are not assumed Entra-privileged, so a delegated directory read 403s
  for them. App-only or no-Graph are the only viable models.
- **OutcomeOps-maintained group catalog (id→label table).** Rejected as the
  default -- it is constant maintenance as the directory changes. The naming
  convention + live matching achieves the same picker without a kept list.
- **Wildcard / pattern bindings** (`outcomeops-team-*` → role). Rejected --
  powerful but makes a workspace's access list non-obvious and lets a
  newly-created group silently gain access. Patterns scope *visibility*;
  bindings stay explicit.
- **Native Entra wildcard group-push** (Okta `aws-*` analog). Not available --
  Entra app-assignment is explicit per group. The nearest equivalent is an
  identity-team script that assigns `outcomeops-*` groups to the app; that lives
  on the customer's side, not in OutcomeOps.
- **Sync every request (no cache).** Rejected -- needless load; the ALB reissues
  the OIDC data hourly.
- **Revoke on empty groups during the >200 overage.** Rejected -- it would
  mass-revoke exactly the users with the most group memberships.
