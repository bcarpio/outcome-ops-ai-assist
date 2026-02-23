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

## Verification

1. Access the UI URL from the Terraform output (`ui_url`)
2. You should be redirected to the Azure AD login page
3. After login, the chat UI loads
4. Check Fargate logs for `[proxy] User: user@yourdomain.com` entries

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
