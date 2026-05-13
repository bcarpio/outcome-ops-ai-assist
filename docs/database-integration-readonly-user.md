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
| Sample rows (when admin opts in per-table) | ✓ | ✓ | ✓ |
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
- For full per-column aggregate stats, the introspector runs
  `ANALYZE TABLE ... UPDATE HISTOGRAM`. Plain `SELECT` is enough for
  this to work on InnoDB with `innodb_stats_auto_recalc=ON` (default).
  If you want bulletproof stats parity, also grant `INSERT, UPDATE`
  on `mysql.column_stats`:
  ```sql
  -- Optional: enables ANALYZE UPDATE HISTOGRAM by the RO user
  GRANT INSERT, UPDATE, DELETE ON mysql.column_stats TO 'outcomeops_ro'@'%';
  ```
  Most customers won't need this; InnoDB's automatic stats are usually
  fresh enough.
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
  each picked table (only when sampling is enabled per-table).
- **MySQL**: `INFORMATION_SCHEMA.SCHEMATA`, `.TABLES`, `.COLUMNS`,
  `.STATISTICS`, `.KEY_COLUMN_USAGE`, `.REFERENTIAL_CONSTRAINTS`,
  plus `SELECT ... LIMIT 5` against each picked table (when
  sampling is enabled), plus `ANALYZE TABLE ... UPDATE HISTOGRAM`
  (gracefully falls back on permission error).
- **MS SQL Server**: `sys.databases`, `sys.tables`, `sys.views`,
  `sys.schemas`, `sys.columns`, `sys.types`, `sys.indexes`,
  `sys.key_constraints`, `sys.foreign_keys`, `sys.extended_properties`,
  `sys.computed_columns`, `sys.default_constraints`, plus
  `SELECT TOP 5 ...` against each picked table (when sampling).

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
