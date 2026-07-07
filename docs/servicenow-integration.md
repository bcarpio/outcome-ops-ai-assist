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
> | The **operator** deploying OutcomeOps into your AWS account and turning ServiceNow on for the first time | [Deploying the integration](#deploying-the-integration-operator) |
> | A **workspace admin** connecting your ServiceNow instance to a workspace | [Connecting ServiceNow](#connecting-servicenow-workspace-admin) |
> | A **ServiceNow admin** registering the OAuth app | [Register the OAuth Application Registry entry](#step-1-register-the-oauth-application-registry-entry-servicenow-admin) |
> | A **security / compliance reviewer** evaluating what leaves your instance | [What gets ingested](#what-gets-ingested) and [Where secrets live](#where-secrets-live-adr-044) |

## Scope

The integration is **Knowledge Base only** for MVP:

| Ingested | Not ingested |
|---|---|
| `kb_knowledge` records with `workflow_state=published` | `incident`, `sc_task`, `problem` and other ticket tables |
| Attachments on those articles (filtered to a file-type allowlist — see below) | CMDB (`cmdb_ci_*`) |
| `sys_updated_on` cursor for delta-only re-syncs on scheduler ticks | Draft / review KB articles |
| Article metadata (number, short description, KB, category, sys id) | Article ratings, comments, versions history |

The out-of-scope items may land in future chunks when a customer explicitly
asks. Right now the KB flow is the highest-value grounding source for
chat and it's what regulated customers most commonly want retrieval
against.

## Prerequisites

You'll need:

- **On the OutcomeOps side:** operator access to the AWS account
  OutcomeOps is deployed into; ability to flip a tfvar and run
  `terraform apply`.
- **On the ServiceNow side:** admin access to the target ServiceNow
  instance — specifically the ability to register an OAuth Application
  Registry entry (System OAuth → Application Registry). A ServiceNow
  "admin" role or equivalent.
- **The workspace admin** does NOT need to be the ServiceNow admin;
  they just need the `client_id` + `client_secret` + `instance_url`
  in hand when they hit Save.

---

## Deploying the integration (operator)

There is **only one deploy step**, and no SSM parameters to populate.
(This is unusual — every other integration has env-level OAuth SSM
SecureStrings you have to pre-populate. ServiceNow doesn't; see
[Where secrets live](#where-secrets-live-adr-044) for why.)

### Step 1: Flip the flag

In your `terraform/prd.tfvars` (or `dev.tfvars`):

```hcl
enable_servicenow_integration = true
```

### Step 2: `terraform apply` (both regions)

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
  (`45 * * * ? *` — hourly at :45, matching RetrieveIT's staggering
  convention), both regions.
- CloudWatch alarms on `servicenow-sync-dlq` depth + Errors > 0 on all
  three Lambdas, both regions. Alarms route to
  `module.audit_alerts_sns.topic_arn` (see [ADR-044](adr/ADR-044-per-workspace-oauth-servicenow.md)
  for delivery detail — the CMK fix from 2026-07-07 already carries
  alarm publishes through to email).

### Step 3: Rebuild the UI Fargate image

The UI proxy learns the new `SERVICENOW_LAMBDA_URL` env variable at
task launch. Rebuild the UI image and force-redeploy both regions:

```bash
make build-ui-image
terraform apply -var-file=prd.tfvars   # picks up new task definition
aws ecs update-service --cluster ... --service ui --force-new-deployment
```

That's it. There are no SecureStrings to populate, no
`aws ssm put-parameter` calls. All customer-specific OAuth credentials
land at workspace-connect time via the UI setup form.

### Verifying the deploy

```bash
# All three primary-region Lambdas registered:
aws lambda list-functions --region us-west-2 \
  --query 'Functions[?contains(FunctionName, `servicenow`)].FunctionName'

# The Function URL surfaced to Fargate:
aws ssm get-parameter --region us-west-2 \
  --name /prd/outcome-ops-ai-assist/api/servicenow-integration-url \
  --query 'Parameter.Value' --output text

# Alarms wired:
aws cloudwatch describe-alarms --region us-west-2 \
  --alarm-names prd-outcome-ops-ai-assist-servicenow-sync-dlq-depth \
                prd-outcome-ops-ai-assist-servicenow-integration-errors \
                prd-outcome-ops-ai-assist-servicenow-sync-errors \
                prd-outcome-ops-ai-assist-servicenow-scheduler-errors \
  --query 'MetricAlarms[].{name:AlarmName,state:StateValue}'
```

All four alarms should exist and be in `OK` state.

---

## Connecting ServiceNow (workspace admin)

Each ServiceNow instance is connected per-workspace. That means every
customer using your deploy registers their own OAuth Application Registry
entry against their own instance — there is no shared OAuth app.

The flow has three actors:

1. **ServiceNow admin** registers a new Application Registry entry on
   the customer's instance ([Step 1](#step-1-register-the-oauth-application-registry-entry-servicenow-admin)).
2. **Workspace admin** enters those credentials in the OutcomeOps UI
   setup form ([Step 2](#step-2-fill-in-the-setup-form-workspace-admin)).
3. The **workspace admin** clicks Save & Authorize, which redirects
   them to ServiceNow for OAuth consent, then back to OutcomeOps
   ([Step 3](#step-3-authorize)).

If the workspace admin and ServiceNow admin are the same person, the
whole flow takes about 5 minutes.

### Step 1: Register the OAuth Application Registry entry (ServiceNow admin)

On the ServiceNow instance you want to connect:

1. Navigate to **System OAuth → Application Registry**.
2. Click **New**. ServiceNow shows the **Select your application connection type** picker. Pick **OAuth - Authorization code grant**.
   - Do NOT pick Client credentials grant or JWT bearer grant. Those are machine-to-machine flows and don't produce a redirect back to the OutcomeOps UI.
   - Do NOT pick Resource owner password credential grant — ServiceNow itself recommends against it and the platform doesn't implement it.
3. Fill in:
   - **Name:** anything — e.g. `OutcomeOps AI Assist (prd)`.
   - **Client ID:** ServiceNow generates this.
   - **Client Secret:** ServiceNow generates this.
   - **Redirect URL:** `https://<your-outcomeops-fqdn>/api/servicenow/callback`
     — must match exactly, no trailing slash, no query string.
     (You can find your fqdn from `terraform output ui_fqdn`.)
   - **Refresh token lifespan:** the default (8,640,000 seconds =
     100 days) is fine. OutcomeOps auto-rotates the refresh token on
     each refresh for instances that support it.
   - **Access token lifespan:** 30 minutes is fine; the sync worker
     refreshes on every invocation.
4. Save. Copy the **Client ID** and **Client Secret** — you'll paste
   them into the OutcomeOps UI in the next step. The Client Secret is
   the last time ServiceNow will show it in plaintext; if you lose it
   you'll have to regenerate.

**Scopes.** ServiceNow doesn't take a scope parameter on the authorize
URL — the OAuth app inherits the requesting user's ACLs. When the
workspace admin authorizes, ServiceNow grants access to the KB tables
their user account can already read. If you want tighter control,
create a dedicated ServiceNow user (e.g. `outcomeops-svc`) with
KB-read-only ACLs, and have the workspace admin sign in as that
user during the OAuth consent step.

### Step 2: Fill in the setup form (workspace admin)

In the OutcomeOps UI:

1. Navigate to **Workspace Settings → Integrations**.
2. Find the **ServiceNow Knowledge Base** section. Click **Connect**.
3. Fill in the three fields:
   - **Instance URL:** `https://<subdomain>.service-now.com`. No
     trailing slash. Legacy `.servicenow.com` (without the hyphen) is
     accepted with a warning but not recommended.
   - **Client ID:** paste from Step 1.
   - **Client Secret:** paste from Step 1.
4. Click **Save & Authorize.**

Under the hood, this POSTs to
`/api/workspaces/{ws}/servicenow/setup` which KMS-encrypts the
`client_secret` with the shared `oauth_tokens` CMK and stores it in
DynamoDB. It then requests an authorize URL from
`/api/workspaces/{ws}/servicenow/authorize?connection_id={id}` and
redirects the browser.

### Step 3: Authorize

You'll land on the ServiceNow instance's OAuth consent screen.
Sign in (if you weren't already) and click **Allow.**
ServiceNow redirects you back to the OutcomeOps UI, which:

1. Exchanges the code for an access + refresh token.
2. Calls `sys_user?sysparm_query=user_name=javascript:gs.getUserName()`
   to capture your ServiceNow user identity — this becomes the
   "connected as" label on the connection card and lets us detect a
   reconnect that lands on a different user account.
3. Enqueues an immediate sync so you see KB articles appearing without
   waiting for the top-of-hour scheduler tick.

Refresh the workspace page after ~30 seconds. The connection card
should show `Status: success` with the KB article count implicit in
the fact that new articles have shown up in chat retrieval.

---

## Where secrets live (ADR-044)

Unlike every other OutcomeOps integration, ServiceNow does not use
**env-level SSM SecureStrings** for the OAuth credentials. Instead:

| Field | Where it lives |
|---|---|
| `instance_url` | DynamoDB, connection row, plaintext (not a secret) |
| `client_id` | DynamoDB, connection row, plaintext (not a secret) |
| `client_secret` | DynamoDB, token row, **KMS-encrypted with the shared `oauth_tokens` CMK** |
| `refresh_token` | DynamoDB, token row, KMS-encrypted (same key) |
| `access_token` | Never persisted — minted on demand from the refresh token at each sync worker invocation |

Why: each customer's ServiceNow instance requires its own Application
Registry OAuth app registered on that instance. There is no shared
"OutcomeOps ServiceNow OAuth app" to pre-populate at deploy time. See
[ADR-044](adr/ADR-044-per-workspace-oauth-servicenow.md) for the full
decision context.

Practical consequences:

- Deploy has zero `aws ssm put-parameter` steps for ServiceNow. The
  operator's `ha-list-secrets` output shouldn't mention ServiceNow.
- Rotation: if a customer regenerates their Client Secret on the
  ServiceNow side, the workspace admin re-runs the setup form with
  the new value. Same connection_id is reused (idempotent per
  `(workspace_id, instance_url)`), so no reconnect / lost-history
  disruption.
- Disconnect: `DELETE /workspaces/{ws}/servicenow/connections/{id}`
  removes both DDB rows and fans a delete-worker message so the
  workspaces/{ws}/servicenow/{conn}/* S3 keys + downstream vectors
  get purged.

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
  Attachments outside the allowlist are counted as `skipped`, not
  `failed`. Uploaded to
  `.../{article_sys_id}/attachments/{safe_filename}`.

For every article and attachment, the `metadata` on the S3 object
carries `source=servicenow`, the article + attachment sys_id, the
article number, and the ServiceNow instance URL. Downstream retrieval
(chat, MCP) can filter on any of these.

## Sync cadence + cost

- Hourly at :45 UTC. Scheduler queries the workspaces table for every
  `SERVICENOW_CONNECTION#` row that isn't in `reauth_required` /
  `in_progress` / `needs_oauth` and enqueues one message per healthy
  connection.
- Delta-only after the first sync. The scheduler passes the
  connection's stored `config.cursor` (max `sys_updated_on` seen);
  the sync worker enumerates articles with `sys_updated_on > cursor`
  in 200-page chunks.
- Full walk on first sync only, or on operator-triggered manual sync
  via the UI's **Sync now** button (which starts from the current
  cursor — you can't force a full re-walk without disconnecting +
  reconnecting).
- Orphan cleanup runs after a complete enumeration: articles that
  disappeared from ServiceNow (moved out of `published` or deleted)
  get delete-worker messages queued so their S3 keys + vectors
  disappear from the KB.

## Rate limiting

ServiceNow rate-limits per instance. The sync worker retries `429`
with `Retry-After` backoff up to 3 attempts before raising
`ServiceNowRateLimitedError`. When that happens, the sync marks the
run `partial_success` (if some articles landed) or `failed` (if none
did), skips orphan cleanup to avoid false deletes, and lets SQS
redeliver via the DLQ path if the underlying issue persists.

On the observability side, the CloudWatch metric filter phrase is:

```
ServiceNow rate limited
```

Which matches every 429-during-sync log line. A CloudWatch metric
filter + alarm can be added on that phrase if a customer needs the
signal isolated from the DLQ-depth alarm.

## Rollback / disconnect

**Uninstall the integration entirely:**

```hcl
enable_servicenow_integration = false
```

`terraform apply`. This zero-counts every ServiceNow resource. Existing
S3 KB objects under `workspaces/{ws}/servicenow/` are NOT
deleted by terraform — they're customer data that outlived the
integration flag. If you want them gone, disconnect each workspace's
ServiceNow connection first (the UI's Disconnect button fans a
delete-worker message for the full prefix); then flip the flag.

**Rotate the Client Secret:** rerun the workspace admin's setup form
with the new value.

---

## Troubleshooting

**Q: The workspace admin filled in Save & Authorize but ServiceNow returns
"Redirect URI mismatch."**
A: The Redirect URL on the Application Registry entry has to exactly match
`https://<your-fqdn>/api/servicenow/callback`. No trailing slash, no query
string, case-sensitive subdomain. This is the most common setup failure
across every OAuth integration.

**Q: Connection card says `reauth_required`.**
A: The refresh token was rejected (usually because the ServiceNow admin
revoked or regenerated the OAuth app, or the user's ACLs changed to
disallow the KB tables). The workspace admin clicks **Connect** again
and re-runs the setup form; existing article-mapped S3 keys survive.

**Q: Card says `failed` with "ServiceNow rate limited" in the error field.**
A: The instance is rate-limiting harder than the sync worker's 3-attempt
retry can absorb. The next scheduler tick will retry with a fresh cursor
starting from where the last successful walk stopped. If it persists,
raise the instance's rate limit (Table API is usually configurable per
role) or reach out to ServiceNow support.

**Q: I don't see the ServiceNow section in Workspace Settings at all.**
A: Either `enable_servicenow_integration = false` in your tfvars, or the
UI Fargate task didn't pick up the manifest env var yet. Check
`aws lambda get-function-configuration --function-name
prd-outcome-ops-ai-assist-workspace-management --query
'Environment.Variables.INTEGRATION_SERVICENOW_ENABLED'` — it should
return `"true"`. If yes, force-redeploy the UI ECS service.

**Q: The sync log says `report_repos_snapshot failed`.**
A: Non-fatal — the license layer's liveness reporting couldn't reach
the license server. Sync proceeds normally. If you see this on every
run, check that the outcomeops-license Lambda layer is attached to
the `servicenow-integration` function.

---

## Related

- [ADR-044: Per-workspace OAuth credentials for ServiceNow](adr/ADR-044-per-workspace-oauth-servicenow.md)
- [ADR-019: License layer as source of truth](adr/ADR-019-outcomeops-license-layer.md) — same repo-limit accounting applies; one ServiceNow instance counts as one licensable "repo" identifier
- [ADR-022: Active/passive multi-region HA](adr/ADR-022-active-passive-multi-region.md) — the ServiceNow secondary-region topology follows the same pattern as the other integrations
