# Database Integration

OutcomeOps AI Assist's database integration ingests **production-truth
schema context** — tables, columns, relationships, and safe aggregate
statistics — from your operational and analytical databases. It
introspects schemas via a schema introspection engine wrapped in the
same 3-Lambda trio pattern used by every other OutcomeOps source.

Once connected, your AI assistant can answer questions like:

- "Which tables hold patient diagnosis codes?"
- "What columns track customer email in the analytics warehouse?"
- "How many distinct values does `claim_status` have?"
- "What's the join relationship between `orders` and `customers`?"

without anyone hand-curating a data dictionary.

> **Who is this doc for?**
>
> | If you're... | Jump to |
> |---|---|
> | The **operator** deploying OutcomeOps into your AWS account and turning on the database source for the first time | [Deploying the integration](#deploying-the-integration-operator) — 3 commands: build the database Lambda container images, `terraform apply`, rebuild the UI image |
> | A **workspace admin** connecting one of your databases to a workspace | [Connecting a database](#connecting-a-database) → [Configuring scope](#configuring-scope-which-tables-to-ingest) |
> | A **security / compliance reviewer** evaluating what data leaves the customer's DB | [What gets ingested](#what-gets-ingested) and [Compliance posture](#compliance-posture) |

## Why this integration exists

For regulated enterprises (HCLS, financial services, public sector),
the schema is frequently the **only** source of truth. Heritage
Oracle / MS SQL Server / SAP HANA databases often have no clean code
repo behind them — the DB itself IS the documentation. Even where
ORMs / migrations exist, code intent regularly diverges from
production reality (manual `ALTER`s, dropped FKs, etc.). Code-maps
alone don't cover this.

## Supported engines

| Engine | Covers | Notes |
|---|---|---|
| **PostgreSQL** | Postgres, Aurora Postgres, RDS Postgres, Redshift | Redshift speaks the Postgres wire protocol, so the same connector reaches it |
| **MySQL** | MySQL, Aurora MySQL, RDS MySQL, MariaDB | Old `caching_sha2_password` plugin may need `mysql_native_password` fallback |
| **MS SQL Server** | On-prem MSSQL, Azure SQL, Amazon RDS for SQL Server | Uses Microsoft's `mssql-python` driver |

### Roadmap (demand-driven, not in MVP)

- **Oracle** — the upstream engine does not support today. Real regulated-buyer need; tracked as a near-term roadmap item.
- **SAP HANA** — the upstream engine does not support today. Lost-deal signal informs priority; tracked alongside Oracle.
- **BigQuery / Snowflake** — supported by the upstream engine but deprioritized for the regulated-buyer MVP (most ICP customers run AWS-native warehouses). Will add when a customer asks.
- **dbt projects** — supported by the upstream engine, but rare in regulated ICP (heritage ETL dominates). Deferred.

### Out of scope

NoSQL stores (MongoDB, DynamoDB, Cassandra). The schema introspection
engine is fundamentally relational/columnar — these don't fit the
introspection model.

## What gets ingested

| What | Default | Notes |
|---|---|---|
| **Schema metadata** — tables, columns, types, FKs, PKs, indexes | Always pulled | Read from `INFORMATION_SCHEMA`; no row data leaves the DB |
| **Aggregate column statistics** — row counts, null %, cardinality, numeric min/max | On | Aggregates computed by the DB itself; only the result leaves |
| **`top_values`** (column-level most-common values) | **STRIPPED** before reaching S3 | Treated as data leakage, not statistics. Stripped twice (sync-side and file-ingestion-side defensively) |
| **Row sampling** | Off | Admin opt-in per-schema (any tables in a sampling-enabled schema get rows pulled). Comprehend PII redaction runs at ingest if sampling is enabled. |

The strip-and-default-off posture exists for regulated-buyer privacy
defaults: for a column called `patient_diagnosis`, the top 10 values
*are* the data. Even when "aggregated by frequency," they expose
PHI in a way that aggregate statistics like null-percentage don't.

## Deploying the integration (operator)

This section is for the operator deploying OutcomeOps into their AWS
account — not the workspace admin connecting an individual DB later.

The integration ships as two **Lambda container images** (not a Lambda
layer — the schema introspection engine + DB drivers total over 350 MB
unzipped, well past Lambda's 250 MB unzipped layer cap; container
images allow up to 10 GB):

- `database-integration` — UI-facing picker / connection CRUD
- `database-sync` — SQS-driven introspection worker

Plus a third zip-based Lambda (`database-scheduler`) for the hourly cron.

1. **Build + push the two container images**. The script also stages the
   OutcomeOps license validator layer content into the sync image at
   build time (container-image Lambdas can't attach Lambda layers, so
   the license code is baked in):
   ```bash
   ENVIRONMENT=prd make build-database-images
   ```
   This creates the ECR repos via a targeted `terraform apply`, builds
   both images, pushes them, and writes the image tags to SSM so the
   main Terraform apply can reference them.

2. **Apply Terraform** — picks up `lambda-database.tf`, `kms-database.tf`,
   and the Fargate `DATABASE_LAMBDA_URL` env var wired into
   `ui-fargate.tf`:
   ```bash
   cd terraform
   terraform plan -var-file=prd.tfvars -out=terraform.prd.out
   terraform apply terraform.prd.out
   ```
   No new tfvars required — the integration is gated by the existing
   `enable_workspaces` flag.

3. **Rebuild + redeploy the UI image** so the new Database card
   appears in Workspace Settings:
   ```bash
   ENVIRONMENT=prd make build-ui-image
   aws ecs update-service \
     --cluster prd-outcome-ops-ai-assist-ui \
     --service ui \
     --force-new-deployment
   ```

After step 3, the **Database** card appears under **Workspace
Settings → Integrations** for workspace admins. Continue to
"Connecting a database" below for the per-workspace setup.

**Rebuild the images whenever:**
- `lambda/db-introspection-requirements.txt` changes (introspection-engine release bump, DB driver bump)
- Either Lambda's Dockerfile changes
- The handler Python code changes (rebuild + redeploy to pick up the new code)
- The OutcomeOps license layer version changes (`outcomeops_license_layer_arn` in tfvars bumps)

See [`docs/deployment-guide.md`](./deployment-guide.md#container-image-updates) for the broader image-update story.

## Connecting a database

Workspace admins go to **Workspace Settings → Integrations** and click
**Connect** on the Database card.

### Per-engine connection inputs

| Engine | Required | Optional |
|---|---|---|
| **Postgres** | host, port (default 5432), database name, username, password | SSL mode (`require` / `verify-ca` / `verify-full` / `disable`); display label |
| **MySQL** | host, port (default 3306), database name, username, password | SSL mode; display label |
| **MS SQL Server** | host, port (default 1433), database name, username, password | `Encrypt=yes` toggle (recommended on); display label |

The **Test connection** button runs the engine's connection-check
API against your DB **without persisting the credentials**, so you
get immediate feedback if the creds are bad before you save.

### Creating a read-only DB user

Strongly recommended — OutcomeOps only needs `SELECT` privileges, so
give it a dedicated read-only user rather than an admin / writeable
account. Per-engine SQL is in
[`database-integration-readonly-user.md`](./database-integration-readonly-user.md).

### Networking

OutcomeOps's database-sync Lambda must be able to reach your DB host.
This is the customer's networking call:

- **VPC peering / PrivateLink** (preferred for prod): your DB stays
  private; the Lambda VPC reaches it via the peer/endpoint.
- **DB publicly accessible, SG allowlist**: lower-friction, less
  preferred. Allowlist OutcomeOps's NAT egress IP(s) in your DB's
  security group.
- **VPN / Direct Connect**: works fine if you already have a path.

OutcomeOps does NOT ingress data through the public internet by
default — network exposure is yours to decide.

## Configuring scope (which schemas to ingest)

After the connection is created it lands in `needs_configuration`
status. Click **Configure** to open the schema picker.

The picker is **schemas-only** — admins pick which `catalog.schema`
pairs the integration should ingest, and every table in those schemas
is auto-discovered on every sync. There is no per-table picker: when
your DBAs add or drop a table inside a selected schema, the next
sync (hourly cron or **Sync now**) picks it up automatically.

For each selected schema you can independently toggle:

- **Introspect** (always on for selected schemas): pull schema +
  aggregate stats for every table in the schema. Safe.
- **Sample rows** (per-schema opt-in, default off): also pull
  example rows from tables in that schema. Comprehend PII redaction
  runs at ingest if you enable it, but the regulated-buyer-friendly
  default is to leave sampling off entirely.

Save → the connection moves to `queued`, and the next scheduler tick
(hourly) or **Sync now** kicks off introspection.

### Auto-discovery semantics

- **Tables added** inside a selected schema flow into the KB on the
  next sync. No admin action required.
- **Tables removed** inside a selected schema are detected on the
  next sync; the delete-worker wipes their S3 artifacts + vectors via
  the orphan-cleanup queue.
- **Whole new schemas** the DBA creates are NOT auto-included — the
  regulated-buyer posture is "the admin says yes once per schema."
  The connection card surfaces a "N new schemas available - click
  Configure" pill when the introspector sees a schema your RO user
  can read that you haven't included yet, and the picker tags those
  schemas with a "NEW" badge.
- **Whole schemas removed** are detected too; their auto-discovered
  tables are orphan-cleaned just like the per-table case.

The drift surfaced at each sync (schemas/tables added + removed)
also lands on the audit row and (optionally) emails — see
[Drift notifications](#drift-notifications) and
[Audit events emitted](#audit-events-emitted).

## Under the hood

The schema introspection engine is JetBrains' [Databao Context
Engine](https://github.com/JetBrains/databao-context-engine) (Apache
2.0). OutcomeOps wraps it in a thin helper module and runs it inside
an AWS Lambda container image; it's a build-time dependency, not a
runtime service. Attribution lives in the repository's
[NOTICE](../NOTICE) file.

## Compliance posture

For the conversation with your auditors / security org:

- **Credentials at rest**: stored in DynamoDB in your AWS account
  (BYOC), KMS-encrypted with a dedicated KMS key
  (`alias/{env}-outcome-ops-ai-assist-db-credentials`). Separate from
  the OAuth-token KMS key so IAM policies on the DB key scope cleanly
  to the database-* Lambdas only.
- **Every credential access** emits a `DB_CREDENTIAL_USED` row in the
  unified audit table. No CloudTrail vs. audit-table split — every
  forensic event lives in one queryable place.
- **No live SQL execution** against your DB from the chat Lambda.
  The integration does one-shot introspection only; no AI-generated
  SQL ever runs against your prod DB in this MVP. (Roadmap below for
  what "live SQL via MCP" would require us to ship.)
- **PII redaction** via AWS Comprehend runs at ingest as a defense-
  in-depth safety net on any descriptive metadata.
- **`top_values` strip** double-gates column-value data from
  reaching S3 even if either the sync or file-ingestion strip
  regresses.

## Sync mechanics

```
Workspace admin clicks "Connect Database"
     |
     v
database-integration Lambda
  - validates connection (engine's connection-check API)
  - encrypts credentials with KMS, stores DB_CREDENTIAL# in DDB
  - emits DB_CONNECTION_ESTABLISHED audit row
  - returns 200; UI shows the connection card
     |
     v
Admin selects schemas -> Save
  - update_scope() writes scope_config to DDB
    (strict 2-segment catalog.schema keys; no per-table entries)
  - queues sync to DATABASE_SYNC_QUEUE
     |
     v
database-sync Lambda (also fired hourly by database-scheduler)
  - license-validate (ADR-019)
  - decrypts credential -> emits DB_CREDENTIAL_USED audit row
  - check_repo_limit("database:{engine}:{host_hash}")
  - SCOPED introspect: walks only admin-selected schemas; every
    table in those schemas is auto-discovered (no per-table list)
  - STRIPS top_values from YAML in-memory
  - queues to FILE_INGESTION_QUEUE (one message per table)
  - UNRESTRICTED schema enumeration (cheap metadata-only call):
    every schema the RO user can see, used to compute
    schemas_added / schemas_removed against the connection's
    prior-sync available_schemas snapshot
  - emits DATABASE_SYNCED audit row with the schema + table drift
    deltas; publishes drift notification to SNS if non-empty
  - queues delete_orphans for stale tables
     |
     v
file-ingestion Lambda
  - process_database_artifact()
  - DEFENSIVE top_values strip (belt + suspenders)
  - Comprehend PII redaction
  - chunks YAML per-table -> S3 keys with table_id in filename
  - vector embeddings via the existing pipeline
```

### Per-sync schema-drift detection

After the scoped introspect, the sync runs a second unrestricted
introspect call — profiling and sampling both disabled — to
enumerate every `catalog.schema` the RO user can see. The current
visible-schema set is persisted on the connection record as
`available_schemas`, and the previous record's `available_schemas`
is diffed to compute `schemas_added` / `schemas_removed`. This is
what powers the "N new schemas available" pill on the connection
card and the new-schema badging in the picker — without it, the
auto-discovery model would only catch tables added inside
already-included schemas, leaving whole new schemas invisible
until an admin happened to reopen the picker. Failure of the
unrestricted call is graceful (logged + treated as empty drift);
the scoped introspect's result still flows to the KB.

## Drift notifications

The hourly sync detects schema-level drift (new schemas the DBA
added that admins haven't included, schemas that disappeared) and
table-level drift (tables added or removed inside selected
schemas). These deltas ride on the `DATABASE_SYNCED` audit row
regardless, but an SNS-backed push path can also email admins so
they don't have to poll the audit table.

### Opting in

Set `database_drift_notification_emails` in your environment's
tfvars to a list of email addresses:

```hcl
database_drift_notification_emails = [
  "data-platform-oncall@example.com",
  "dba-team@example.com",
]
```

Empty list (default) = no SNS topic, no subscriptions, no IAM
publish-grant on the sync Lambda. The audit row still carries the
drift fields, so audit-table polling / downstream alerting via the
existing audit pipeline still works regardless.

`terraform apply` creates a per-environment topic
(`{env}-{app}-database-drift`, KMS-encrypted via `alias/aws/sns`),
subscribes the listed emails, and exports the topic ARN as the
`database_drift_topic_arn` Terraform output. **Each subscribed
inbox must confirm the SNS subscription** the first time —
standard SNS opt-in dance.

### What triggers a notification

The sync publishes if **any** of these lists are non-empty:

- `schemas_added` — `catalog.schema` keys that appeared since the
  last sync (DBA created a schema the RO user can see)
- `schemas_removed` — `catalog.schema` keys that disappeared
- `tables_added` — sanitized `catalog.schema.table` ids inside
  selected schemas that appeared since the last sync (DBA created
  a new table)
- `tables_removed` — sanitized `catalog.schema.table` ids inside
  selected schemas that disappeared

Steady-state syncs (no drift) publish nothing — admins only hear
from this topic when something genuinely changed. The first sync
after a fresh connect, or the first sync after this chunk's
deploy, will publish because everything is "added" relative to the
empty prior-sync snapshot.

### Sample notification

```
Subject: [OutcomeOps] Database drift: prod-postgres-analytics (postgres)

Database drift detected during sync.

Connection:   prod-postgres-analytics
Engine:       postgres
Host:         analytics-rds.cluster-xyz.us-west-2.rds.amazonaws.com
Database:     analytics
Workspace:    ws_abc123
Sync ID:      sync_20260514_140000_a1b2c3d4
Time:         2026-05-14T14:00:00.123456+00:00

Schemas added:   ['analytics.fraud_v2']
Schemas removed: []

Tables added:    ['analytics.healthcare.encounters_v2']
Tables removed:  []

These changes were detected against the connection's prior-sync
snapshot. Tables in selected schemas auto-flow to your KB; new
SCHEMAS require admin action - open Configure Scope on the
connection to include them.

This is an automated notification from OutcomeOps AI Assist. To
adjust who receives these, update database_drift_notification_emails
in your terraform tfvars and re-apply.
```

Plain-text format so Slack-webhook / SES-rule / generic-email
subscribers all consume cleanly.

### Subscribing additional endpoints (non-email)

The `database_drift_topic_arn` Terraform output lets you wire
additional subscribers without touching the OutcomeOps codebase:

| Use case | How |
|---|---|
| **Slack via webhook** | Subscribe a transform Lambda to the topic; transform Lambda formats + posts to the Slack webhook URL. |
| **PagerDuty** | Subscribe PagerDuty's SNS integration endpoint (HTTPS) to the topic ARN. |
| **SQS routing** | Subscribe an SQS queue to the topic ARN (set `RawMessageDelivery=true` if you want the raw body, otherwise SNS wraps it). |
| **Cross-account fan-out** | Add the foreign account principal to the topic policy via the SNS console / a separate `aws_sns_topic_policy` resource in your customer-owned Terraform. |

Empty topic ARN (default `database_drift_notification_emails = []`)
means no topic exists, so additional subscribers can only be wired
once at least one email is configured — pick a placeholder inbox
if you want SNS-only subscribers and no human readers.

## Audit events emitted

| Action | Fires when | Volume |
|---|---|---|
| `DB_CONNECTION_ESTABLISHED` | Admin connects a DB | One per connect |
| `DB_CREDENTIAL_USED` | Sync decrypts credential to introspect | One per connection per scheduler tick |
| `DATABASE_SYNCED` | Successful introspection completed | One per successful sync |
| `DB_CONNECTION_DELETED` | Admin disconnects | One per disconnect |

`DB_CREDENTIAL_USED` is hidden from dashboard cards (high-frequency)
but is queryable via `?action=DB_CREDENTIAL_USED` in the audit API.

### `DATABASE_SYNCED` extra_details fields

| Field | Meaning |
|---|---|
| `sync_id` | Stable id for this sync run; appears in CloudWatch logs and the drift email |
| `engine` | `postgres` / `mysql` / `mssql` |
| `host_hash` | First 16 hex chars of SHA-256 over the connection host — used for license-tracking repo identity |
| `tables_count` | Number of tables in scope after auto-discovery |
| `columns_count` | Total column count across all in-scope tables |
| `top_values_stripped` | Always `true` — the strip ran (defense-in-depth check) |
| `sampling_schemas` | The `catalog.schema` keys in `scope_config.sampling.include` for this sync (which schemas had row-sampling on). Empty list = sampling fully off. |
| `tables_added` | Sanitized `catalog.schema.table` ids that appeared in selected schemas since the prior sync (auto-discovered new tables) |
| `tables_removed` | Sanitized `catalog.schema.table` ids that disappeared from selected schemas since the prior sync |
| `table_count_delta` | `len(tables_added) - len(tables_removed)` — quick scan-friendly net delta |
| `schemas_added` | `catalog.schema` keys the RO user can see now that weren't visible in the prior sync's unrestricted enumeration |
| `schemas_removed` | `catalog.schema` keys present in the prior sync's enumeration that are gone now |
| `schema_count_delta` | `len(schemas_added) - len(schemas_removed)` |

Subscribe alerting (CloudWatch Metric Filter, audit-stream Lambda,
Splunk, etc.) directly to these fields if email isn't your
preferred push path.

## Roadmap

Demand-driven follow-ups, not "future" hand-waving — each maps to a
specific customer ask we're tracking:

- **Oracle support** — upstream engine gap; HCLS / financial-services demand.
- **SAP HANA support** — upstream engine gap; named lost-deal signal.
- **BigQuery + Snowflake** — supported by the upstream engine but lower priority for the regulated ICP; add when a customer hits this gap.
- **Row sampling default-on with admin per-schema opt-in UX** — already wired technically (per-schema toggle in the picker), just default-off in MVP. Enable selectively for customers comfortable with the redaction story.
- **Live SQL execution via the chat Lambda (upstream engine's MCP server)** — the big enterprise unlock, but requires us to own a per-workspace query-governance layer (column-level masking, query allow-lists, per-user query audit, generated-SQL audit trail). **Will not ship until a lighthouse regulated customer co-designs the governance with us.**

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| **"Connection test failed"** at create time | Bad credentials, unreachable host, or firewall block | Verify the username/password against the DB directly. Check that the OutcomeOps Lambda VPC can route to your DB host (VPC peering / SG allowlist / etc.) |
| **MSSQL: "Cannot access database 'X' — check the database name or your grants on that database. (Authentication itself succeeded against 'master'.)"** at create / Test connection time | Connection-create probed `master` with the same creds, auth succeeded there, so the original `Login failed for user` was MSSQL's privacy-preserving alias for "DB not found / no grants on this DB." | Double-check the database name. If the name is correct, grant the RO user `db_datareader` on that database (see [readonly-user doc](./database-integration-readonly-user.md#ms-sql-server-incl-azure-sql-rds-for-sql-server)). |
| **MSSQL: "Login failed for user 'X'"** still surfaced (master-probe didn't fire) | Either the credentials are genuinely wrong, or the `master` probe itself failed (network blip, encrypt mismatch, transient outage). | Verify the password against the DB directly via `sqlcmd`. If the password is correct and master is reachable, re-run **Test connection** — the master-probe is best-effort and falls back to the raw upstream error on its own failure. |
| **Picker returns HTTP 502 with `{"error": "...", "error_type": "introspection_failed"}`** | Picker hit a real introspection failure (auth, network, malformed config, upstream exception) — distinct from "user genuinely has zero visible schemas." | The `error` field carries the upstream Python exception message verbatim. Most common causes: SSL cert rejection (see [Adding customer trust roots](./database-integration-readonly-user.md#adding-customer-trust-roots-non-rds--on-prem)), revoked DB credential (reconnect with new password), or a recently-changed RO user that lost `SELECT` on `information_schema`. The Lambda log carries the full traceback. |
| **MySQL: "Access denied for user 'X'@'%' to database 'Y'"** (error code 1044) | User authenticated but has no grants on the named database. Different error from MSSQL — MySQL surfaces this distinctly. | `GRANT SELECT ON <db>.* TO '<user>'@'%';` then re-test. |
| **`auth_error`** on a connection after some time | DB password rotated, user revoked, or SSL cert changed | Workspace admin disconnects and reconnects with the new creds. (DB-side auth has no refresh-token equivalent — manual rotation is the path.) |
| **`blocked_license_limit`** status | Hitting the repo-limit for your OutcomeOps tier | Same as any other source — increase your tier or trim connections. Repo identity is `{engine}:{host_hash}` so the same physical DB across multiple workspace connections counts ONCE |
| **Schema picker is slow / empty** | Large warehouse, slow `INFORMATION_SCHEMA` introspection, or the connection user lacks `SELECT` on the schemas | Confirm the user has the right grants per [`database-integration-readonly-user.md`](./database-integration-readonly-user.md). For very large warehouses (10k+ tables), a DDB-cached schema tree is on the roadmap |
| **MSSQL columns have `stats: null`** despite sampling=on | The upstream introspection plugin doesn't implement per-column stats for MSSQL. Schema metadata + sample rows still work; aggregate column stats don't. | No customer-side fix — tracked as a v2 follow-up. Postgres + MySQL get full stats; MSSQL gets schema + samples only. |
| **Connection card returns "Server misconfiguration: schema introspection engine unavailable"** | Container image wasn't built (or not yet pushed) before terraform apply | Run `ENVIRONMENT=prd make build-database-images` then `terraform apply` |

## Related

- [`docs/azure-ad-m365-integrations.md`](./azure-ad-m365-integrations.md) — M365 source setup (OneDrive, SharePoint, Outlook, Teams, OneNote)
- ADR-019 — license-layer rollout (database-sync is license-gated like every other sync)
- ADR-018 — audit logging (every DB integration event emits an audit row)
