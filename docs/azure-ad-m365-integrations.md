# Azure AD App Setup — M365 Integrations (SharePoint / Outlook / Teams / OneDrive)

This guide is for setting up the **Microsoft Graph OAuth app** used by the M365 source integrations (SharePoint, Outlook, Teams, OneDrive). It is **separate from the ALB OIDC login app** documented in [`azure-ad-setup.md`](./azure-ad-setup.md) — the two apps have different purposes and live as two distinct App Registrations.

| Concern | App | Documented in |
|---|---|---|
| Logging into the OutcomeOps UI (Azure AD as the corporate IdP) | ALB OIDC app | `azure-ad-setup.md` |
| Reading files / mail / messages from M365 sources (per-user OAuth consent) | Microsoft Graph app | **this doc** |

If you only set up the OIDC app, M365 integrations will fail at OAuth consent time with "AADSTS50011: Reply URL mismatch" or "AADSTS65001: The user or administrator has not consented to use the application".

## Architecture

```
[Workspace admin clicks "Connect"]
       |
       v
[UI redirects to login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize
   with this app's client_id + scope + redirect_uri]
       |
       v
[User consents to the requested scopes]
       |
       v
[Microsoft redirects back to https://{customer-fqdn}/api/{provider}/callback
   with an auth code]
       |
       v
[Express proxy forwards to the {provider}-integration Lambda]
       |
       v
[Lambda exchanges code -> tokens, stores refresh_token KMS-encrypted in DynamoDB]
       |
       v
[Per-hour scheduler triggers sync workers that refresh tokens + call Graph API]
```

The app uses **delegated permissions** (acts on behalf of the consenting user) — not application permissions. Each connection is scoped to one user's M365 access.

## Step 1: Create the Microsoft Graph App Registration

1. [Azure Portal](https://portal.azure.com) > **Microsoft Entra ID** > **App registrations** > **New registration**
2. Configure:
   - **Name**: `OutcomeOps AI Assist - M365 Integrations`
   - **Supported account types**: Choose based on your sales motion:
     - **Single tenant** if only one customer org will use this deployment (typical for BYOC)
     - **Multi-tenant** if multiple customer orgs will consent to the same app (not the typical OutcomeOps pattern)
   - **Redirect URI**: Leave blank — we add them in Step 4 after the deploy
3. Click **Register**
4. From the Overview page, note:
   - **Application (client) ID** → goes in `microsoft_oauth_client_id` (tfvars)
   - **Directory (tenant) ID** → only needed if you change `https://login.microsoftonline.com/common/...` to a tenant-specific URL

## Step 2: Add API Permissions (delegated)

In your App Registration > **API permissions** > **Add a permission** > **Microsoft Graph** > **Delegated permissions**:

| Scope | Used by | Why |
|---|---|---|
| `openid` | all | OAuth 2.0 OIDC discovery |
| `profile` | all | Returns user displayName etc. |
| `email` | all | Returns user email for connection record |
| `offline_access` | all | Required for refresh tokens (we don't make users re-consent every hour) |
| `User.Read` | SharePoint, OneDrive | Lets the Lambda hit `/me` for the connecting user's profile |
| `Mail.Read` | Outlook | Read mail messages |
| `Mail.ReadBasic` | Outlook | Read message metadata without bodies (lighter touch) |
| `Sites.Read.All` | SharePoint | List sites + read documents from site drives |
| `Files.Read` | SharePoint | Read individual files (older API path; required alongside Sites.Read.All) |
| `Files.Read.All` | OneDrive, Teams | Read all files the user has access to in their OneDrive + Teams channel files |
| `Team.ReadBasic.All` | Teams | List the user's teams |
| `Channel.ReadBasic.All` | Teams | List channels in a team |
| `ChannelMessage.Read.All` | Teams | Read channel message content |
| `Notes.Read.All` | OneNote | Read OneNote notebooks, sections, pages |

After adding all permissions, click **Grant admin consent for {tenant}** — this lets your tenant's users consent without an admin block.

## Step 3: Create Client Secret

1. App Registration > **Certificates & secrets** > **New client secret**
2. Description: `OutcomeOps AI Assist M365 integration`
3. Expiration: pick something you can rotate before — 24 months is reasonable; longer than that = forgotten secret = silent integration outage
4. **Copy the Value field immediately** — it's only shown once

## Step 4: Store Client Secret + Client ID in SSM

The Lambdas read both the client ID and client secret from SSM (the secret is KMS-encrypted at rest, the client ID is plain since it's not sensitive).

```bash
# Pick env: dev or prd
ENV=prd

aws ssm put-parameter \
  --name "/${ENV}/outcome-ops-ai-assist/oauth/microsoft/client-id" \
  --type String \
  --value "your-app-client-id-here" \
  --overwrite \
  --region us-west-2

aws ssm put-parameter \
  --name "/${ENV}/outcome-ops-ai-assist/oauth/microsoft/client-secret" \
  --type SecureString \
  --value "your-client-secret-value-here" \
  --overwrite \
  --region us-west-2
```

Also set the same client ID in tfvars (it's referenced for the auth-URL builder):

```hcl
# terraform/prd.tfvars
microsoft_oauth_client_id = "your-app-client-id-here"
```

The `client-secret` is **never** in tfvars — only SSM.

## Step 5: Run Terraform Apply

```bash
terraform apply -var-file=prd.tfvars
```

This provisions:

- 4 Lambda integration trios (one per source: SharePoint, Outlook, Teams, OneDrive) — each is an integration Lambda + sync Lambda + scheduler Lambda
- An SSM parameter per source holding its callback URL — Terraform sets these to `https://{your-customer-fqdn}/api/{provider}/callback`

You can read the callback URLs back to confirm what to register in Azure:

```bash
aws ssm get-parameters-by-path \
  --path "/prd/outcome-ops-ai-assist/oauth/microsoft" \
  --region us-west-2 \
  --query 'Parameters[?contains(Name, `callback-url`)].[Name,Value]' --output table
```

## Step 6: Add Redirect URIs in Azure

After Terraform creates the FQDN and callback URLs, add ALL of the M365 callbacks to the App Registration.

In your App Registration > **Authentication** > **Add a platform** > **Web** > paste each URL > **Configure**.

The full set you need to register (replace `{fqdn}` with your customer's domain, e.g. `outcomeops.example.com`):

```
https://{fqdn}/api/sharepoint/callback
https://{fqdn}/api/outlook/callback
https://{fqdn}/api/teams/callback
https://{fqdn}/api/onedrive/callback
https://{fqdn}/api/onenote/callback
```

Each integration's auth flow uses **only its own callback URL**. They don't share endpoints. If you forget to add one of these, the OAuth consent screen for that integration will throw `AADSTS50011: Reply URL mismatch`.

## Step 7: Verify

End-to-end smoke test (one integration is sufficient — they all share the same app):

1. Sign into the OutcomeOps UI as a **workspace admin**
2. Open a workspace's **Settings > Integrations** tab
3. Click **Connect** on (e.g.) the OneDrive card
4. You should be redirected to `login.microsoftonline.com/...` with the consent screen showing the scopes from Step 2
5. After consent you should land back on `https://{fqdn}/workspaces/{id}/settings?tab=integrations&configure_onedrive={connection_id}`
6. The connection card should now show your user email + a "Configure" button

If the user sees `AADSTS65001: The user or administrator has not consented...`, an admin needs to click **Grant admin consent** in Step 2.

## Multi-customer note (BYOC)

OutcomeOps AI Assist is BYOC (deploys into the customer's own AWS account). Each customer makes their own Azure AD app registration following the steps above. There is no shared SaaS app — the `microsoft_oauth_client_id` in each customer's tfvars points at their own App Registration.

For solo / pre-customer dev work, sharing one app across multiple deployments is fine. For each commercial customer, plan a dedicated app registration as part of their onboarding runbook.

## Permission-grant policies

If the customer's tenant requires admin approval for new app registrations (common in regulated industries), the workspace admin who first clicks **Connect** will see a "request admin approval" dialog instead of the normal consent screen. The Azure AD admin must approve before any user can connect M365 sources. This is unrelated to OutcomeOps — it's a tenant-level policy.

## Rotating the Client Secret

1. Create a new secret in Azure AD (Step 3)
2. Update SSM with the new value (using `--overwrite`)
3. Roll the integration Lambdas via env-var touch to clear in-memory cached config:

   ```bash
   for fn in sharepoint outlook teams onedrive; do
     aws lambda update-function-configuration \
       --function-name "prd-outcome-ops-ai-assist-${fn}-integration" \
       --region us-west-2 \
       --environment "Variables={$(aws lambda get-function-configuration --function-name "prd-outcome-ops-ai-assist-${fn}-integration" --region us-west-2 --query 'Environment.Variables' --output json | jq -r 'to_entries|map("\(.key)=\(.value|tostring)")|join(",")'),CACHE_BUST=$(date +%s)}"
   done
   ```

4. Delete the old secret in Azure AD after you confirm the new one works

The sync Lambdas pick up the new secret automatically — they re-read SSM at module-load time.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `AADSTS50011: Reply URL mismatch` | Callback URL not registered or has a typo | Verify Step 6. Each integration has its OWN callback URL. The callback the SSM param `/prd/outcome-ops-ai-assist/oauth/microsoft/{provider}-callback-url` resolves to is the authoritative one to register |
| `AADSTS65001: User or admin has not consented` | Tenant policy blocks user consent | Step 2 — click "Grant admin consent" |
| `AADSTS7000215: Invalid client secret` | SSM secret stale / rotated in Azure but not in SSM | Re-run Step 4's `put-parameter` with the current secret + roll the integration Lambda envs to clear cache |
| Connection works but no files sync | Tokens stored but sync hasn't fired yet | EventBridge runs hourly; click "Sync now" on the connection card OR wait for the cron |
| `auth_error` status on a connection after some time | Refresh token revoked (user signed out everywhere / admin revoked grants / tenant moved) | Workspace admin clicks Disconnect then Connect again on the integration card |

## Related

- ALB OIDC login app setup: [`azure-ad-setup.md`](./azure-ad-setup.md)
- ADR-019 (license layer) — the M365 sync Lambdas are all license-gated
- ADR-018 (audit logging) — every successful sync emits a `{SOURCE}_SYNCED` audit row
