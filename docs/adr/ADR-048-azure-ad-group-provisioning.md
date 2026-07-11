# ADR-048: Azure AD group-driven workspace provisioning

## Status: Accepted

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

## Decision

**Workspace access is provisioned and de-provisioned from Azure AD group
membership, reconciled on login, without a provisioning endpoint (no SCIM).**

1. **OIDC `groups` claim.** Both ALB listener rules request
   `scope = "openid email profile groups"`. Azure AD emits `groups` only when
   the enterprise app sets `groupMembershipClaims = SecurityGroup` (or `All`) --
   an operator prerequisite (deployment guide).

2. **Group → workspace bindings.** An admin binds an Entra group to a workspace
   at a role. Stored in the workspaces table as a new item type:
   `PK=GROUP#<gid>`, `SK=WORKSPACE#<wid>`, with a GSI1 mirror
   (`GSI1PK=WS#<wid>`, `GSI1SK=GROUP#<gid>`) so bindings are queryable both by
   group (the on-login sync) and by workspace (the admin UI). No new table.

3. **On-login reconciliation.** The Fargate proxy, at most once per user per
   hour (matching the ALB OIDC token-refresh cadence), fires a fire-and-forget
   `POST /sync-memberships` carrying the user's current group ids. The
   workspace-management Lambda reads the bindings for those groups + the user's
   existing `WS_MEMBER#` rows, computes a delta
   (`shared.group_workspace_sync.compute_delta`), and applies it:
   - grants tagged `source = "group:<gid>"` are added for newly-eligible
     workspaces (and re-written on role drift);
   - group-owned rows whose backing group left the claim (offboarding) or whose
     binding was removed are revoked.

4. **Ownership invariant (load-bearing).** The sync OWNS only rows tagged
   `source = "group:<gid>"`. **Manual** memberships -- rows with no `source`
   (legacy) or `source = "manual"` -- are NEVER written or revoked by the sync.
   Admin `add_member` now stamps `source = "manual"` so the boundary holds
   going forward. There is exactly one `WS_MEMBER#` row per (user, workspace),
   so where a manual row already grants a workspace the sync defers to it (no
   duplicate, no downgrade).

5. **Roles use the live vocabulary** `workspace_admin | member` (not
   viewer/editor/admin). When several of a user's groups bind the same
   workspace, the highest role wins.

6. **>200-group overage.** When a user is in >200 groups Azure AD emits
   `_claim_names` + `hasGroups` INSTEAD of `groups`. The proxy detects this and
   **skips the sync** for that request (syncing with an empty group list would
   wrongly revoke every group-derived membership). The Graph
   `GET /me/memberOf` fallback that resolves the true list is a fast-follow;
   until it ships, >200-group users keep their memberships (no false
   revocation), they just are not re-synced.

## Consequences

- Offboarding in Entra propagates to workspace access on the user's next login
  (within the hourly token-refresh window) with no human action -- the
  governance win.
- Manual grants and IdP-driven grants coexist safely via the `source` tag.
- No SCIM: enterprise IdPs vary and a provisioning endpoint carries operational
  burden; the group-claim covers the offboarding requirement. SCIM can come
  later behind a flag if a customer needs full lifecycle provisioning.
- HA parity: when HA returns, the `groups` OIDC scope must be mirrored on the
  secondary listener rules; the derived rows already dual-write.

## Alternatives considered

- **SCIM provisioning endpoint.** Rejected for now -- operational burden and
  IdP variance; group-claim + on-login reconcile covers offboarding.
- **Sync every request (no cache).** Rejected -- needless load; the ALB
  reissues the OIDC data hourly, so an hourly per-user reconcile is sufficient.
- **Revoke on empty groups even during the >200 overage.** Rejected -- it would
  mass-revoke exactly the users with the most group memberships. Skip-until-
  Graph-fallback is the safe behavior.
