# Azure DevOps integration — operator setup runbook

> Status: **STUB.** Chunk 10 of the Azure DevOps integration plan fills
> in the content. This file exists now because chunks 02, 04, and 08
> reference it as a stable link target in error messages, the chat UI,
> and the connection-flow consent screen. Replacing this with the real
> runbook is a chunk-10 deliverable; until then the sections below
> describe what each will cover so a reader knows what's coming.

## Audience

The customer's Azure / Entra ID administrator. The setup involves
Azure portal click-ops; one-time per deploy.

## What you'll do

1. **Register an Entra ID application** in your Azure tenant (the
   application identity through which the platform talks to Azure
   DevOps on behalf of connected users).
2. **Configure the OAuth redirect URI** to point at the platform's
   `/api/azure-devops/callback` endpoint on your deploy's FQDN.
3. **Grant the Azure DevOps API permission** (`user_impersonation`
   under the Azure DevOps API surface — distinct from Microsoft
   Graph).
4. **Grant admin consent** for the permission so end users connecting
   later don't see a consent prompt they can't satisfy.
5. **Generate a client secret** and capture it for SSM.
6. **Populate the platform's tfvars** with tenant ID, client ID,
   redirect URI, and the SSM parameter name holding the secret.
7. **Verify the connection** by opening the Workspace Settings → Azure
   DevOps tab and walking the OAuth flow end-to-end.

## Troubleshooting

Common setup failures (chunk 10 will document each with the exact
error message + fix):

- Redirect URI mismatch (trailing slash, path typo, http vs https)
- Admin consent not granted (users see "approval required")
- Wrong API permission (Microsoft Graph instead of Azure DevOps)
- Client secret expired or pasted with whitespace
- tenant_id pointing at the wrong tenant (multi-tenant scenarios)
- SSM parameter not readable by the integration Lambda's IAM role

## What's deliberately NOT supported in v1

These are documented here so customer SecOps doesn't ask why during
review and assume they're oversights:

- **Azure DevOps Server (on-premises TFS)** — only the cloud-hosted
  `dev.azure.com/{org}` is supported. Different URL pattern + no
  Entra ID OAuth on-prem.
- **Service Hooks (ADO's webhooks)** — the integration uses
  pull-based sync via an hourly EventBridge scheduler (same model as
  the GitHub integration). PR-check results post via REST when the
  customer's pipeline (or a manual UI trigger) invokes the platform's
  PR-check endpoint.
- **Azure Pipelines / build artifact ingestion** — the platform is a
  code-knowledge surface, not a CI dashboard. The PR-check endpoint
  is the boundary.
- **Boards / Wiki / Test Plans** — separate integration paths; ADO
  Boards (Work Items) has its own planned integration that mirrors
  the Jira shape and ships as a sibling product.

## References

- ADR `docs/adr/NNNN-azure-devops-integration.md` (created when this
  runbook ships in full) captures the architectural choices: single-
  tenant Entra ID OAuth, `org/project/repo` namespace, pull-based
  sync, deferred-scope list.
