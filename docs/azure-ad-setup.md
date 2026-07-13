# Azure AD OIDC Authentication Setup

This guide configures Azure AD (Entra ID) as an OIDC identity provider for the OutcomeOps Chat UI. When enabled, users must authenticate through Azure AD before accessing the UI.

## Architecture

```
[User] --> [ALB :443 HTTPS] --> [Azure AD Login] --> [ALB OIDC callback]
                                                          |
                                                x-amzn-oidc-* headers
                                                          |
                                                [Fargate :3000] --> [Lambda]
```

The ALB handles the entire OIDC flow. After authentication, it forwards requests to Fargate with user identity headers (`x-amzn-oidc-data`, `x-amzn-oidc-identity`).

## Prerequisites

- A Route53 hosted zone for your domain in the same AWS account
- An Azure AD (Entra ID) tenant
- `deploy_ui = true` in your tfvars

## Step 1: Create Azure App Registration

1. Go to [Azure Portal](https://portal.azure.com) > **Microsoft Entra ID** > **App registrations**
2. Click **New registration**
3. Configure:
   - **Name**: `OutcomeOps AI Assist`
   - **Supported account types**: Accounts in this organizational directory only (Single tenant)
   - **Redirect URI**: Leave blank for now (added after Terraform apply)
4. Click **Register**
5. Note the **Application (client) ID** and **Directory (tenant) ID** from the overview page

## Step 2: Create Client Secret

1. In your App Registration, go to **Certificates & secrets**
2. Click **New client secret**
3. Set a description and expiration (recommended: 24 months)
4. Copy the secret **Value** immediately (it won't be shown again)

## Step 3: Store Client Secret in SSM

Store the secret in AWS SSM Parameter Store before running Terraform:

```bash
# For dev environment
aws ssm put-parameter \
  --name "/dev/outcome-ops-ai-assist/oidc/client-secret" \
  --type SecureString \
  --value "your-client-secret-value-here"

# For prd environment
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/oidc/client-secret" \
  --type SecureString \
  --value "your-client-secret-value-here"
```

## Step 4: Configure Terraform Variables

Update your tfvars file:

```hcl
domain         = "yourdomain.com"    # Must have a Route53 hosted zone
oidc_enabled   = true
oidc_client_id = "your-app-client-id"
oidc_tenant_id = "your-azure-tenant-id"
```

The FQDN is derived automatically using the `ui_subdomain` variable (default: `outcomeops`):
- **prd** workspace: `{ui_subdomain}.{domain}` (e.g., `outcomeops.example.com`)
- **dev** workspace: `{ui_subdomain}-dev.{domain}` (e.g., `outcomeops-dev.example.com`)

## Step 5: Run Terraform Apply

```bash
# Via CI/CD (recommended) or:
terraform plan -var-file=dev.tfvars
```

This creates:
- ACM certificate with DNS validation via Route53
- Route53 A record pointing to the ALB
- HTTPS (443) listener with OIDC authentication
- HTTP (80) listener that redirects to HTTPS

## Step 6: Add Redirect URI in Azure

After Terraform creates the ALB, add the redirect URI to your App Registration:

1. Go to your App Registration > **Authentication**
2. Click **Add a platform** > **Web**
3. Set the **Redirect URI** to your FQDN + `/oauth2/idpresponse`:
   ```
   https://outcomeops.example.com/oauth2/idpresponse
   ```
   (Use your actual FQDN from the Terraform output `ui_url`)
4. Click **Configure**

The path `/oauth2/idpresponse` is the ALB's built-in OIDC callback endpoint.

## Step 7: Configure API Permissions (Optional)

By default, the `openid`, `email`, and `profile` scopes are requested. These are typically granted by default. If not:

1. Go to **API permissions** > **Add a permission**
2. Select **Microsoft Graph** > **Delegated permissions**
3. Add: `openid`, `email`, `profile`
4. Click **Grant admin consent** if required by your organization

## Step 8: Group -> workspace provisioning (Optional, ADR-048)

Drive workspace membership from Entra security groups instead of per-user
invites. Members of a bound group get the workspace (at the role you set) on
their next login; removing them from the group revokes it.

### 8a. Emit the `groups` claim, scoped to the app

1. App Registration > **Token configuration** > **Add groups claim**.
2. Choose **Groups assigned to the application** (`ApplicationGroup`), not "All
   groups" — this curates the claim to the groups your identity team assigns to
   OutcomeOps and keeps users under the 200-group token limit.
3. Under **Enterprise applications** > OutcomeOps > **Users and groups**, assign
   the security groups you want available (e.g. your `outcomeops-*` set).

Do **not** add `groups` to the OIDC `scope` — it is not an OAuth scope in Azure
AD and Entra will reject sign-in with `AADSTS650053`. The claim is delivered by
the Token configuration above and the ALB forwards it in `x-amzn-oidc-data`; the
listener scope stays `openid email profile`.

### 8b. Name matching for on-prem-synced groups (credential-free)

If your groups are synced from on-prem AD via Entra Connect, emit their names so
OutcomeOps can match them without any Graph permission:

1. In the groups claim config (8a), set the emitted format to
   **sAMAccountName** (Entra emits names only for on-prem-synced groups;
   cloud-only groups always emit object IDs).
2. Set `azure_ad_group_name_claim_enabled = true` and
   `azure_ad_group_patterns = ["outcomeops-*"]` (your convention) in tfvars.

### 8c. Graph read for the full picker / cloud-only groups (Optional)

For a "search all eligible groups" picker, or for cloud-only groups, grant a
read-only Graph **application** permission **in your own tenant** (no vendor
credential). The default path **reuses the OIDC login App Registration you
already created in Step 1** — one registration legitimately holds both the
delegated sign-in perms and this one application read perm, so there is nothing
new to register and no new secret to manage:

1. On that **same** App Registration (client id = your `oidc_client_id`),
   **API permissions** > **Microsoft Graph** > **Application permissions** > add
   `Group.Read.All`, then **Grant admin consent**.
2. Nothing else. The picker reuses the app's existing client id, tenant, and the
   secret you already stored in Step 3 at
   `/<env>/outcome-ops-ai-assist/oidc/client-secret` — the terraform derives all
   three, so an SSM path never goes in tfvars.
3. In tfvars, a single toggle:
   ```hcl
   azure_ad_group_graph_fallback_enabled = true
   ```

**Dedicated read-only app (optional).** If your security team requires the
app-only Graph reader to be separate from the user-facing login credential,
register a second app with only `Group.Read.All` (admin-consented), store its
secret at a path of your choosing, and override:
```hcl
azure_ad_group_graph_fallback_enabled = true
azure_ad_graph_client_id       = "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
azure_ad_graph_secret_ssm_path = "/<env>/outcome-ops-ai-assist/azure/graph-secret"  # dedicated
```
When set, these overrides win; otherwise the login app above is used. Either way
the workspace-management Lambda's `ssm:GetParameter` grant is scoped to exactly
the effective secret ARN.

Without 8c, the picker is served from the admin's own token groups (filtered by
your patterns), plus a paste-by-object-ID fallback — no Graph permission needed.

Bindings are always explicit per-group (chosen in **Workspace > Members > Azure
AD Groups**); the patterns only scope which groups are offered.

## Verification

1. Access the UI URL from the Terraform output (`ui_url`)
2. You should be redirected to the Azure AD login page
3. After login, the chat UI loads
4. Check Fargate logs for `[proxy] User: user@yourdomain.com` entries
5. (Groups) In a workspace's **Members > Azure AD Groups**, bind a group; a
   member of it gets `source=group` membership on their next login.

## Disabling Authentication

Set `oidc_enabled = false` in tfvars and re-apply. The ALB reverts to HTTP-only without authentication. The `domain` variable can remain set (HTTPS will still work, just without the OIDC auth step).

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| `AADSTS50011: Reply URL mismatch` | Redirect URI not configured in Azure | Add `https://{fqdn}/oauth2/idpresponse` to App Registration |
| `Certificate pending validation` | DNS validation record not propagated | Wait a few minutes for Route53 propagation |
| `Parameter not found` error in Terraform | SSM secret not created | Run the `aws ssm put-parameter` command from Step 3 |
| `401 Unauthorized` after login | Client secret expired or wrong | Rotate secret in Azure and update SSM parameter |

## Rotating the Client Secret

1. Create a new secret in Azure AD
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/{env}/outcome-ops-ai-assist/oidc/client-secret" \
     --type SecureString \
     --value "new-secret-value" \
     --overwrite
   ```
3. Re-apply Terraform to pick up the new value
4. Delete the old secret in Azure AD
