# Azure DevOps integration — operator setup runbook

The platform connects to Azure DevOps via Microsoft Entra ID OAuth.
This runbook walks the customer's Entra ID administrator through the
one-time setup, the handoff to the platform operator, and the
verification flow. Time budget: ~30 minutes for an admin who has
done one Entra ID app registration before.

## 1. What this integration does

The platform reads source code from the Azure DevOps repos a connected
user has access to. Specifically it:

- Lists the orgs / projects / repos the connecting user can see, so
  workspace admins can pick which repos to ingest.
- Fetches repo content from default-branch tip on every sync to build
  the platform's code-map for each repo.
- Posts pull-request-check results back as PR thread comments when
  triggered by your CI pipeline or by the platform's UI.

Explicitly NOT in v1, documented here so SecOps review doesn't ask:

- Azure DevOps Server (on-prem TFS). Cloud-hosted `dev.azure.com/{org}`
  only.
- ADO Service Hooks (webhooks). The platform polls hourly + accepts
  manual triggers; it does not subscribe to ADO push events.
- Azure Pipelines integration. The platform is a code-knowledge
  surface, not a CI dashboard; the PR-check endpoint is the boundary.
- ADO Wiki / Test Plans. Separate integration paths.
  (Boards ships as a sibling integration to Repos; see §12 below.)

See [ADR-036](../adr/ADR-036-azure-devops-integration.md) for the
architectural rationale on each of these.

## 2. Prerequisites

- **Azure tenant admin access** — you need to register an app and
  grant admin consent in the customer's Entra ID directory.
- **Azure DevOps organization access** — to validate the connection
  end-to-end you need read access to at least one project + repo in
  the org you want the platform to see.
- **Platform operator coordination** — the operator will need the
  tenant ID, client ID, redirect URI, and client secret from you. They
  populate `tfvars` and create the SSM SecureString that holds the
  secret value.

## 3. Register an Entra ID application

1. Sign in to the **Microsoft Entra admin center**
   (`https://entra.microsoft.com`) with an account that has
   `Application Administrator` or `Cloud Application Administrator`
   role in the tenant.
2. Navigate to **Applications → App registrations → New registration**.
3. **Name**: `OutcomeOps AI Assist - Azure DevOps` (the exact name
   doesn't matter; this is how you'll find it later).
4. **Supported account types**:
   `Accounts in this organizational directory only (... - Single tenant)`.
   **Do NOT pick multi-tenant.** The integration is registered in your
   tenant and serves your users only.
5. **Redirect URI**:
   - Platform: **Web**
   - Value: `https://{platform-domain}/api/azure-devops/callback`

   Replace `{platform-domain}` with the FQDN of your platform deploy
   (your operator can confirm this; it's the URL users hit to access
   the platform UI). The path is exactly `/api/azure-devops/callback`
   — no trailing slash, no query string.

   **This is the most common cause of setup failure.** Entra ID
   does an exact-match validation on this URL during the OAuth
   callback. A trailing slash, an `http://` instead of `https://`,
   or a typo in the host all produce a `redirect_uri mismatch`
   error that returns the user to a generic error page.

6. Click **Register**.

7. On the resulting Overview page, copy these two values into your
   handoff notes:
   - **Application (client) ID** — a UUID
   - **Directory (tenant) ID** — a UUID

## 4. Grant the Azure DevOps API permission

The platform calls Azure DevOps REST APIs on the connected user's
behalf. This requires the `user_impersonation` delegated permission
on the Azure DevOps resource (NOT Microsoft Graph — they're separate
permission surfaces).

1. In your app registration, navigate to **API permissions →
   Add a permission**.
2. Click the **APIs my organization uses** tab.
3. Search for `Azure DevOps`. You're looking for the entry with the
   exact name **Azure DevOps** and resource ID
   `499b84ac-1321-427f-aa17-267ca6975798`. This is Microsoft's
   well-known resource ID; it's the same value across every Entra ID
   tenant.

   If the "Azure DevOps" entry doesn't appear: someone in your tenant
   needs to sign in to Azure DevOps once for the service principal to
   provision in your tenant. A one-time visit to
   `https://dev.azure.com/{any-org-your-tenant-has}` from any account
   in your tenant resolves this.

4. Select **Delegated permissions** → check **user_impersonation** →
   click **Add permissions**.
5. Back on the API permissions list, click **Grant admin consent for
   {tenant name}** and confirm. The status column for
   `user_impersonation` should now read **Granted for {tenant name}**.

   Without admin consent, users hitting the platform's "Connect Azure
   DevOps" flow will see a consent prompt they cannot satisfy
   themselves — they'd need to ask an admin to approve, and that's a
   per-user friction the integration is designed to avoid.

## 5. Create a client secret

1. Navigate to **Certificates & secrets → Client secrets → New client
   secret**.
2. **Description**: `OutcomeOps AI Assist`.
3. **Expires**: pick a value per your tenant's secret-rotation policy.
   Default Entra options are 6, 12, or 24 months. The platform reads
   the secret from SSM at request time; if the secret expires the
   integration stops working until you generate + hand off a new one,
   so longer = fewer interruptions, shorter = more secure. A 24-month
   secret with a calendar reminder at month 22 is a reasonable
   default.
4. Click **Add**.
5. **Immediately copy the Value column** — not the Secret ID. Entra
   ID only shows the value once on this page; reload the page and
   it's gone forever. You'd have to delete the secret and generate a
   new one.
6. Hand the value off to your platform operator via a secure channel:
   a shared 1Password / Bitwarden item, a sealed-secrets workflow,
   etc. **Never email or Slack-DM** the secret value.

## 6. Hand off to the platform operator

The operator needs four values from you:

| What | From step | Goes into |
|---|---|---|
| Tenant ID | 3.7 | `azure_devops_tenant_id` in tfvars |
| Client ID | 3.7 | `azure_devops_client_id` in tfvars |
| Redirect URI | 3.5 | `azure_devops_redirect_uri` in tfvars |
| Client secret value | 5.5 | SSM SecureString (see below) |

The operator populates the tfvars in the appropriate `tfvars` file
(`prd.tfvars` for production or the deploy's environment-specific
file; never committed to git per CLAUDE.md). They also set:

```hcl
azure_devops_integration_enabled = true
azure_devops_client_secret_ssm_param = "/prd/outcome-ops-ai-assist/azure-devops/client-secret"
```

The SSM parameter name is the path that holds the client secret; the
operator creates it manually (terraform does NOT create or manage the
secret value):

```bash
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/azure-devops/client-secret" \
  --value "<the secret value you handed off>" \
  --type SecureString \
  --key-id alias/aws/ssm \
  --region us-west-2
```

The operator then applies terraform via the normal CI/CD pipeline.
Per CLAUDE.md, terraform is never applied locally.

## 7. Verify the connection

Once the operator confirms the deploy succeeded:

1. Open the platform UI as a user who has **workspace_admin** role on
   the workspace you want to connect Azure DevOps to.
2. Navigate to **Workspace Settings → Integrations**.
3. Click **Connect Azure DevOps**.
4. The browser opens Microsoft's consent screen for the Entra ID app
   you registered in step 3. The user accepts; admin consent (granted
   in step 4) means no second consent prompt.
5. The browser returns to **Workspace Settings → Integrations** with
   a green confirmation banner showing the connected user's display
   name and email.
6. Click **Add Repositories**. The picker walks: orgs → projects →
   repos. Pick one repo and click **Add**.
7. The repo appears in the workspace's repo list with sync status
   **pending → queued → in_progress → success** (the transitions take
   ~30s for a small repo; up to several minutes for large monorepos).

   If the sync stays in `pending` for more than 5 minutes, click
   **Sync Now** to trigger immediately, or check the
   `azure-devops-sync-dlq` SQS queue for failed messages.

## 8. Customer ADO Pipeline integration for PR-check (optional)

If your team uses Azure Pipelines and wants PR-check results to post
automatically on every PR build, add this step to your
`azure-pipelines.yml`. The step invokes the platform's `analyze-pr`
Lambda directly via the AWS CLI (the CLI ships pre-installed on
Microsoft-hosted `ubuntu-latest` / `windows-latest` agents).

```yaml
- task: AWSShellScript@1
  displayName: 'Trigger OutcomeOps PR-check'
  condition: eq(variables['Build.Reason'], 'PullRequest')
  inputs:
    awsCredentials: 'OutcomeOps-PRCheck'   # AWS service connection
    regionName: 'us-west-2'                # your platform's aws_region
    scriptType: 'inline'
    inlineScript: |
      # Parse ADO's Build.Repository.ID into org/project/repo shape.
      # ADO ships $(System.TeamProject) + $(Build.Repository.Name) as
      # project + repo; the org is your Azure DevOps organization slug.
      PAYLOAD=$(jq -n \
        --argjson pr_number "$(System.PullRequest.PullRequestId)" \
        --arg org  "$(OUTCOMEOPS_ADO_ORG)" \
        --arg proj "$(System.TeamProject)" \
        --arg repo "$(Build.Repository.Name)" \
        --arg conn "$(OUTCOMEOPS_CONNECTION_ID)" \
        '{
          pr_number: $pr_number,
          repository: ($org + "/" + $proj + "/" + $repo),
          provider: "azure_devops",
          connection_id: $conn,
          ado_org: $org,
          ado_project: $proj,
          ado_repo: $repo
        }')
      aws lambda invoke \
        --function-name prd-outcome-ops-ai-assist-analyze-pr \
        --payload "$PAYLOAD" \
        --cli-binary-format raw-in-base64-out \
        /tmp/response.json > /dev/null
      cat /tmp/response.json | jq .
```

Pipeline prerequisites (one-time setup):

- **`OutcomeOps-PRCheck` AWS service connection** in your Azure DevOps
  project (Project Settings → Service connections → New → AWS). Uses
  an IAM user (or role) in the platform operator's AWS account with
  `lambda:InvokeFunction` on the `*-analyze-pr` function ARN. The
  operator provisions this following the platform's existing API-token
  process.
- **AWS Toolkit for Azure DevOps** extension installed in the
  organization (adds the `AWSShellScript@1` task). Free from the
  Marketplace.

Pipeline variables to define (Pipelines → Library → Variable group):

- `OUTCOMEOPS_ADO_ORG` — your Azure DevOps organization slug (the
  `{org}` in `dev.azure.com/{org}`). Not sensitive.
- `OUTCOMEOPS_CONNECTION_ID` — the Azure DevOps connection ID
  visible in Workspace Settings → Integrations after step 7. Treat
  it as opaque; it's not sensitive (no auth value) but rotating it
  is a manual replace-everywhere operation.

Alternate trigger: from any machine with AWS CLI + credentials to
the platform account (e.g. a developer's terminal), run:

```bash
scripts/outcome-ops-assist analyze-pr-ado <pr_number> <org>/<project>/<repo> \
  --connection-id <connection_id> --env prd
```

Same payload, same Lambda, human-driven instead of pipeline-driven.

The PR-check Lambda posts a comment thread on the named PR with the
check results. Comments are authored by the user who created the
connection in step 7 (audit attribution flows through Entra ID
delegation, so the comment is logged as "{connected user} via
OutcomeOps AI Assist").

**Check parity with GitHub.** All six check types (`ADR_COMPLIANCE`,
`README_FRESHNESS`, `TEST_COVERAGE`, `BREAKING_CHANGES`,
`ARCHITECTURAL_DUPLICATION`, `LICENSE_COMPLIANCE`) run real
analysis against Azure DevOps source — same behavior as the
GitHub integration. The pipeline fetches PR metadata + file diffs
+ commit list via the ADO REST API, invokes Bedrock Claude for
analysis, and posts the result to your PR thread. `LICENSE_COMPLIANCE`'s
"AI-generated code" detection (Phase 2 — inspects the PR's commit
list for AI-tool co-author markers like `Co-Authored-By: Claude`)
runs on both providers; the ADO code path transparently refetches
individual commits when the list endpoint truncates the message
body so trailer detection still works.

UI-driven manual triggers work without any pipeline changes — workspace
members open a PR's check page in the UI and click "Run check."

## 9. Troubleshooting

| Error | Cause | Fix |
|---|---|---|
| `OAuth callback failed: redirect_uri mismatch` | Step 3.5 redirect URI doesn't match exactly. | Re-check the Entra app registration's redirect URI. Match scheme (https), host, and path character-for-character. No trailing slash. |
| `Approval required` / `Need admin approval` consent screen | Admin consent (step 4.5) was not granted. | Go back to API permissions and click Grant admin consent. |
| `invalid_resource` from Microsoft | Wrong API was added in step 4. | Re-check API permissions — should be "Azure DevOps", NOT "Microsoft Graph". |
| `Connection lost: reconnect_required` | The user who connected has not used the integration in ~90 days; Entra ID expired the refresh token. | The same user (or any other workspace admin) clicks "Reconnect Azure DevOps" in Workspace Settings. |
| `Pull request comment not posting` | The connected user doesn't have PR-comment permission in the target repo, OR the `vso.code_write` sub-scope wasn't granted under `.default` at admin consent time. | Verify the user has Contributor on the project, then have an Azure DevOps PCA re-run admin consent. |
| `Repo missing from picker` | The connected user lacks read access to that repo in ADO. | Grant the user read on the repo in ADO; the picker will see it on the next refresh. |
| `Client secret authentication failed` | The secret expired, or was pasted with leading/trailing whitespace into SSM. | Regenerate per step 5 and re-run the SSM `put-parameter` command. |

## 10. Disconnecting

1. Workspace admin: **Workspace Settings → Integrations →
   Disconnect Azure DevOps**.
2. Confirm. The platform:
   - Removes every repo from the workspace that was added through
     this connection (the repo's KB documents are also cleaned up;
     this is irreversible).
   - Deletes the refresh + access tokens.
   - Removes the connection record.

To rotate the connecting user (e.g., the original connector left
the company): disconnect, then any other workspace admin clicks
**Connect Azure DevOps** and walks the OAuth flow under their own
Entra ID identity. Workspace repos must be re-added against the new
connection (the platform doesn't carry repos across connections by
design — repos are tied to the connecting user's token).

## 11. Rotating the client secret (recurring)

The Entra app's client secret has an expiration date (step 5.3).
Calendar a reminder at ~30 days before expiration:

1. Repeat step 5 to generate a NEW secret. Don't delete the old one
   yet.
2. Hand the new value to the platform operator.
3. Operator runs the SSM `put-parameter --overwrite` command to
   replace the value. The integration Lambda picks up the new value
   on its next cold start (which happens within ~15 minutes of
   normal traffic) — no terraform apply, no Lambda redeploy.
4. Confirm the integration still works (open the connection picker,
   confirm orgs load).
5. **Then** delete the old secret in the Entra app to revoke it.

Don't delete the old secret before confirming the new one works.
Worst case during rotation: the SSM update happens between two
Lambda invocations and you have a few seconds of "the old secret is
SSM but the Lambda's cached copy is the older older one" — harmless
because both are currently valid.

## 12. Enabling Boards on an existing Repos integration

The Azure DevOps Boards integration ingests **work items** (User
Stories, Bugs, Tasks, Epics, Features) into the platform's knowledge
base as searchable context — the ADO analog of the Jira integration.
It's a **separate integration** from Repos with independent OAuth
consent, an independent workspace-settings tab, an independent
disconnect flow, and its own license-layer counting bucket
([ADR-039](../adr/ADR-039-azure-devops-boards-integration.md)).

Boards reuses the Entra ID app you already registered for Repos
(§3), the client secret you already put in SSM (§5), and the tfvars
you already populated (§6). Two new steps: add the `vso.work` API
permission to the existing app, and register a second redirect URI.

Time budget: ~15 minutes for an admin who has completed the Repos
setup.

### 12.1 Add the `vso.work` API permission

Go to the Azure DevOps Boards work-items scope of the app registration:

1. Azure Portal → **Entra ID** → **App registrations** → your existing
   OutcomeOps AI Assist app (the one you registered in §3).
2. **API permissions** → **Add a permission** → **APIs my organization
   uses** → search for **Azure DevOps** → click it.
3. Choose **Delegated permissions** → tick **vso.work** (Read your
   organization's work items and their metadata) → **Add permissions**.
4. Back on the API permissions list, click **Grant admin consent for
   {your tenant}** → confirm.

If your customer's Boards workflow requires the platform to post
comments back to work items (v2 feature, not yet in v1), also tick
**vso.work_write**. Not required for read-only ingest, which is what
v1 ships.

### 12.2 Add the second redirect URI

Entra ID validates redirect URIs literally; the Boards OAuth callback
lands on a different path than Repos.

1. Same app → **Authentication** → **Redirect URIs** → **Add URI**.
2. Enter `https://{your-app-fqdn}/api/azure-devops-boards/callback`
   (mirrors the Repos redirect you added in §3, but with
   `-boards` in the path).
3. **Save**.

### 12.3 Update tfvars

In `terraform/prd.tfvars` (or your equivalent):

```hcl
azure_devops_boards_integration_enabled = true
```

That's the only new line. Tenant / client-id / client-secret-SSM-param
are reused from the Repos block. The redirect URI env var the Lambda
consumes is derived from your deploy's FQDN in the terraform module —
you don't type it in tfvars. The value the customer registers in
Entra ID (step 12.2 above) has to match exactly:
`https://{your-app-fqdn}/api/azure-devops-boards/callback`.

### 12.4 Deploy

```bash
cd terraform
terraform apply -var-file=prd.tfvars
```

The apply creates:
- `{env}-{app}-azure-devops-boards-integration` Lambda + Function URL
- `{env}-{app}-azure-devops-boards-sync` Lambda + SQS queue + DLQ
- `{env}-{app}-azure-devops-boards-scheduler` Lambda + EventBridge rule
  (`rate(1 hour)`)
- Identical resources under `aws.secondary` if `secondary_region` is
  set (HA mode)

The UI's Integrations tab now shows an "Azure DevOps (Boards)"
section next to the "Azure DevOps (Repos)" section.

### 12.5 Connect a workspace

1. Log into the platform → navigate to any workspace → **Settings** →
   **Integrations**.
2. Under "Azure DevOps (Boards)", click **Connect Azure DevOps Boards**.
3. Complete the OAuth consent screen (Microsoft asks for `vso.work`
   only, so the consent copy is scoped correctly — customers who
   flagged the Repos consent as "too broad" typically approve this
   one without pushback).
4. Back on the settings tab, the connection shows with **Add projects**.
5. Click **Add projects**, pick 1+ projects from the dropdown, save.
6. The initial WIQL sync fires immediately (`triggered_by=connect` in
   CloudWatch); subsequent syncs run hourly via the scheduler.

Work items become retrievable in chat within one scheduler tick
(typically <1 hour, less if the workspace admin clicked the per-
project **Sync** button).

### 12.6 What syncs vs. doesn't

- **Only open work items are re-synced each cycle.** Filter is
  `[System.State] NOT IN ('Closed', 'Done', 'Removed', 'Resolved')`.
- **Once ingested, closed items stay in the KB indefinitely.** A
  bug you fix today stays retrievable ("what was the fix for X?").
  History grows over time.
- **Work items closed *before* Boards was connected are not
  ingested.** No retroactive backfill in v1.
- **Initial sync is capped at the 5000 most recently changed work
  items per project.** Larger projects require an operator-run
  backfill script (out of scope for this runbook).
- **v1 field set is fixed:** Title, State, WorkItemType, AssignedTo,
  Description, Repro Steps, Acceptance Criteria, Priority, Severity,
  Tags, Area, Iteration, Created, Changed, Reason, Id. Custom fields
  are a v2 feature.
- **Comments are included inline** on each work item's document (not
  as separate documents), matching the Jira pattern.
- **Attachments are not ingested** in v1.

### 12.7 Troubleshooting

**"AADSTS65001: The user or administrator has not consented" on
connect.** The `vso.work` permission wasn't granted admin consent in
step 12.1. Go back to Azure Portal, grant admin consent, retry.

**"AADSTS500011: The resource principal named 499b84ac-... was not
found."** The Entra ID app doesn't have the Azure DevOps API
permission at all. Same fix as above — add it, grant consent.

**Connection shows `auth_error` with "reconnect_required".** The
stored refresh token was revoked (user password change, admin
revoke, or 90-day inactivity). Disconnect and reconnect from the UI.

**Initial sync completes but no work items appear in chat.** Wait
5-10 minutes for embedding + indexing. If still empty after that,
check CloudWatch logs for
`/aws/lambda/{env}-{app}-azure-devops-boards-sync` — look for
`[boards-sync] WIQL for {org}/{project}` and confirm the WIQL query
returned IDs.

**All work items sync but comments are missing on some.** ADO's
comments API is best-effort: a transient failure ingests the work
item without comments. The next sync retries and picks them up. Not
a bug; expected under intermittent ADO API load.

**Disconnect leaves work-item documents in the KB.** By design.
Disconnecting removes the connection + tokens but preserves the
already-ingested documents until an explicit workspace-level
cleanup. Reconnecting the same connection resumes the ingest without
re-uploading unchanged content.

## References

- Architectural decisions for this integration:
  [ADR-036 — Azure DevOps source-code integration](../adr/ADR-036-azure-devops-integration.md)
- License layer counting (ADO repos count toward customer repo limit;
  Boards projects count as a separate `azure_devops_boards:` identifier):
  ADR-019
- Multi-region active/passive (the integration is duplicated under
  `aws.secondary` so failover preserves access): ADR-022
