# ADR-044: Per-Workspace OAuth Credentials for ServiceNow

## Status: Accepted

## Context

Every other integration OutcomeOps ships (Confluence, Jira, Outlook, Teams,
SharePoint, OneDrive, OneNote, Azure DevOps, Azure DevOps Boards, GitHub App,
Database) uses **one OAuth app per deployment**. The `client_id` and
`client_secret` live in env-level SSM (e.g.
`/{env}/{app}/oauth/atlassian/client-secret`), the deploy operator sets them
once at bring-up, and every customer workspace that connects that source
authorizes against the same app.

**ServiceNow does not fit this shape.** Each customer's ServiceNow instance
lives at their own subdomain (`https://<customer>.service-now.com`) and their
Application Registry OAuth apps live on that instance. There is no
"OutcomeOps ServiceNow OAuth app" that could be pre-registered per deployment
— the OAuth app has to exist on the customer's own instance.

Trying to shoehorn ServiceNow into the env-SSM pattern would force each
deploy operator to negotiate one shared OAuth app across every customer
tenant that ever uses their deploy — impossible without violating the
"customer owns their AWS account" architectural promise (see
[`project_deploy_model_not_saas`](../../.claude/…)-scoped memory) and
awkward even if it were possible (rotation, revocation, blast radius).

## Decision

For **ServiceNow only**, OAuth credentials are per-workspace:

- The UI collects `instance_url` + `client_id` + `client_secret` in a
  setup form BEFORE the OAuth authorize redirect.
- `instance_url` and `client_id` are stored in plaintext on the
  connection row (`WS#{ws} / SERVICENOW_CONNECTION#{conn}`); they are
  not secrets. `client_secret` is KMS-encrypted with the shared
  `oauth_tokens` CMK and stored on the token row
  (`SERVICENOW_TOKEN#{conn} / CREDENTIALS`) alongside the encrypted
  refresh token.
- The token manager reads credentials from DynamoDB at each refresh, NOT
  from module-init SSM. Setup submissions are idempotent per
  `(workspace_id, instance_url)`: a re-submit for the same instance
  updates the credentials in place while preserving the same
  `connection_id`.
- **No `/{env}/{app}/oauth/servicenow/*` SSM parameters exist.** No
  entries in `scripts/ha-list-secrets` or `scripts/ha-sync-secrets`
  for ServiceNow. Nothing at the env level to configure.

## Consequences

**UI flow deviation.** Every other integration surfaces a single
"Connect" button that opens the OAuth authorize URL directly. ServiceNow
surfaces a Connect button that opens a setup form; the OAuth redirect
happens only after the form is saved. `WorkspaceSettings.tsx` gets one
extra path per integration that follows the ServiceNow shape (if
future integrations follow the same customer-instance-per-tenant
pattern — SAP, some Salesforce edge cases, air-gapped Bitbucket
Server — they land on the same shape).

**HA parity is simpler.** No env-level SSM to replicate across regions.
The `oauth_tokens` CMK's cross-region replica already covers the token
row; nothing else needs mirroring for the secondary region to work.

**Multi-instance guard.** On OAuth callback the connection captures
`servicenow_user_id` + `servicenow_user_login` (from
`GET /api/now/table/sys_user?sysparm_query=user_name=javascript:gs.getUserName()`)
so a subsequent reconnect against a different user account can be
detected. This wasn't strictly required by the env-SSM model but becomes
worth it once each connection has its own OAuth app: a user reconnecting
against a different ServiceNow account on the same OutcomeOps workspace
would be a genuine identity change worth surfacing.

**No shared consent surface.** Some customers, especially the regulated
ones, prefer that vendors do NOT hold OAuth apps against their instance.
The per-workspace model surfaces the customer's own OAuth app on their
own instance's Application Registry — the customer's SSO admin can
audit, rotate, or revoke that app entirely from their side. This aligns
with [[feedback_enterprise_security]] (private-first for enterprise
security teams).

## Alternatives considered

**(A) One shared OAuth app per deployment, customer registers it on
their instance.** Rejected because it forces the OutcomeOps deploy
operator into the customer's ServiceNow admin flow (per-instance app
registration + secret coordination). Doesn't scale past a handful of
customers and defeats the deploy-into-customer-account posture entirely.

**(B) Machine-to-machine bearer tokens without OAuth.** ServiceNow's
implementation still ties long-lived bearer tokens to Application
Registry entries on the customer's instance, so this doesn't actually
avoid the per-tenant app registration problem; it just uses a
different auth grant. Rejected for no meaningful gain.

**(C) Basic auth against a service-account username + password.**
Rejected because it violates
[[feedback_enterprise_security]] — no credential distribution flows.
Also historically fragile against SSO-only ServiceNow deployments where
local service accounts aren't allowed.

## References

- Ported from RetrieveIT's ADR-003 (same architectural choice made
  under the SaaS variant).
- ADR-019 for the license-layer + repo-limit context that applies here
  identically (ServiceNow instance counts as one licensable "repo"
  identifier).
- [[project_deploy_model_not_saas]] for the reason a shared vendor
  OAuth app isn't an option in the OutcomeOps deploy model.
