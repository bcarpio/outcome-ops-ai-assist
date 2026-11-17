# ADR-044: Salesforce OAuth uses one shared Connected App per deploy

## Status: Accepted

## Context

We initially built ServiceNow with per-workspace OAuth credentials
because ServiceNow has no shared vendor developer console -- each
customer's ServiceNow instance is its own OAuth authority and every
Application Registry entry lives on that instance. We later collapsed
that back to env-SSM once we realized single-instance-per-deploy is the
common case and the deploy-in-customer-account model makes env-SSM
strictly simpler. See the git history + memory
[[feedback_no_retrieveit_patterns]] for the reasoning.

**Salesforce is different.** Connected Apps support an OAuth Policies
setting "Available for use by all users" (also known as multi-tenant
authorization). One Connected App registered on any Salesforce org can
authorize users signing in from any other Salesforce org. This makes
Salesforce OAuth structurally identical to Confluence (Atlassian
Cloud) or Microsoft 365 (multi-tenant Entra ID app registrations):

- One Connected App per **deployment** (not per customer, not per
  workspace).
- `consumer_key` in tfvars, `consumer_secret` in SSM SecureString.
- Any workspace admin can connect any Salesforce org (their own prod,
  a sandbox, a dev org, an acquired-company org) against that one app.
- `instance_url` is discovered per-connection at OAuth callback time
  (from the token response body's `instance_url` field) and stored on
  the connection row.
- Sandbox vs prod authorize host is chosen per-connection at connect
  time via an `is_sandbox` flag: `test.salesforce.com` vs
  `login.salesforce.com`. Persisted on the connection row so the
  refresh path picks the correct token endpoint.

## Decision

Salesforce OAuth uses the **env-SSM shared-app pattern** used by every
other OAuth integration in OutcomeOps *except* ServiceNow. Concretely:

- Env vars on the integration + sync + file-ingestion Lambdas:
  - `SALESFORCE_CLIENT_ID` (from tfvar `salesforce_oauth_client_id`)
  - `SALESFORCE_CLIENT_SECRET_SSM_PARAM` (path to SSM SecureString)
- SSM SecureString at `/{env}/{app}/oauth/salesforce/client-secret`
  with `PLACEHOLDER_CONFIGURE_ME` value + `lifecycle.ignore_changes`.
  Operator populates the real value via `aws ssm put-parameter --overwrite`.
- Ha-list-secrets + ha-sync-secrets replicate the secret to the
  secondary region so the failover-region Lambdas can also decrypt it.
- Per-workspace connection row (`WS#{ws}` /
  `SALESFORCE_CONNECTION#{conn}`) stores `instance_url`, `is_sandbox`,
  `salesforce_org_id`, `salesforce_user_id`, `identity_url`,
  `last_sync_status`, `config.cursors`. No client_id / secret at this
  level.
- Per-connection token row (`SALESFORCE_TOKEN#{conn}` / `CREDENTIALS`)
  stores KMS-encrypted refresh token; access tokens are minted per
  invocation and never persisted.

## Consequences

**Same UI flow as Confluence/Jira with one extra pick step.** Connect
click opens an env picker (radio: Production vs Sandbox), Continue
POSTs to `/connect` with `is_sandbox`, then GET `/authorize` returns
the OAuth URL. This is one extra dialog compared to Confluence's
single-click Connect but is unavoidable -- Salesforce requires the
authorize host to match the target environment.

**No per-tenant registry churn.** Operator sets up the Connected App
once at deploy bring-up. Adding a new workspace requires zero deploy
work.

**HA parity is simple.** Same env-SSM shape as every other integration.
`kms_oauth` grants both regions' `oauth_tokens` CMKs so a token minted
against the primary key stays decryptable after failover routes the
user to the secondary Lambda. Documented as the general
[[project_ha_toggle_orphans_kms_tokens]] pattern -- Salesforce inherits
it for free.

## Alternatives considered

**(A) Per-workspace Connected App registration (mimic ServiceNow's
initial per-workspace design).** Rejected because Salesforce doesn't
require it -- shared Connected Apps are a first-class Salesforce
feature, unlike ServiceNow where there's no equivalent. Adopting this
would recreate the ServiceNow refactor pain for zero benefit.

**(B) One Connected App per customer's AWS account, but registered on
the customer's Salesforce org (partial per-tenant).** Rejected because
it forces every OutcomeOps deploy operator into the customer's
Salesforce admin flow (per-org app registration + secret coordination).
Same anti-pattern that made the ServiceNow per-workspace shape
untenable.

**(C) Machine-to-machine JWT bearer flow (Salesforce Connected App
without user OAuth).** Rejected because SOQL runs as the connected
user, applying field-level security + sharing rules automatically.
JWT bearer with a service user would either grant too much (a super-
user profile that sees everything) or require the customer to build
custom per-record sharing for the service user. Interactive OAuth
keeps the security model aligned with how the customer already thinks
about who sees what.

## References

- `../app.retrieveit.ai/lambda/salesforce-integration/` for the
  shape reference the port derived from. Note: RetrieveIT is SaaS but
  used the same shared-Connected-App pattern for the same reasons --
  Salesforce doesn't need per-tenant here.
- [[feedback_no_retrieveit_patterns]] documenting when RetrieveIT
  patterns do vs don't transfer.
- [[project_deploy_model_not_saas]] for the OutcomeOps
  deploy-in-customer-account context.
