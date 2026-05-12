# Database Integration

OutcomeOps AI Assist's database integration ingests **production-truth
schema context** — tables, columns, relationships, and safe aggregate
statistics — from your operational and analytical databases. It uses
[JetBrains' Databao Context Engine](https://github.com/JetBrains/databao-context-engine)
(Apache 2.0) under the hood and wraps it in the same 3-Lambda trio
pattern used by every other OutcomeOps source.

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
> | The **operator** deploying OutcomeOps into your AWS account and turning on the database source for the first time | [Deploying the integration](#deploying-the-integration-operator) — 3 commands: build the Databao Lambda layer, `terraform apply`, rebuild the UI image |
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

- **Oracle** — Databao does not support today. Real regulated-buyer need; tracked as a near-term roadmap item.
- **SAP HANA** — Databao does not support today. Lost-deal signal informs priority; tracked alongside Oracle.
- **BigQuery / Snowflake** — supported by Databao but deprioritized for the regulated-buyer MVP (most ICP customers run AWS-native warehouses). Will add when a customer asks.
- **dbt projects** — Databao supports, but rare in regulated ICP (heritage ETL dominates). Deferred.

### Out of scope

NoSQL stores (MongoDB, DynamoDB, Cassandra). Databao is fundamentally
relational/columnar — these don't fit the introspection model.

## What gets ingested

| What | Default | Notes |
|---|---|---|
| **Schema metadata** — tables, columns, types, FKs, PKs, indexes | Always pulled | Read from `INFORMATION_SCHEMA`; no row data leaves the DB |
| **Aggregate column statistics** — row counts, null %, cardinality, numeric min/max | On | Aggregates computed by the DB itself; only the result leaves |
| **`top_values`** (column-level most-common values) | **STRIPPED** before reaching S3 | Treated as data leakage, not statistics. Stripped twice (sync-side and file-ingestion-side defensively) |
| **Row sampling** | Off | Admin opt-in per-table. Comprehend PII redaction runs at ingest if sampling is enabled |

The strip-and-default-off posture exists for regulated-buyer privacy
defaults: for a column called `patient_diagnosis`, the top 10 values
*are* the data. Even when "aggregated by frequency," they expose
PHI in a way that aggregate statistics like null-percentage don't.

## Deploying the integration (operator)

This section is for the operator deploying OutcomeOps into their AWS
account — not the workspace admin connecting an individual DB later.

The integration ships as two **Lambda container images** (not a Lambda
layer — Databao + DB drivers total over 350 MB unzipped, well past
Lambda's 250 MB unzipped layer cap; container images allow up to 10 GB):

- `database-integration` — UI-facing picker / connection CRUD
- `database-sync` — SQS-driven Databao introspection worker

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
- `lambda/databao-requirements.txt` changes (Databao release bump, DB driver bump)
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

The **Test connection** button runs Databao's
`check_datasource_connection` against your DB **without persisting
the credentials**, so you get immediate feedback if the creds are
bad before you save.

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

## Configuring scope (which tables to ingest)

After the connection is created it lands in `needs_configuration`
status. Click **Configure** to open the schema/table picker.

For each table you can independently toggle:

- **Introspect**: pull the schema + aggregate stats for this table. Safe.
- **Sample rows**: also pull example rows for the LLM to learn from.
  Off by default. Comprehend PII redaction runs at ingest if you
  enable it, but the regulated-buyer-friendly default is to leave
  this off.

Save → the connection moves to `queued`, and the next scheduler tick
(hourly) or **Sync now** kicks off introspection.

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
  - validates connection (Databao check_datasource_connection)
  - encrypts credentials with KMS, stores DB_CREDENTIAL# in DDB
  - emits DB_CONNECTION_ESTABLISHED audit row
  - returns 200; UI shows the connection card
     |
     v
Admin selects schemas/tables -> Save
  - update_scope() writes scope_config to DDB
  - queues sync to DATABASE_SYNC_QUEUE
     |
     v
database-sync Lambda (also fired hourly by database-scheduler)
  - license-validate (ADR-019)
  - decrypts credential -> emits DB_CREDENTIAL_USED audit row
  - check_repo_limit("database:{engine}:{host_hash}")
  - calls Databao build_context() -> YAML artifact
  - STRIPS top_values from YAML in-memory
  - queues to FILE_INGESTION_QUEUE (one message per table)
  - emits DATABASE_SYNCED audit row
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

## Audit events emitted

| Action | Fires when | Volume |
|---|---|---|
| `DB_CONNECTION_ESTABLISHED` | Admin connects a DB | One per connect |
| `DB_CREDENTIAL_USED` | Sync decrypts credential to introspect | One per connection per scheduler tick |
| `DATABASE_SYNCED` | Successful introspection completed | One per successful sync |
| `DB_CONNECTION_DELETED` | Admin disconnects | One per disconnect |

`DB_CREDENTIAL_USED` is hidden from dashboard cards (high-frequency)
but is queryable via `?action=DB_CREDENTIAL_USED` in the audit API.

## Roadmap

Demand-driven follow-ups, not "future" hand-waving — each maps to a
specific customer ask we're tracking:

- **Oracle support** — Databao upstream gap; HCLS / financial-services demand.
- **SAP HANA support** — Databao upstream gap; named lost-deal signal.
- **BigQuery + Snowflake** — supported by Databao but lower priority for the regulated ICP; add when a customer hits this gap.
- **Row sampling default-on with admin per-table opt-in UX** — already wired technically, just default-off in MVP. Enable selectively for customers comfortable with the redaction story.
- **Live SQL execution via the chat Lambda (Databao's MCP server)** — the big enterprise unlock, but requires us to own a per-workspace query-governance layer (column-level masking, query allow-lists, per-user query audit, generated-SQL audit trail). **Will not ship until a lighthouse regulated customer co-designs the governance with us.**

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| **"Connection test failed"** at create time | Bad credentials, unreachable host, or firewall block | Verify the username/password against the DB directly. Check that the OutcomeOps Lambda VPC can route to your DB host (VPC peering / SG allowlist / etc.) |
| **`auth_error`** on a connection after some time | DB password rotated, user revoked, or SSL cert changed | Workspace admin disconnects and reconnects with the new creds. (DB-side auth has no refresh-token equivalent — manual rotation is the path.) |
| **`blocked_license_limit`** status | Hitting the repo-limit for your OutcomeOps tier | Same as any other source — increase your tier or trim connections. Repo identity is `{engine}:{host_hash}` so the same physical DB across multiple workspace connections counts ONCE |
| **Schema picker is slow / empty** | Large warehouse, slow `INFORMATION_SCHEMA` introspection, or the connection user lacks `SELECT` on the schemas | Confirm the user has the right grants. For very large warehouses (10k+ tables), a DDB-cached schema tree is on the roadmap |
| **Connection card returns "Server misconfiguration: Databao layer missing"** | Container image wasn't built (or not yet pushed) before terraform apply | Run `ENVIRONMENT=prd make build-database-images` then `terraform apply` |

## Related

- [`docs/azure-ad-m365-integrations.md`](./azure-ad-m365-integrations.md) — M365 source setup (OneDrive, SharePoint, Outlook, Teams, OneNote)
- ADR-019 — license-layer rollout (database-sync is license-gated like every other sync)
- ADR-018 — audit logging (every DB integration event emits an audit row)
