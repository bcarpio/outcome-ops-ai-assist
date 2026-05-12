# Database Integration — Creating a Read-Only User

OutcomeOps's database integration only needs `SELECT` privileges on the
schemas you want to expose to the AI assistant. Strongly recommended:
create a dedicated read-only user rather than reusing a DB admin or
application user.

This doc has the per-engine SQL.

## Postgres (incl. Aurora Postgres, RDS Postgres, Redshift)

```sql
-- 1. Create the user
CREATE USER outcomeops_ro WITH PASSWORD '<paste-secure-password-here>';

-- 2. Allow connecting to the database
GRANT CONNECT ON DATABASE analytics TO outcomeops_ro;

-- 3. For each schema you want ingested:
GRANT USAGE ON SCHEMA public TO outcomeops_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO outcomeops_ro;

-- 4. So future tables created in that schema also get SELECT automatically:
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT ON TABLES TO outcomeops_ro;
```

Repeat steps 3-4 for each schema you want ingested.

## MySQL (incl. Aurora MySQL, RDS MySQL, MariaDB)

```sql
-- 1. Create the user
CREATE USER 'outcomeops_ro'@'%' IDENTIFIED BY '<paste-secure-password-here>';

-- 2. Read-only on a specific database:
GRANT SELECT ON analytics.* TO 'outcomeops_ro'@'%';

-- 3. Apply
FLUSH PRIVILEGES;
```

For MySQL 8+ with `caching_sha2_password` plugin issues, you can fall
back to `mysql_native_password`:

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

## Verifying

After creating the user, run a quick `SELECT 1` connection check as
that user from `psql` / `mysql` / `sqlcmd` before plugging the
credentials into OutcomeOps. If it works there, the **Test connection**
button in the UI will work too.

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
