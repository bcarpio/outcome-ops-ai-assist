# Database Integration — Creating a Read-Only User

OutcomeOps's database integration only needs `SELECT` privileges on the
schemas you want to expose to the AI assistant. Strongly recommended:
create a dedicated read-only user rather than reusing a DB admin or
application user.

This doc has the per-engine SQL, plus the engine-specific quirks that
have tripped people up.

## What the integration produces, by engine

Different engines support different levels of context fidelity. Plan
your customer expectations accordingly:

| Capability | Postgres | MySQL | MS SQL Server |
|---|---|---|---|
| Catalog / schema / table tree | ✓ | ✓ | ✓ |
| Column metadata (name, type, nullable, default, identity, generated) | ✓ | ✓ | ✓ |
| Primary keys, unique constraints, foreign keys, indexes | ✓ | ✓ | ✓ |
| Sample rows (when admin opts in per-schema) | ✓ | ✓ | ✓ |
| Table-level row counts | ✓ | ✓ | — |
| Per-column aggregate stats (null counts, distinct counts, min/max, cardinality buckets) | ✓ partial¹ | ✓ | — |

¹ Postgres reads from `pg_stats`, which is populated by `ANALYZE` /
autovacuum. Stats coverage depends on how fresh those have run.
Histogram-based min/max are populated for ordered types (numeric, date)
but not for strings on small tables.

**The stats gap on MSSQL is an upstream plugin limitation,** not a
permission problem. The Databao MSSQL plugin doesn't implement the
`collect_stats` path that Postgres and MySQL do, so no amount of
elevated grants will populate the stats columns. Customers who need
MSSQL stats parity should raise it upstream (or with us — tracked as
a v2 follow-up).

## Postgres (incl. Aurora Postgres, RDS Postgres, Redshift)

```sql
-- 1. Create the user
CREATE USER outcomeops_ro WITH PASSWORD '<paste-secure-password-here>';

-- 2. Allow connecting to the database
GRANT CONNECT ON DATABASE analytics TO outcomeops_ro;

-- 3. For each schema you want ingested:
GRANT USAGE ON SCHEMA healthcare TO outcomeops_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA healthcare TO outcomeops_ro;

-- 4. So future tables created in that schema also get SELECT automatically:
ALTER DEFAULT PRIVILEGES IN SCHEMA healthcare
    GRANT SELECT ON TABLES TO outcomeops_ro;
```

Repeat steps 3-4 for each schema you want ingested.

**Postgres-specific notes:**

- The connection form's "Database" field is the Postgres database. The
  introspector then enumerates schemas within that database — it does
  NOT cross-connect to other Postgres databases on the same cluster.
  Each Postgres database = one OutcomeOps connection.
- `pg_stats` is a system view that auto-filters rows by what the
  current role has SELECT on. No additional grant is needed to read
  it; the SELECT grants in step 3 cover it.
- For complete stats, run `ANALYZE` on the schemas after the grants
  land (or wait for autovacuum to do it). Empty stats usually mean
  the tables haven't been analyzed.

```sql
ANALYZE healthcare.patients;
-- ...or schema-wide:
ANALYZE healthcare;
```

## MySQL (incl. Aurora MySQL, RDS MySQL, MariaDB)

**MySQL has no sub-schemas:** each "database" in MySQL is a top-level
container; there are no schemas within databases the way Postgres and
MSSQL have them. If your data lives in multiple databases, grant each
one separately:

```sql
-- 1. Create the user
CREATE USER 'outcomeops_ro'@'%' IDENTIFIED BY '<paste-secure-password-here>';

-- 2. SELECT on each database you want ingested:
GRANT SELECT ON healthcare.* TO 'outcomeops_ro'@'%';
GRANT SELECT ON billing.*    TO 'outcomeops_ro'@'%';

-- 3. Apply
FLUSH PRIVILEGES;
```

**MySQL-specific notes:**

- The connection form's "Database" field is the initial connect
  target. The introspector then enumerates ALL databases visible to
  the connected user via `information_schema.SCHEMATA`. So you can
  set it to `healthcare` (or any granted DB) and the picker will
  still surface every DB the user has SELECT on.
- **`ANALYZE TABLE` needs INSERT, not just SELECT.** MySQL 8's
  `ANALYZE TABLE` (which the upstream introspector runs on every
  sync to refresh column stats and histograms) requires both
  `SELECT` and `INSERT` privileges on the target table, even though
  it doesn't actually insert row data. Without `INSERT`, sync logs
  a harmless `Failed to analyze table ... INSERT command denied`
  warning per table per sync and falls back to whatever stats
  `innodb_stats_auto_recalc` already cached. Two ways to handle:
  ```sql
  -- Option A: grant INSERT on the user-data schemas. The
  -- introspector never actually writes row data; INSERT is required
  -- only for ANALYZE TABLE's privilege check. Trade-off: a
  -- compromised RO credential could now INSERT fake rows.
  GRANT SELECT, INSERT ON healthcare.* TO 'outcomeops_ro'@'%';

  -- Option B: keep SELECT-only. ANALYZE TABLE fails (warning
  -- logged, not error). Stats still come from
  -- innodb_stats_auto_recalc's background refresh, which is usually
  -- fresh enough but lags moments after schema changes.
  ```
  RDS MySQL also blocks `GRANT ... ON mysql.column_stats` even from
  the admin user, so the histogram-table grant some upstream docs
  recommend isn't reachable on RDS — but it's not strictly necessary
  either; the source-table INSERT is the blocking permission.
- For MySQL 8+ with `caching_sha2_password` plugin issues, fall back
  to `mysql_native_password`:
  ```sql
  ALTER USER 'outcomeops_ro'@'%' IDENTIFIED WITH mysql_native_password BY '<password>';
  ```

## MS SQL Server (incl. Azure SQL, RDS for SQL Server)

```sql
-- 1. Create a login at the server level
CREATE LOGIN outcomeops_ro WITH PASSWORD = '<paste-secure-password-here>';

-- 2. In the database you want to expose:
USE analytics;
CREATE USER outcomeops_ro FOR LOGIN outcomeops_ro;

-- 3. Grant read-only role
ALTER ROLE db_datareader ADD MEMBER outcomeops_ro;
```

For Azure SQL the contained-user variant works too if you prefer to
skip the server-level login step:

```sql
USE analytics;
CREATE USER outcomeops_ro WITH PASSWORD = '<paste-secure-password-here>';
ALTER ROLE db_datareader ADD MEMBER outcomeops_ro;
```

**MS SQL Server-specific notes:**

- `db_datareader` membership is sufficient. It grants SELECT on every
  table + view in the database AND read access to the `sys.*` catalog
  views the introspector needs (`sys.tables`, `sys.schemas`,
  `sys.columns`, `sys.indexes`, `sys.foreign_keys`, etc.).
- The "Database" field in the connection form is the catalog you want
  to expose. Unlike MySQL, the MSSQL introspector does NOT enumerate
  other catalogs on the server — one connection = one catalog.
- **"Login failed for user" can mean "wrong database name"**. MSSQL
  hides "database doesn't exist" behind the same `Login failed for
  user 'X'` string it uses for wrong passwords, to prevent
  information disclosure about which databases exist. If you've
  double-checked the password and still see this error, double-check
  the database name in the connection form too. We're tracking a
  fix to disambiguate this in the platform UI; until then, the
  workaround is connect as the same user via `sqlcmd` with
  `-d <DBname>` and verify the name is correct.
- **Per-column aggregate stats are not collected on MSSQL** because
  the upstream plugin doesn't implement them. Schema, samples,
  constraints, indexes all work; the stats columns will come back
  as `null`. This is independent of grants — no permission level
  unlocks them. Tracked as a v2 follow-up.

## Adding customer trust roots (non-RDS / on-prem)

The OutcomeOps database container images bake the **AWS RDS global
CA bundle** into the system trust store by default — Postgres /
MySQL / MSSQL on Amazon RDS or Aurora "just work" with `Encrypt=yes`
and full cert validation.

For DBs **not behind the RDS bundle** — Azure SQL signed by a
corporate root, on-prem MSSQL signed by an internal CA, GCP Cloud
SQL for MSSQL with a custom chain — you need to register the
trust root(s) so the TLS handshake validates. Otherwise the
introspector fails with `unable to get local issuer certificate`
(specifically on MSSQL — mssql-python's ODBC Driver 18 ships its
own trust path and doesn't honor `SSL_CERT_FILE`).

### When you need this

- Azure SQL backed by your org's internal CA chain.
- On-prem MSSQL signed by your internal CA.
- GCP Cloud SQL for MSSQL with a non-public chain.
- Any DB whose server cert isn't already in the AWS RDS bundle or
  the standard public-root store baked into the Lambda image.

You do NOT need this for RDS / Aurora — those work out of the box.

### Upload the PEM to SSM Parameter Store as a SecureString

```bash
aws ssm put-parameter \
  --name /outcomeops/ca-bundles/corp-root \
  --type SecureString \
  --value file://corp-root.pem
```

The `file://` prefix tells the CLI to read PEM contents from disk.
SSM SecureString encrypts at rest with KMS (the AWS-managed
`alias/aws/ssm` key by default; pass `--key-id <your-cmk-id>` to
use a customer-managed key).

The bundle must be a **single PEM-encoded X.509 cert** (root or
intermediate). For multi-cert chains, either:
- Concatenate them into one **SSM Advanced** parameter (8 KB cap), or
- Split across multiple **SSM Standard** parameters (4 KB cap each).

Build-time validation runs `openssl x509 -noout -dates` on each
parameter; a malformed PEM hard-fails the image build with the
offending ARN named in the build log, so customers see breakage
at build time rather than at first TLS handshake.

### Register the parameter ARNs in tfvars

```hcl
# terraform/prd.tfvars
customer_ca_bundle_ssm_param_arns = [
  "arn:aws:ssm:us-west-2:123456789012:parameter/outcomeops/ca-bundles/corp-root",
  "arn:aws:ssm:us-west-2:123456789012:parameter/outcomeops/ca-bundles/azure-sql-intermediate",
]
```

The `scripts/build-database-images.sh` script fetches each parameter
at build time, validates the PEM, and bakes the bundles into both
the `database-integration` and `database-sync` image trust stores
(`/etc/pki/ca-trust/source/anchors/` → `update-ca-trust`).

### Customer-managed KMS keys

If you encrypted the SecureString with a customer-managed KMS key
(`--key-id arn:aws:kms:...:key/...`), grant the deploy role (the
IAM principal that runs `make build-database-images`) `kms:Decrypt`
on that key in addition to the standard `ssm:GetParameter`. The
AWS-managed `alias/aws/ssm` key doesn't need an explicit grant —
SSM authorizes via the parameter ARN alone.

### Rotation workflow

When a CA rotates upstream, the path is:

1. Update the SecureString with the new PEM:
   ```bash
   aws ssm put-parameter \
     --name /outcomeops/ca-bundles/corp-root \
     --type SecureString \
     --value file://corp-root-new.pem \
     --overwrite
   ```
2. Rebuild the database container images so the new bundle gets
   baked in:
   ```bash
   ENVIRONMENT=prd make build-database-images
   ```
3. `terraform apply` to point the Lambdas at the new image tag.

**Bake-time rebuild is required** because mssql-python's ODBC
Driver 18 reads the system trust store at a fixed path inside the
container — it doesn't honor `SSL_CERT_FILE` or pick up
runtime-mounted certs. Updating the SecureString alone won't
propagate to a running Lambda; the image needs to be rebuilt.

### What goes wrong if skipped

Connect / Test connection / Sync against the affected DB returns:

```
unable to get local issuer certificate
```

(MSSQL surfaces this specifically; Postgres and MySQL drivers
respect `SSL_CERT_FILE`, so the same DB hosted on Postgres/MySQL
backed by a corporate CA also needs the SSM bundle path even
though the error wording differs.)

The picker's structured 502 (see the troubleshooting table in
[`database-integration.md`](./database-integration.md#troubleshooting))
will carry this string in `error` so admins know they need to
register the trust root.

## Verifying

After creating the user, run a quick `SELECT 1` connection check as
that user from `psql` / `mysql` / `sqlcmd` before plugging the
credentials into OutcomeOps. If it works there, the **Test connection**
button in the UI will work too.

For MySQL specifically, also confirm what databases the user can see:

```sql
-- as outcomeops_ro
SHOW DATABASES;
```

The list should match what you intend the picker to surface.

## What the introspector actually queries (security-review reference)

For DBAs who need to confirm the integration's blast radius before
granting credentials:

- **Postgres**: `pg_class`, `pg_namespace`, `pg_attribute`, `pg_type`,
  `pg_stats` (filtered by SELECT privilege), `pg_inherits`,
  `information_schema.schemata`, `information_schema.tables`,
  `information_schema.columns`, plus `SELECT ... LIMIT 5` against
  every auto-discovered table inside a sampling-enabled schema
  (only when that schema is in `scope_config.sampling.include`).
- **MySQL**: `INFORMATION_SCHEMA.SCHEMATA`, `.TABLES`, `.COLUMNS`,
  `.STATISTICS`, `.KEY_COLUMN_USAGE`, `.REFERENTIAL_CONSTRAINTS`,
  plus `SELECT ... LIMIT 5` against every auto-discovered table
  inside a sampling-enabled schema, plus
  `ANALYZE TABLE ... UPDATE HISTOGRAM` (gracefully falls back on
  permission error).
- **MS SQL Server**: `sys.databases`, `sys.tables`, `sys.views`,
  `sys.schemas`, `sys.columns`, `sys.types`, `sys.indexes`,
  `sys.key_constraints`, `sys.foreign_keys`, `sys.extended_properties`,
  `sys.computed_columns`, `sys.default_constraints`, plus
  `SELECT TOP 5 ...` against every auto-discovered table inside a
  sampling-enabled schema.

No writes are issued except the MySQL `ANALYZE TABLE` above, which is
a no-op stats refresh (does not modify table data).

## Rotating credentials

When you rotate the password DB-side, click **Disconnect** then
**Connect again** in OutcomeOps with the new password. DB integrations
don't have an OAuth-refresh equivalent — manual reconnect is the path.

The previously synced schema metadata stays in your knowledge base
across the reconnect (delete-worker is only triggered on Disconnect
of the matching connection record).

## Why not just use the DB admin?

Two reasons:
1. **Least privilege.** OutcomeOps reads schema + aggregates. A
   compromised credential should not grant write / DDL anywhere.
2. **Audit clarity.** When `DB_CREDENTIAL_USED` audit rows fire, you
   want them to attribute to a dedicated `outcomeops_ro` identity in
   your DB audit logs, not blended with admin activity.
