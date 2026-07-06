# Integration gating (operator reference)

Every workspace integration ships behind a platform-level `enable_...`
tfvar. Default is `false` — customers opt in to what they need. See
ADR-040 for rationale.

## Flags

| Flag                                      | Default | What provisions when true                                                                                          |
| ---                                       | ---     | ---                                                                                                                |
| `enable_azure_devops_integration`         | `false` | Workspace Azure DevOps (Repos) Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge                        |
| `enable_azure_devops_boards_integration`  | `false` | Workspace Azure DevOps (Boards / work items) Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge          |
| `enable_github_integration`               | `false` | Workspace GitHub App Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + App-credential SSM parameters  |
| `enable_github_issue_integration`         | `true`  | Code-generation API Gateway v2 (public HTTP endpoint that receives GitHub webhooks) + Lambda trigger permission    |
| `enable_confluence_integration`           | `false` | Workspace Confluence Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + confluence-specific callback SSM |
| `enable_jira_integration`                 | `false` | Workspace Jira Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + Jira-specific callback SSM + SSM Automation cross-account role + doc |
| `enable_outlook_integration`              | `false` | Workspace Outlook Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + outlook-specific callback SSM     |
| `enable_teams_integration`                | `false` | Workspace Teams Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + teams-specific callback SSM         |
| `enable_sharepoint_integration`           | `false` | Workspace SharePoint Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + sp-specific callback SSM       |
| `enable_onedrive_integration`             | `false` | Workspace OneDrive Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + od-specific callback SSM         |
| `enable_onenote_integration`              | `false` | Workspace OneNote Lambdas + Function URL + SQS + DLQ + scheduler + EventBridge + on-specific callback SSM          |
| `enable_database_integration`             | `false` | Workspace Database Lambdas (container images) + Function URL + SQS + DLQ + scheduler + EventBridge + drift SNS     |

Shared SSM parameters (Atlassian OAuth for Confluence + Jira; Microsoft
OAuth for the 5 M365 integrations) provision when ANY of the sharing
integrations is enabled.

## To enable an integration

1. Add `enable_<name>_integration = true` to your `tfvars` file.
2. If the integration requires OAuth credentials, populate the
   integration-specific tfvars (see the per-integration runbook —
   e.g., `docs/runbooks/azure-devops-setup.md`).
3. `terraform apply` via your normal CI/CD path (never locally per
   CLAUDE.md's `Local Development` section).
4. Verify: the workspace-management Lambda's
   `GET /api/config/integrations` manifest should return the flag as
   `true`. The UI's Workspace Settings → Integrations tab renders the
   integration's section once the manifest reload completes.

## To disable an integration

1. Set `enable_<name>_integration = false` (or remove the line — the
   default is false).
2. `terraform apply` — plan will show destruction of the integration's
   Lambdas, SQS queues, EventBridge rules, and SSM parameters. Shared
   OAuth parameters stay if another sharing integration is still
   enabled.
3. Verify: the manifest returns `false` and the UI section is hidden.

**Existing workspace connections are NOT auto-deleted** when the flag
flips off. The DynamoDB rows survive; users see stale connection state
in DBs until manually cleaned up. Reserve for a follow-up if this
becomes a real customer concern.

## To check current state on a live deploy

```bash
curl -sS https://{deploy-fqdn}/api/config/integrations | jq
```

Returns:

```json
{
  "azure_devops": true,
  "azure_devops_boards": true,
  "github": true,
  "github_issues": true,
  "confluence": false,
  "jira": true,
  "outlook": false,
  "teams": false,
  "sharepoint": false,
  "onedrive": false,
  "onenote": false,
  "database": true
}
```

## Related docs

- ADR-040 — rationale for per-integration gates.
- `docs/runbooks/azure-devops-setup.md` — how the customer's Entra ID
  admin registers the ADO OAuth app before enabling the flag.
- `docs/jira-integration-setup.md` — Jira Automation cross-account
  setup after enabling `enable_jira_integration`.
