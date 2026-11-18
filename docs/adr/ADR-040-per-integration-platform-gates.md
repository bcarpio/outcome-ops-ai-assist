# ADR-040: Per-integration platform-level gates

## Status: Accepted

## Context

Before this ADR, the platform's integration surface deployed on an all-
or-nothing basis: the single `enable_workspaces = true` tfvar
provisioned all 9 workspace integrations (GitHub, Confluence, Jira,
Outlook, Teams, SharePoint, OneDrive, OneNote, Database) plus the two
Azure DevOps integrations (Repos + Boards). Enabling any of them meant
enabling all of them.

Two flags predated this ADR:

- `enable_github_issue_integration` — provisions the code-generation
  API Gateway v2 (public HTTP endpoint that GitHub webhooks POST to).
- `enable_jira_integration` — provisioned the SSM Automation cross-
  account document + IAM role that Jira Automation assumes to trigger
  code generation.

For the workspace-level Azure DevOps integrations,
`enable_azure_devops_integration` and
`enable_azure_devops_boards_integration` gated a runtime env-var check
in the handler (`AZURE_DEVOPS_INTEGRATION_ENABLED = "false"` → 404 on
authorize / callback), but the underlying Lambdas + SQS + scheduler
were provisioned regardless.

This shape is a problem for two reasons:

1. **SecOps review flags unused public surface.** Customers using
   Azure DevOps who don't want a GitHub App integration still saw
   its Lambdas provisioned. Customers who don't use Jira Automation
   still saw its cross-account IAM role. The
   `enable_github_issue_integration = true` default provisioned a
   public HTTP API Gateway on every deploy, which SecOps teams
   consistently flagged as an unnecessary attack surface.

2. **Runtime env-var gating leaks operational cost.** The ADO Lambdas
   deployed + billed for their inactive state; the runtime check hid
   the integration in the UI but didn't hide it in AWS Cost Explorer.

## Decision

Every integration gains a platform-level `enable_<name>_integration`
bool tfvar, default `false`, that gates the ENTIRE terraform footprint
via `count`. When the flag is false, the integration's Lambdas + SQS
queues + DLQs + EventBridge rules + SSM parameters + KMS grants + IAM
policies are not provisioned. Zero AWS cost for a disabled integration.

### Naming

Two names live in the tfvars namespace with slightly different
meanings, kept as-is to avoid churning customer deploys:

- `enable_github_issue_integration` — gates the code-generation API
  Gateway v2 (public HTTP endpoint). NOT the workspace GitHub App
  integration.
- `enable_jira_integration` — widened by this ADR. Now gates BOTH the
  SSM Automation cross-account role AND the workspace-level Jira
  Lambdas. If a future customer needs the split, a new
  `enable_jira_automation_integration` flag can be introduced then.

All other flags follow `enable_<name>_integration`:

| Flag                                     | Gates                                            |
| ---                                      | ---                                              |
| `enable_azure_devops_integration`        | Workspace ADO Repos Lambdas + SQS + scheduler    |
| `enable_azure_devops_boards_integration` | Workspace ADO Boards Lambdas + SQS + scheduler   |
| `enable_github_integration`              | Workspace GitHub App Lambdas + SQS + scheduler + App-credential SSM params |
| `enable_github_issue_integration`        | Code-gen API Gateway v2 + Lambda trigger         |
| `enable_confluence_integration`          | Workspace Confluence Lambdas + SQS + scheduler + confluence-specific callback SSM |
| `enable_jira_integration`                | Workspace Jira Lambdas + Automation SSM + IAM role |
| `enable_outlook_integration`             | Workspace Outlook Lambdas + SQS + scheduler + outlook-specific callback SSM |
| `enable_teams_integration`               | Workspace Teams Lambdas + SQS + scheduler + teams-specific callback SSM |
| `enable_sharepoint_integration`          | Workspace SharePoint Lambdas + SQS + scheduler + sp-specific callback SSM |
| `enable_onedrive_integration`            | Workspace OneDrive Lambdas + SQS + scheduler + od-specific callback SSM |
| `enable_onenote_integration`             | Workspace OneNote Lambdas + SQS + scheduler + on-specific callback SSM |
| `enable_database_integration`            | Workspace Database Lambdas + SQS + scheduler + drift-notification SNS |

### Shared credential SSM parameters

Some SSM parameters are shared across integrations that use the same
OAuth app:

- `atlassian_client_id` + `atlassian_client_secret` (Confluence + Jira
  share the org's Atlassian OAuth app). Gated on
  `(enable_confluence_integration || enable_jira_integration)`.
- `microsoft_client_id` + `microsoft_client_secret` (Outlook + Teams +
  SharePoint + OneDrive + OneNote share the org's Microsoft OAuth app).
  Gated on 5-way OR of the corresponding flags.

Per-integration callback URL SSM parameters (`atlassian_callback_url`,
`atlassian_jira_callback_url`, `microsoft_callback_url`,
`microsoft_teams_callback_url`, etc.) are gated on the specific
integration's flag only.

### UI

The UI must know which integrations to show. A new
`GET /api/config/integrations` endpoint on the workspace-management
Lambda returns a manifest of enabled integrations. The Lambda reads
the manifest from `INTEGRATION_<NAME>_ENABLED` env vars set by
terraform.

The UI's `WorkspaceSettings` component fetches the manifest on mount
and wraps each integration section in
`{enabledIntegrations.<name> && ( ... )}`. Disabled sections are
omitted from the DOM entirely (not hidden with CSS).

Function URL env vars passed to the UI Fargate task are wrapped in
`try(aws_lambda_function_url.X[0].function_url, "")` so a disabled
integration's absent Function URL resolves to an empty string rather
than blowing up `terraform plan`.

### Example implementation

```hcl
# Primary-region resource pattern
module "github_integration_lambda" {
  count = var.enable_workspaces && var.enable_github_integration ? 1 : 0
  # ...
}

# Secondary-region resource pattern
module "github_integration_lambda_secondary" {
  count = var.enable_workspaces && var.enable_github_integration && var.secondary_region != "" ? 1 : 0
  # ...
}

# Shared OAuth SSM parameter (either Confluence OR Jira needs it)
resource "aws_ssm_parameter" "atlassian_client_id" {
  count = var.enable_workspaces && (var.enable_confluence_integration || var.enable_jira_integration) ? 1 : 0
  # ...
}

# UI Fargate consumer of a per-integration Function URL
{
  name  = "GITHUB_LAMBDA_URL"
  value = try(aws_lambda_function_url.github_integration[0].function_url, "")
}
```

## Consequences

### Positive

- SecOps review passes: unused integrations produce zero AWS
  footprint. A customer using only Azure DevOps + Database sees no
  GitHub / Confluence / Jira / M365 Lambdas or SQS queues in their
  account.
- Cost visibility: AWS Cost Explorer reports match customer intent.
- Bootstrap-safe: fresh deploys with default tfvars (all flags false)
  provision only the shared workspace-management + chat + KB Lambdas
  + KMS keys. Customers opt in to what they need.

### Tradeoffs

- **Migration cost for existing deploys.** Any customer with an
  existing deploy must add explicit `= true` lines to their tfvars for
  every integration they're currently using. Otherwise
  `terraform apply` destroys the resources. Mitigated for the
  repo-owner's `prd` sandbox by later phases which added the
  necessary lines. Customer deploys elsewhere require operator
  intervention on the flip.
- **Shared SSM parameter gate expressions get long.** The 5-way OR for
  `microsoft_client_id` is readable but not elegant. Acceptable
  because the number of shared params is small and the expressions are
  grep-able.

### When to reconsider

- If a future integration doesn't share OAuth credentials with any
  other, its `count` expression stays simple.
- If two integrations diverge (e.g., Confluence wants its own OAuth
  app separate from Jira), the shared-SSM OR expression can be split
  back into per-integration params.

## Implementation

Shipped over 11 commits in July 2026 (the per-
integration-gating plan). Chunk 12 lands this ADR + owner tfvars
sync + runbook updates.

## Related ADRs

- ADR-036: Azure DevOps integration
- ADR-039: Azure DevOps Boards integration

## References

- `terraform/lambda-<integration>.tf` — one file per integration,
  each demonstrating the `count = var.enable_workspaces &&
  var.enable_<name>_integration ? 1 : 0` pattern.
- `lambda/workspace-management/handler.py`
  `get_integrations_config_response` — the manifest endpoint.
- `ui/src/pages/WorkspaceSettings.tsx` — reads the manifest, hides
  disabled sections.

Version History:

- v1.0 (2026-07-06): Initial decision + 12-chunk implementation.

<!-- Confluence sync -->
