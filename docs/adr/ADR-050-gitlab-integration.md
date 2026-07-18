# ADR-050: GitLab code-hosting integration

## Status: Accepted

- v1.0 (2026-07-18) — Add GitLab as a third code-hosting provider alongside
  GitHub and Azure DevOps (ADO), toggled per-env by `enable_gitlab_integration`.
  SaaS-first (gitlab.com). Mirrors the ADO design (ADR-036): OAuth auth, S3-first
  ingest, scheduled (no webhooks). Foundation (shared OAuth + API client + this
  decision record) is chunk 01; the Lambda triad, read_source dispatch, UI, and
  HA parity follow.

## Context

The platform already supports GitHub (GitHub App installation tokens) and Azure
DevOps (Entra ID OAuth). GitLab is the third code host. `shared/source_fetch.py`
already reserves a `gitlab` provider branch using an S3 mirror at
`workspaces/{workspace_id}/gitlab/{slug}/{path}` ("gitlab-sync follows the ADO
pattern -- write to S3 first"). GitLab (gitlab.com and self-managed) is common in
regulated enterprises, so it must be a first-class provider, not a bolt-on.

The two existing code hosts differ in auth posture, so "which one to mirror" is a
real decision, not a copy-paste. ADO is the closer template: it is the most recent
"add a second code host" precedent and is OAuth-based like GitLab.

## Decision

1. **SaaS-first; instance base URL configurable later.** The first cut hardcodes
   `https://gitlab.com`. A later change makes the base URL a tfvar for BOTH
   GitLab (self-managed) and GitHub (GHES), since regulated customers self-host
   both. Not conflated into the initial integration.

2. **Auth = Model A, delegated OAuth (per-user tokens).** A GitLab admin registers
   ONE group-owned OAuth application for the deployment (`client_id` in tfvars,
   secret in operator-managed SSM, redirect URI = the integration Function URL
   callback). Each end user performs a one-click OAuth **authorize** (never
   creates an app), minting a **user-scoped** access+refresh token. Consequences:
   "a user can only enroll repos they already have access to" is automatic and
   free (no per-user membership plumbing), and every connection carries per-user
   attribution.
   - **Rejected: Model B, a single org-level group access token.** It would allow
     admin-once setup with no per-user connect, but the token has flat org-wide
     visibility — per-user access restriction would require mapping each workspace
     user to a GitLab identity and checking project membership. Model A gets the
     governance property (per-user scoping + attribution) for free, and matches
     the ADO template. The one-time per-user authorize click is the same cost ADO
     already imposes.

3. **Token storage reuses the generic Entra OAuth primitives; the GitLab OAuth
   HTTP is its own.** `shared/entra_id_oauth.py` exposes provider-agnostic,
   `pk_prefix`-keyed, KMS-encrypted DDB storage (`save_tokens`,
   `get_refresh_token`, `delete_tokens`); GitLab reuses these with
   `pk_prefix="GITLAB_TOKEN#"`. It CANNOT reuse Entra's token *exchange/refresh*
   (hardcoded to `login.microsoftonline.com`), so `shared/oauth/gitlab.py`
   implements GitLab's own `build_authorize_url`, `exchange_code_for_tokens`,
   `refresh_access_token`, and `get_user_profile` against gitlab.com's
   `/oauth/authorize`, `/oauth/token`, and `/api/v4/user` — HTTP shape modeled on
   the self-contained non-Microsoft module `shared/oauth/salesforce.py`, but
   delegating persistence to the shared storage rather than duplicating KMS/DDB.

4. **S3-first ingest (ADO pattern), not live-fetch (GitHub pattern).** `gitlab-sync`
   mirrors source to `workspaces/{workspace_id}/gitlab/{slug}/{path}` via
   `dual_write.s3_put_object` (metadata `source:"gitlab"`), which the
   S3-triggered `process-documents` indexes for chat retrieval, then fans
   application repos out to `REPO_SUMMARIES_QUEUE` (`provider:"gitlab"`) for
   code-map + symbol-graph generation.

5. **No inbound webhooks.** Ingest is scheduled (hourly EventBridge → scan
   `GITLAB_REPO#` rows → SQS → sync worker), matching GitHub/ADO. MR checks (later
   chunk) are triggered by the UI or the customer's CI POSTing with a `provider`
   field — not webhooks.

6. **Provider dispatch = DDB `*_REPO#` lookup, NOT slash-count inference.** GitHub
   (`owner/name`, 1 slash) and ADO (`org/project/repo`, 2 slashes) disambiguate by
   slash count today. GitLab supports **nested subgroups**, so a slug is
   `group/{...subgroups}/project` with a VARIABLE number of slashes (≥2) — it can
   collide with ADO's fixed 3-segment shape, breaking slash-count inference.
   Therefore `read_source` (and analyze-pr) resolve the provider by looking up the
   enrolled `*_REPO#` rows for the workspace (GitHub install → ADO repo → GitLab
   repo), honoring an explicit `provider` hint when a caller supplies one. Slash
   count in tool *descriptions* remains an advisory hint for the model only. This
   supersedes shape inference for GitLab.

7. **DDB key conventions** (`PK = WS#{workspace_id}`): `GITLAB_CONNECTION#{connection_id}`,
   `GITLAB_TOKEN#{connection_id}` (KMS-encrypted refresh token), and
   `GITLAB_REPO#{group}/{...subgroups}/{project}` — each `GITLAB_REPO#` row carries
   its own `connection_id` (a workspace can span multiple GitLab connections) and
   `provider:"gitlab"`. The slug is the URL-decoded full project path; the GitLab
   API v4 client URL-encodes it (`group%2Fsub%2Fproject`) for `/projects/{id}`.

8. **Raw `urllib` API client, no SDK** (ADR-036 Decision 5). `shared/gitlab_api.py`
   lists groups/projects, resolves default branch + latest commit SHA, walks the
   repository tree, and reads file content.

## Consequences

- Per-user access scoping and attribution come for free from Model A; the cost is
  a one-time per-user authorize click (identical to ADO).
- The subgroup slug breaks two long-standing assumptions — fixed-segment slugs and
  slash-count platform inference — so lookup-based dispatch (Decision 6) is
  load-bearing and must land before read_source/analyze-pr touch GitLab.
- GitLab is additive and gated; with `enable_gitlab_integration=false` (default)
  the footprint is zero and existing integrations are untouched.
- Self-managed GitLab is deferred to the configurable-base-URL change (Decision 1).

## Alternatives considered

- **Mirror GitHub (App-install / org-level token).** Rejected for the base auth:
  GitLab's OAuth-app equivalent to a GitHub App is a group access token (Model B),
  which loses per-user scoping (Decision 2).
- **Slash-count platform inference (as GitHub/ADO do).** Rejected: nested
  subgroups make slash count ambiguous (Decision 6).
- **A fully self-contained OAuth module (Salesforce style).** Rejected the
  duplication: Entra's storage primitives are already provider-agnostic, so GitLab
  reuses them and owns only the provider-specific HTTP (Decision 3).
