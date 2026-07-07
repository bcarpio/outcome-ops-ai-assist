# ServiceNow Integration

OutcomeOps AI Assist's ServiceNow integration ingests **published Knowledge
Base articles + their attachments** from your ServiceNow instance so your
AI assistant can answer questions grounded in the KB you already maintain
for your service desk, not a hand-curated retrieval-only copy.

Once connected, chat can answer:

- "What's our password reset process for cloud-only accounts?"
- "Which KB article covers the office VPN client on macOS?"
- "How do we handle a P1 outage for the billing platform?"

with the actual KB copy pulled straight from your instance, cited by
article number.

> **Who is this doc for?**
>
> | If you're… | Jump to |
> |---|---|
> | The **operator** deploying OutcomeOps and turning ServiceNow on for the first time | [Deploying the integration](#deploying-the-integration-operator) |
> | A **workspace admin** connecting the deploy's ServiceNow instance to a workspace | [Connecting ServiceNow](#connecting-servicenow-workspace-admin) |
> | A **ServiceNow admin** registering the OAuth app | [Register the OAuth Application Registry entry](#step-1-register-the-oauth-application-registry-entry-servicenow-admin) |
> | A **security / compliance reviewer** evaluating what leaves your instance | [What gets ingested](#what-gets-ingested) and [Where secrets live](#where-secrets-live) |

## Scope

The integration is **Knowledge Base only** for MVP:

| Ingested | Not ingested |
|---|---|
| `kb_knowledge` records with `workflow_state=published` | `incident`, `sc_task`, `problem` and other ticket tables |
| Attachments on those articles (filtered to a file-type allowlist) | CMDB (`cmdb_ci_*`) |
| `sys_updated_on` cursor for delta-only re-syncs on scheduler ticks | Draft / review KB articles |
| Article metadata (number, short description, KB, category, sys id) | Article ratings, comments, versions history |

## Model: one OAuth app per deploy

Unlike a SaaS multi-tenant vendor, OutcomeOps deploys into your own AWS
account. Because ServiceNow has no shared vendor developer console (there
is no `developer.servicenow.com` where a vendor could pre-register a
"OutcomeOps" OAuth app), the OAuth Application Registry entry lives on
**your** ServiceNow instance. Your ServiceNow admin registers it once,
your deploy operator captures the `client_id` + `client_secret` into
tfvars + SSM, and every workspace on the deploy connects against that
single OAuth app.

If you happen to have multiple ServiceNow instances (prod + sandbox, or
divisional splits, or an acquired-company instance), each one is a
separate OutcomeOps deploy target — the same way you'd have separate
Confluence sites or separate Azure DevOps tenants across acquisitions.

## Prerequisites

- **On the OutcomeOps side:** operator access to the AWS account
  OutcomeOps is deployed into; ability to flip tfvars and run
  `terraform apply`.
- **On the ServiceNow side:** admin access to the target ServiceNow
  instance — specifically the ability to register an OAuth Application
  Registry entry (System OAuth → Application Registry).

---

## Deploying the integration (operator)

Four steps: register the OAuth app on ServiceNow, put the secret in SSM,
put the URL + client ID in tfvars, apply.

### Step 1: Register the OAuth Application Registry entry (ServiceNow admin)

On the ServiceNow instance you want to connect:

1. Navigate to **System OAuth → Application Registry**.
2. Click **New**. ServiceNow shows the **Select your application connection type** picker. Pick **OAuth - Authorization code grant**.
   - Do NOT pick Client credentials grant or JWT bearer grant — those are machine-to-machine flows and don't produce a redirect back to the OutcomeOps UI.
   - Do NOT pick Resource owner password credential grant — ServiceNow itself recommends against it and the platform doesn't implement it.
3. Fill in:
   - **Name:** anything — e.g. `OutcomeOps AI Assist (prd)`.
   - **Client ID:** ServiceNow generates this.
   - **Client Secret:** ServiceNow generates this.
   - **Redirect URL:** `https://<your-outcomeops-fqdn>/api/servicenow/callback`
     — must match exactly, no trailing slash, no query string.
     (You can find your fqdn from `terraform output ui_fqdn`.)
   - **Refresh token lifespan:** the default (8,640,000 seconds =
     100 days) is fine.
   - **Access token lifespan:** 30 minutes is fine.
4. Save. Copy the **Client ID** and **Client Secret** — you'll paste them
   into tfvars + SSM in the next steps. The Client Secret is the last
   time ServiceNow will show it in plaintext.

**Scopes.** ServiceNow doesn't take a scope parameter on the authorize
URL — the OAuth app inherits the requesting user's ACLs. When a workspace
admin authorizes, ServiceNow grants access to the KB tables their user
account can already read. To constrain what the platform can see, connect
as a dedicated ServiceNow user (e.g. `outcomeops-svc`) with KB-read-only
ACLs, rather than a full admin.

### Step 2: Populate the client_secret in SSM

```bash
aws ssm put-parameter \
  --name "/prd/outcome-ops-ai-assist/oauth/servicenow/client-secret" \
  --value "<the client_secret from Step 1>" \
  --type SecureString \
  --overwrite \
  --region us-west-2
```

**Both regions (HA):** run the same command with the secondary region
too, or use `scripts/ha-sync-secrets` to mirror everything after the
primary is populated:

```bash
ENV=prd APP=outcome-ops-ai-assist \
  PRIMARY=us-west-2 SECONDARY=us-east-1 \
  ./scripts/ha-sync-secrets
```

### Step 3: Set tfvars

In your `terraform/prd.tfvars`:

```hcl
enable_servicenow_integration = true
servicenow_instance_url       = "https://dev307154.service-now.com"
servicenow_oauth_client_id    = "..."  # from Step 1
```

### Step 4: `terraform apply`

```bash
cd terraform
terraform apply -var-file=prd.tfvars
```

This provisions:

- `${env}-${app}-servicenow-integration` Lambda + IAM-authenticated
  Function URL, both regions.
- `${env}-${app}-servicenow-sync` Lambda + SQS queue + DLQ
  (`maxReceiveCount=3`, `ReportBatchItemFailures`), both regions.
- `${env}-${app}-servicenow-scheduler` Lambda + EventBridge cron
  (`45 * * * ? *` — hourly at :45), both regions.
- SSM SecureString placeholder at `/{env}/{app}/oauth/servicenow/client-secret`
  (Step 2 overwrites the placeholder with the real value).
- IAM read grant on the SSM param, both regions.
- CloudWatch alarms on `servicenow-sync-dlq` depth + Errors > 0 on all
  three Lambdas, both regions.

### Step 5: Rebuild the UI Fargate image

The UI proxy learns the new `SERVICENOW_LAMBDA_URL` env variable at
task launch.

```bash
make build-ui-image
terraform apply -var-file=prd.tfvars   # picks up the new task definition
aws ecs update-service --cluster ... --service ui --force-new-deployment
```

### Verifying the deploy

```bash
# Client secret populated (i.e. NOT PLACEHOLDER_CONFIGURE_ME):
aws ssm get-parameter --region us-west-2 --with-decryption \
  --name /prd/outcome-ops-ai-assist/oauth/servicenow/client-secret \
  --query 'Parameter.Value' --output text | grep -qv PLACEHOLDER && echo OK

# All three primary-region Lambdas registered:
aws lambda list-functions --region us-west-2 \
  --query 'Functions[?contains(FunctionName, `servicenow`)].FunctionName'

# Alarms wired:
aws cloudwatch describe-alarms --region us-west-2 \
  --alarm-names prd-outcome-ops-ai-assist-servicenow-sync-dlq-depth \
                prd-outcome-ops-ai-assist-servicenow-integration-errors \
                prd-outcome-ops-ai-assist-servicenow-sync-errors \
                prd-outcome-ops-ai-assist-servicenow-scheduler-errors \
  --query 'MetricAlarms[].{name:AlarmName,state:StateValue}'
```

---

## Connecting ServiceNow (workspace admin)

Because the OAuth app is env-configured, the workspace admin's job is a
single click.

1. Open the OutcomeOps UI and navigate to **Workspace Settings → Integrations**.
2. Find the **ServiceNow Knowledge Base** section. Click **Connect**.
3. Your browser redirects to your ServiceNow instance's OAuth consent
   screen. Sign in (if you weren't already) and click **Allow**.
4. ServiceNow redirects back to the OutcomeOps UI. The connection card
   should show `Status: queued` and then flip to `success` after the
   first sync completes (usually within a minute).

Under the hood the integration Lambda creates (or reuses) a pending
connection row for the workspace, generates an OAuth state, and returns
the authorize URL. Behavior is identical to Confluence/Jira/M365.

---

## Where secrets live

| Field | Where it lives |
|---|---|
| `servicenow_instance_url` | tfvars (public, non-secret) |
| `servicenow_oauth_client_id` | tfvars (public, non-secret) |
| `client_secret` | SSM SecureString at `/{env}/{app}/oauth/servicenow/client-secret` (operator-populated, terraform ignores value changes) |
| `refresh_token` (per-workspace) | DynamoDB, token row, KMS-encrypted with the shared `oauth_tokens` CMK |
| `access_token` | Never persisted — minted on demand from the refresh token at each sync worker invocation |

The pattern matches every other OAuth integration (Atlassian, Microsoft,
Azure DevOps). See `scripts/ha-list-secrets` for the full inventory of
SecureString parameters the operator populates.

---

## What gets ingested

For every article in `kb_knowledge` with `workflow_state=published`:

- The article body (ServiceNow's `text` field) — wrapped in a minimal
  `<html>` document with the article number + short description as a
  header, uploaded to
  `s3://{kb-bucket}/workspaces/{ws}/servicenow/{connection_id}/{sys_id}.html`.
- Each attachment on the article whose filename matches the
  file-ingestion allowlist:
  `.md .txt .pdf .yaml .yml .json .docx .xlsx .pptx .csv .html .htm`.
  Non-allowlist attachments are counted as `skipped`, not `failed`.
  Uploaded to `.../{article_sys_id}/attachments/{safe_filename}`.

## Sync cadence + cost

- Hourly at :45 UTC. Scheduler queries the workspaces table for every
  `SERVICENOW_CONNECTION#` row that isn't in `reauth_required` /
  `in_progress` / `needs_oauth` and enqueues one message per healthy
  connection.
- Delta-only after the first sync (walks articles with
  `sys_updated_on > cursor` in 200-page chunks).
- Manual sync from the UI's **Sync now** button starts from the current
  cursor.
- Orphan cleanup runs after a complete enumeration: articles that
  disappeared from `published` get purged from S3 + vectors.

## Rate limiting

ServiceNow rate-limits per instance. The sync worker retries `429` with
`Retry-After` backoff up to 3 attempts before raising
`ServiceNowRateLimitedError`. On sustained rate-limiting, the run marks
`partial_success` or `failed` and defers to the next scheduler tick.

## Rotation

**Rotate the Client Secret:**

1. On ServiceNow, regenerate the Client Secret on the Application
   Registry entry.
2. Run `aws ssm put-parameter --overwrite` on
   `/prd/outcome-ops-ai-assist/oauth/servicenow/client-secret` with the
   new value. Mirror to the secondary region.
3. Wait for the next Lambda cold start (or invoke the integration Lambda
   once to force one) so the cached secret is refreshed. No `terraform
   apply` needed; the SSM param uses `lifecycle.ignore_changes = [value]`.

**Rotate the client_id or instance URL:** update tfvars, `terraform
apply`. The env vars on the Lambdas will pick up the new values.

## Rollback / disconnect

**Disconnect one workspace:** Workspace Settings → Integrations →
ServiceNow → **Disconnect**. Removes the connection row + token row and
purges `workspaces/{ws}/servicenow/{connection_id}/*` from S3.

**Uninstall the integration entirely:** flip
`enable_servicenow_integration = false`, `terraform apply`. Zero-counts
every resource. Existing S3 KB objects survive (customer data outlives
the flag). Existing SSM SecureString survives (uses
`lifecycle.ignore_changes`); delete manually if needed.

---

## Troubleshooting

**Q: The workspace admin clicks Connect and ServiceNow returns "Redirect
URI mismatch."**
A: The Redirect URL on the Application Registry entry has to exactly match
`https://<your-fqdn>/api/servicenow/callback`. No trailing slash, no
query string, case-sensitive subdomain.

**Q: Integration Lambda returns 500 "ServiceNow integration not fully
configured."**
A: One of `servicenow_instance_url` / `servicenow_oauth_client_id` is
missing from tfvars, or the client-secret SSM param is empty. Check with
the verify-deploy commands above.

**Q: Connection card says `reauth_required`.**
A: The refresh token was rejected (user's ACLs changed, or the OAuth app
was revoked/regenerated). Workspace admin clicks **Connect** again;
existing article-mapped S3 keys survive.

**Q: Card says `failed` with "ServiceNow rate limited".**
A: Instance rate-limiting exceeds the sync worker's backoff. The next
scheduler tick will retry from where the last successful walk stopped.
If it persists, raise the Table API rate limit on the ServiceNow side
(usually configurable per role).

**Q: I don't see the ServiceNow section in Workspace Settings at all.**
A: Either `enable_servicenow_integration = false` in your tfvars, or the
UI Fargate task didn't pick up the manifest env var. Check
`aws lambda get-function-configuration --function-name
prd-outcome-ops-ai-assist-workspace-management --query
'Environment.Variables.INTEGRATION_SERVICENOW_ENABLED'` — should return
`"true"`. If yes, force-redeploy the UI ECS service.

**Q: The sync log says `report_repos_snapshot failed`.**
A: Non-fatal — license-layer liveness reporting couldn't reach the license
server. Sync proceeds normally. If persistent, check the outcomeops-license
Lambda layer is attached to the `servicenow-integration` function.
