# GitHub App Integration Setup

This guide configures a GitHub App for repository access in OutcomeOps AI Assist. The GitHub App enables workspace-scoped repository connections for code-map generation and document ingestion.

## Architecture

```
[User] --> [UI: Connect GitHub] --> [GitHub App Install Page]
                                           |
                                    installs on org/repos
                                           |
[GitHub] --> [Callback URL] --> [Lambda: github-integration]
                                           |
                                    stores installation in DynamoDB
                                           |
[User] --> [UI: Select Repos] --> [Lambda] --> [GitHub API]
                                           |
                                    queues sync jobs
                                           |
[Scheduler] --> [github-sync Lambda] --> [code-maps pipeline]
```

## Prerequisites

- `enable_workspaces = true` in your tfvars
- A GitHub account with permission to create GitHub Apps (org owner or personal account)
- Terraform applied with workspaces enabled

## Step 1: Create GitHub App

1. Go to [GitHub Developer Settings](https://github.com/settings/apps) (or org settings for organization-owned apps)
2. Click **New GitHub App**
3. Configure basic information:
   - **GitHub App name**: `OutcomeOps AI Assist` (must be unique across GitHub)
   - **Homepage URL**: `https://outcomeops.ai` (or your company URL)
   - **Description**: AI-powered code generation assistant

## Step 2: Configure Callback URLs

Set the OAuth callback URL based on your environment:

- **Callback URL**: `https://{your-fqdn}/api/github/callback`
  - Example: `https://outcomeops.outcomeops.ai/api/github/callback`

- **Setup URL** (optional): Same as callback URL
- **Webhook URL**: Leave blank (not required for this integration)
- Uncheck **Active** under Webhook if you don't need real-time events

## Step 3: Configure Permissions

Under **Repository permissions**, set:

| Permission | Access | Purpose |
|------------|--------|---------|
| Contents | Read-only | Read repository files for code-maps |
| Metadata | Read-only | List repositories and branches |

Under **Account permissions**: None required.

Under **Where can this GitHub App be installed?**:
- Select **Any account** for multi-tenant use
- Select **Only on this account** for single-org deployments

## Step 4: Generate Private Key

1. After creating the app, scroll to **Private keys**
2. Click **Generate a private key**
3. A `.pem` file downloads automatically
4. Keep this file secure - you'll need it for SSM configuration

## Step 5: Note App Credentials

From the App settings page, note:

| Field | Location | Example |
|-------|----------|---------|
| App ID | General > About | `123456` |
| App slug | URL slug from App settings page URL | `my-company-ai-assist` |
| Client ID | General > About | `Iv1.abc123def456` |
| Client secret | Client secrets > Generate | `abc123...` (generate one) |

The **App slug** is the URL-friendly name in your GitHub App's URL: `https://github.com/apps/{app-slug}`.

## Step 6: Store Credentials in SSM

Non-secret values (`app-id`, `app-slug`, `client-id`, `callback-url`) are managed by Terraform via tfvars. Only **secrets** need to be stored manually in SSM:

```bash
# Set environment (dev or prd)
ENV=prd
APP_NAME=outcome-ops-ai-assist

# Client Secret (encrypted)
aws ssm put-parameter \
  --name "/${ENV}/${APP_NAME}/github/client-secret" \
  --type SecureString \
  --value "YOUR_CLIENT_SECRET" \
  --overwrite

# Private Key (encrypted) - paste the entire PEM file content
aws ssm put-parameter \
  --name "/${ENV}/${APP_NAME}/github/private-key" \
  --type SecureString \
  --value "$(cat /path/to/your-app.private-key.pem)" \
  --overwrite
```

## Step 7: Apply Terraform

Ensure your tfvars has workspaces enabled:

```hcl
enable_workspaces = true
domain            = "yourdomain.com"      # For callback URL
github_app_slug   = "your-app-slug"       # URL slug from GitHub App settings
github_app_id     = "123456"              # App ID from GitHub App settings
github_client_id  = "Iv1.abc123def456"    # Client ID from GitHub App settings
```

Apply Terraform to deploy the GitHub integration Lambda and update the Fargate task:

```bash
terraform plan -var-file=prd.tfvars
terraform apply -var-file=prd.tfvars
```

## Step 8: Restart Fargate Task

After Terraform applies, force a new deployment to pick up the `GITHUB_LAMBDA_URL` environment variable:

```bash
aws ecs update-service \
  --cluster prd-outcome-ops-ai-assist-ui \
  --service prd-outcome-ops-ai-assist-ui-ui \
  --force-new-deployment
```

## Verification

1. Log into the OutcomeOps UI
2. Navigate to Workspace Settings
3. Click **Connect GitHub**
4. You should be redirected to GitHub to install the App
5. After installation, select repositories to add
6. Check that repos appear in the workspace settings

## Reusing an Existing GitHub App

If you have an existing GitHub App (e.g., from another deployment), you can reuse it:

1. Add the new callback URL to the existing App:
   - Go to App settings > General
   - Add additional callback URL under **Callback URL**

2. Copy credentials from the existing SSM parameters:
   ```bash
   # Source environment (an existing deployment of THIS app you're copying from)
   SRC_ENV=dev
   SRC_APP=outcome-ops-ai-assist

   # Target environment
   TGT_ENV=prd
   TGT_APP=outcome-ops-ai-assist

   # Copy secrets only (non-secrets are managed by tfvars)
   for param in private-key client-secret; do
     VALUE=$(aws ssm get-parameter \
       --name "/${SRC_ENV}/${SRC_APP}/github/${param}" \
       --with-decryption \
       --query 'Parameter.Value' --output text)
     aws ssm put-parameter \
       --name "/${TGT_ENV}/${TGT_APP}/github/${param}" \
       --type SecureString \
       --value "$VALUE" --overwrite
   done
   ```

## SSM Parameter Reference

| Parameter | Type | Source | Description |
|-----------|------|--------|-------------|
| `/{env}/{app}/github/app-id` | String | tfvars | GitHub App ID (numeric) |
| `/{env}/{app}/github/app-slug` | String | tfvars | GitHub App URL slug (from App settings page URL) |
| `/{env}/{app}/github/client-id` | String | tfvars | OAuth client ID (Iv1.xxx format) |
| `/{env}/{app}/github/client-secret` | SecureString | Manual | OAuth client secret |
| `/{env}/{app}/github/private-key` | SecureString | Manual | RSA private key (PEM format) |
| `/{env}/{app}/github/callback-url` | String | tfvars | OAuth callback URL (derived from `domain` variable) |

## Troubleshooting

| Issue | Cause | Fix |
|-------|-------|-----|
| "GitHub integration not enabled" | `GITHUB_LAMBDA_URL` not set | Restart Fargate task after Terraform apply |
| "GitHub App ID not configured" | SSM parameter not set | Run `aws ssm put-parameter` commands |
| "Bad credentials" from GitHub | Wrong App ID or expired key | Verify App ID matches, regenerate private key |
| Callback returns error | Callback URL mismatch | Add exact callback URL to GitHub App settings |
| No repositories shown | App not installed on org | Install app on the GitHub org/account |
| Permission denied on repo | Insufficient permissions | Check App has Contents:Read permission |

## Rotating Credentials

### Rotate Client Secret

1. Generate new client secret in GitHub App settings
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github/client-secret" \
     --type SecureString \
     --value "NEW_SECRET" \
     --overwrite
   ```
3. Restart Fargate task or wait for Lambda cold start
4. Delete old secret in GitHub

### Rotate Private Key

1. Generate new private key in GitHub App settings
2. Update SSM:
   ```bash
   aws ssm put-parameter \
     --name "/${ENV}/${APP_NAME}/github/private-key" \
     --type SecureString \
     --value "$(cat new-private-key.pem)" \
     --overwrite
   ```
3. Restart Fargate task or wait for Lambda cold start
4. Revoke old private key in GitHub

## Multi-Tenant Considerations

When deploying for multiple customers:

1. **Shared GitHub App**: All customers use the same GitHub App
   - Pros: Single app to maintain
   - Cons: All customers see the same app name during install

2. **Per-Customer GitHub App**: Each customer has their own GitHub App
   - Pros: Branded experience, isolated credentials
   - Cons: More apps to manage

For enterprise deployments, consider creating a GitHub App under the customer's organization for better branding and control.
