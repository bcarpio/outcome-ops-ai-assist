"""
Database Sync Lambda for OutcomeOps AI Assist.

SQS-triggered worker that drives schema introspection against a
customer database connection, strips top_values from the produced
artifact, and queues per-table chunks to file-ingestion.

Flow per SQS record:
  1. License-validate (ADR-019). Raise on failure -> SQS retry / DLQ.
  2. Fetch DATABASE_CONNECTION#{conn_id} from DDB.
  3. Decrypt the DB credential, emit DB_CREDENTIAL_USED audit row.
  4. check_repo_limit("database:{engine}:{host_hash}") - repo identity
     uses engine+host_hash so the same physical DB across many
     workspace connections counts once.
  5. Call the engine's introspector.introspect_database() directly
     (bypassing build_context to avoid the upstream's Ollama runtime
     install on Lambda).
  6. For each in-scope table: serialize to dict, strip top_values,
     wrap in the per-table YAML envelope, queue to FILE_INGESTION_QUEUE.
  7. Update connection status to completed, write known_table_ids.
  8. Queue orphan_cleanup to delete-worker.
  9. Emit DATABASE_SYNCED audit row.

The top_values strip is duplicated defensively in file-ingestion so a
regression here can't cause column values to land in S3.
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import (  # noqa: E402
    ACTION_DB_CREDENTIAL_USED,
    log_admin_action,
    log_license_fail_open,
    log_source_sync,
)

from credential_manager import get_credential

# License layer imports (ADR-019)
try:
    from outcomeops_license import (
        check_repo_limit,
        track_repo_active,
        validate_license,
    )
except ImportError:
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]
    track_repo_active = None  # type: ignore[assignment]

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
DATABASE_SYNC_QUEUE = os.environ.get("DATABASE_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

dynamodb = boto3.resource("dynamodb")
sqs_client = boto3.client("sqs")


def sanitize_filename(name: str) -> str:
    """Mirror file-ingestion.sanitize_filename exactly. Used to produce
    the table_id form that ends up as the S3 key filename stem so
    delete-worker's orphan-membership check works with a direct match.
    Same helper as onenote-sync."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:200]


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    table = dynamodb.Table(WORKSPACES_TABLE)
    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError as e:
        logger.error(f"Failed to get connection: {e}")
        return None


def update_connection_status(
    workspace_id: str,
    connection_id: str,
    status: str,
    tables_count: int = 0,
    known_table_ids: Optional[List[str]] = None,
    error: Optional[str] = None,
):
    table = dynamodb.Table(WORKSPACES_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    update_expr = "SET last_sync_status = :status, updated_at = :now"
    expr_values: Dict[str, Any] = {":status": status, ":now": now}

    if status in ("completed", "error", "auth_error", "blocked_license_limit"):
        update_expr += ", last_sync_at = :sync_at"
        expr_values[":sync_at"] = now

    if tables_count > 0:
        update_expr += ", tables_count = :tc"
        expr_values[":tc"] = tables_count

    if known_table_ids is not None:
        update_expr += ", known_table_ids = :kti"
        expr_values[":kti"] = known_table_ids

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def strip_top_values(doc: Any) -> Any:
    """Recursively remove any `top_values` key from a parsed YAML doc.

    Top values for columns are data leaving the DB (just aggregated by
    frequency). Defensible "statistics" framing from the upstream engine,
    but for regulated-buyer defaults we treat them as the same risk
    class as row sampling — strip before the artifact crosses the SQS
    boundary.

    file-ingestion runs this same pass defensively. Both call sites
    share this implementation via duplicated copy (lambdas can't
    import each other), kept in sync by tests.
    """
    if isinstance(doc, dict):
        doc.pop("top_values", None)
        for v in doc.values():
            strip_top_values(v)
    elif isinstance(doc, list):
        for item in doc:
            strip_top_values(item)
    return doc


def queue_table_ingestion(
    workspace_id: str,
    connection_id: str,
    engine: str,
    host_hash: str,
    catalog: str,
    schema: str,
    table: str,
    table_yaml: str,
    sync_id: str,
):
    """Queue one table's YAML chunk to file-ingestion.

    table_id form is `{catalog}.{schema}.{table}` sanitized — matches
    what update_connection_status writes to known_table_ids so
    delete-worker's orphan check is a direct stem match.
    """
    table_id_raw = f"{catalog}.{schema}.{table}"
    table_id_safe = sanitize_filename(table_id_raw)

    msg = {
        "source": "database",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "engine": engine,
            "host_hash": host_hash,
            "catalog": catalog,
            "schema": schema,
            "table": table,
            "table_id": table_id_safe,
            "yaml_content": table_yaml,
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=(
                f"{sync_id}-tbl-"
                f"{hashlib.sha256(table_id_raw.encode()).hexdigest()[:32]}"
            ),
        )
    except ClientError as e:
        logger.error(f"Failed to queue table {table_id_raw}: {e}")


def queue_orphan_cleanup(
    workspace_id: str,
    connection_id: str,
    current_table_ids: List[str],
) -> None:
    """Queue delete_orphans for any S3 artifact whose table_id is no
    longer in scope. Defensive: skip if current_table_ids is empty
    (would wipe everything)."""
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured; skipping database orphan cleanup")
        return
    if not current_table_ids:
        logger.warning(
            f"[database-sync] Empty current_table_ids for "
            f"{workspace_id}/{connection_id}; skipping orphan cleanup"
        )
        return

    payload = {
        "action": "delete_orphans",
        "workspace_id": workspace_id,
        "source": "database",
        "prefix": f"{connection_id}/",
        "current_ids": current_table_ids,
    }
    try:
        sqs_client.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(payload),
        )
        logger.info(
            f"[database-sync] Queued orphan cleanup for "
            f"{workspace_id}/database/{connection_id} "
            f"(current={len(current_table_ids)} table_ids)"
        )
    except ClientError as e:
        logger.error(f"Failed to queue database orphan cleanup: {e}")


def _emit_credential_used_audit(
    workspace_id: str,
    connection_id: str,
    engine: str,
    host_hash: str,
    database_name: str,
):
    """Forensic audit row: sync just decrypted a credential to read
    from a customer DB. Required for the unified-audit-table value
    prop (no events get filed away in CloudTrail only)."""
    try:
        log_admin_action(
            os.environ.get("AUDIT_TABLE", ""),
            ACTION_DB_CREDENTIAL_USED,
            workspace_id=workspace_id,
            user_email="system:database-sync",
            source_ip="",
            user_agent="",
            details={
                "provider": "database",
                "connection_id": connection_id,
                "engine": engine,
                "host_hash": host_hash,
                "database_name": database_name,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit DB_CREDENTIAL_USED audit row: {e}")


def _build_sync_config(
    dce,
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential: Dict[str, Any],
    schemas_by_catalog: Dict[str, set],
    sampling_enabled: bool,
):
    """Engine-specific ConfigFile for the full sync build.

    Mirrors database-integration/introspection_helpers._build_config but
    adds IntrospectionScope (limit the engine to the admin-selected
    schemas) and honors the workspace's sampling toggle. Duplicated
    rather than shared because the two Lambdas can't import each
    other - kept in sync by tests that exercise both call sites.

    # DATABAO-SPECIFIC: this upstream library defaults
    # SamplingConfig.enabled=True; we force it off unless the admin
    # opted in via scope_config. Same posture as
    # introspection_helpers._build_config.
    """
    from databao_context_engine.plugins.databases.sampling_scope import SamplingConfig
    from databao_context_engine.plugins.databases.introspection_scope import (
        IntrospectionScope, ScopeIncludeRule,
    )
    from databao_context_engine.plugins.databases.profiling_config import ProfilingConfig

    user = credential.get("username")
    password = credential.get("password")
    sampling = SamplingConfig(enabled=bool(sampling_enabled))
    # We always want aggregate column stats - admin opt-in to sampling
    # is the separate toggle above.
    profiling = ProfilingConfig(enabled=True)

    # Translate admin's "catalog.schema.table" picker selection into
    # the engine's schema-level scope.
    # DATABAO-SPECIFIC: the upstream engine's introspection scope is
    # schema-level while our admin picker is table-level. The per-table
    # filter is applied post-introspect in _run_schema_introspection
    # against the include_table_keys set.
    include_rules = []
    for catalog, schemas in (schemas_by_catalog or {}).items():
        include_rules.append(
            ScopeIncludeRule(catalog=catalog, schemas=sorted(schemas))
        )
    introspection_scope = IntrospectionScope(include=include_rules) if include_rules else None

    if engine == "postgres":
        from databao_context_engine.plugins.databases.postgresql.config_file import (
            PostgresConfigFile, PostgresConnectionProperties,
        )
        additional = {}
        ssl_mode = credential.get("ssl_mode")
        if ssl_mode and ssl_mode != "disable":
            additional["ssl"] = ssl_mode
        return PostgresConfigFile(
            name="ooat-sync",
            connection=PostgresConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                additional_properties=additional,
            ),
            sampling=sampling,
            profiling=profiling,
            introspection_scope=introspection_scope,
        )

    if engine == "mysql":
        from databao_context_engine.plugins.databases.mysql.config_file import (
            MySQLConfigFile, MySQLConnectionProperties,
        )
        additional = {}
        ssl_mode = credential.get("ssl_mode")
        if ssl_mode and ssl_mode != "disable":
            additional["ssl"] = {}
        return MySQLConfigFile(
            name="ooat-sync",
            connection=MySQLConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                additional_properties=additional,
            ),
            sampling=sampling,
            profiling=profiling,
            introspection_scope=introspection_scope,
        )

    if engine == "mssql":
        from databao_context_engine.plugins.databases.mssql.config_file import (
            MSSQLConfigFile, MSSQLConnectionProperties,
        )
        encrypt_value = "yes" if credential.get("encrypt", True) else "no"
        additional = {}
        if credential.get("trust_server_certificate"):
            additional["trust_server_certificate"] = True
        return MSSQLConfigFile(
            name="ooat-sync",
            connection=MSSQLConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                encrypt=encrypt_value,
                additional_properties=additional,
            ),
            sampling=sampling,
            profiling=profiling,
            introspection_scope=introspection_scope,
        )

    raise ValueError(f"Unsupported engine: {engine!r}")


def _detect_top_values(doc: Any) -> bool:
    """Walk a parsed YAML doc and return True if any node has 'top_values'."""
    if isinstance(doc, dict):
        if "top_values" in doc:
            return True
        return any(_detect_top_values(v) for v in doc.values())
    if isinstance(doc, list):
        return any(_detect_top_values(item) for item in doc)
    return False


def _to_yaml_safe(obj: Any) -> Any:
    """Recursively convert a dataclass/enum tree into YAML-dumpable primitives.

    dataclasses.asdict() doesn't unwrap enums (DatasetKind etc.); this
    walker does, while preserving the nested structure so the resulting
    dict round-trips cleanly through yaml.safe_dump.
    """
    from dataclasses import is_dataclass, fields
    from enum import Enum

    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if is_dataclass(obj):
        return {f.name: _to_yaml_safe(getattr(obj, f.name)) for f in fields(obj)}
    if isinstance(obj, list):
        return [_to_yaml_safe(x) for x in obj]
    if isinstance(obj, tuple):
        return [_to_yaml_safe(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _to_yaml_safe(v) for k, v in obj.items()}
    return obj


def _get_introspector_for_sync(engine: str):
    """Return a configured upstream introspector for the given engine.

    Sibling of database-integration/introspection_helpers._get_introspector
    — duplicated rather than shared because the two Lambdas can't import
    each other (per the comment on _build_sync_config). Behavior must
    match; tests exercise both call sites against the same fixture
    shape.

    Bypasses the upstream `build_context` flow which on Lambda triggers
    an Ollama runtime tarball download that exhausts /tmp.
    """
    if engine == "postgres":
        from databao_context_engine.plugins.databases.postgresql.postgresql_connector import (
            PostgresqlConnector,
        )
        from databao_context_engine.plugins.databases.postgresql.postgresql_introspector import (
            PostgresqlIntrospector,
        )
        return PostgresqlIntrospector(PostgresqlConnector())

    if engine == "mysql":
        from databao_context_engine.plugins.databases.mysql.mysql_connector import (
            MySQLConnector,
        )
        from databao_context_engine.plugins.databases.mysql.mysql_introspector import (
            MySQLIntrospector,
        )
        return MySQLIntrospector(MySQLConnector())

    if engine == "mssql":
        # Use the local subclass that adds the missing collect_stats
        # override. The upstream MSSQLIntrospector inherits a no-op
        # collect_stats from BaseIntrospector, which is why MSSQL
        # tables/columns come back with stats: null. Retire this
        # branch (back to plain MSSQLIntrospector) when JetBrains
        # ships their own collect_stats upstream.
        from shared.mssql_stats import get_outcomeops_mssql_introspector
        return get_outcomeops_mssql_introspector()

    raise ValueError(f"Unsupported engine: {engine!r}")


def _build_table_chunk_yaml(
    table_obj: Any,
    catalog_name: str,
    schema_name: str,
    engine: str,
    datasource_id: str,
) -> tuple[str, int, bool]:
    """Serialize one DatabaseTable to the per-table YAML chunk file-ingestion
    consumes. Returns (yaml_string, column_count, had_top_values).

    The envelope (datasource_id / datasource_type / context.catalogs[]/...)
    is preserved verbatim from the build_context-era format so downstream
    consumers (file-ingestion + future chunking/embedding) don't have
    to change shape.
    """
    import yaml

    table_dict = _to_yaml_safe(table_obj)
    column_count = len(table_dict.get("columns") or [])
    had_top_values = _detect_top_values(table_dict)
    strip_top_values(table_dict)

    chunk_doc = {
        "datasource_id": datasource_id,
        "datasource_type": engine,
        "context": {
            "catalogs": [{
                "name": catalog_name,
                "schemas": [{
                    "name": schema_name,
                    "tables": [table_dict],
                }],
            }],
        },
    }
    return yaml.safe_dump(chunk_doc, sort_keys=False), column_count, had_top_values


def _run_schema_introspection(connection: Dict, credential: Dict) -> Dict[str, Any]:
    """Run schema introspection against the customer DB and emit per-table
    YAML chunks.

    Returns a dict with keys:
      - "tables": list of per-table dicts {catalog, schema, table, yaml}
      - "columns_count": total columns introspected
      - "had_top_values": bool - informational; the YAML in "tables" is
        already stripped of top_values

    Drives the upstream engine's introspector.introspect_database()
    directly. The full build_context flow instantiates an Ollama LLM
    service even with should_enrich_context=False, which on Lambda
    triggers a runtime tarball download that exhausts /tmp. The
    lower-level introspector API walks catalogs/schemas/tables via
    plain SQL — no DuckDB, no LLM, no /tmp downloads.
    """
    # DATABAO-SPECIFIC: keep the upstream package name in the log line
    # below so upstream-issue triage stays fast.
    try:
        import databao_context_engine as dce
    except ImportError:
        logger.error(
            "[database-sync] databao_context_engine not importable - "
            "container image build is broken (Databao should be baked in)"
        )
        return {"tables": [], "columns_count": 0, "had_top_values": False}

    engine = connection.get("engine", "")
    host = connection.get("host", "")
    port = int(connection.get("port") or 0)
    database_name = connection.get("database_name", "")
    connection_id = connection.get("connection_id", "")
    scope_config = connection.get("scope_config") or {}

    include_strs = (scope_config.get("introspection") or {}).get("include") or []
    if not include_strs:
        logger.info("[database-sync] No tables in scope.introspection.include; skipping build")
        return {"tables": [], "columns_count": 0, "had_top_values": False}

    include_table_keys = set(include_strs)
    schemas_by_catalog: Dict[str, set] = {}
    for key in include_strs:
        parts = key.split(".", 2)
        if len(parts) != 3:
            logger.warning(f"[database-sync] Malformed include key (skipped): {key!r}")
            continue
        cat, sch, _tbl = parts
        schemas_by_catalog.setdefault(cat, set()).add(sch)

    sampling_enabled = bool((scope_config.get("sampling") or {}).get("enabled"))

    try:
        config = _build_sync_config(
            dce, engine, host, port, database_name, credential,
            schemas_by_catalog=schemas_by_catalog,
            sampling_enabled=sampling_enabled,
        )
    except (ValueError, ImportError) as e:
        logger.error(f"[database-sync] Config build failed: {e}")
        return {"tables": [], "columns_count": 0, "had_top_values": False}

    try:
        introspector = _get_introspector_for_sync(engine)
        result = introspector.introspect_database(config)
    except Exception:
        # DATABAO-SPECIFIC: keep the upstream name in the log line so
        # upstream-issue triage stays fast.
        logger.exception("[database-sync] Databao introspect_database raised")
        return {"tables": [], "columns_count": 0, "had_top_values": False}

    # Stable datasource_id keyed on the OutcomeOps connection so per-
    # table chunks across a sync share the same envelope value. The
    # downstream embedding path doesn't depend on this being a UUID; a
    # stable string is sufficient and easier to triage in logs.
    datasource_id = f"ooat-sync:{engine}:{connection_id}" if connection_id else f"ooat-sync:{engine}"

    tables_out: List[Dict[str, Any]] = []
    columns_count = 0
    had_top_values = False

    for catalog in result.catalogs:
        cat_name = catalog.name or ""
        for schema in catalog.schemas:
            sch_name = schema.name or ""
            for table in schema.tables:
                tbl_name = table.name or ""
                if not tbl_name:
                    continue
                table_key = f"{cat_name}.{sch_name}.{tbl_name}"
                if table_key not in include_table_keys:
                    continue

                yaml_str, col_count, table_had_top_values = _build_table_chunk_yaml(
                    table, cat_name, sch_name, engine, datasource_id,
                )
                columns_count += col_count
                had_top_values = had_top_values or table_had_top_values
                tables_out.append({
                    "catalog": cat_name,
                    "schema": sch_name,
                    "table": tbl_name,
                    "yaml": yaml_str,
                })

    return {
        "tables": tables_out,
        "columns_count": columns_count,
        "had_top_values": had_top_values,
    }


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """Process a sync request for a database connection."""
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = message.get("sync_id") or (
        f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"
    )

    logger.info(f"Processing database sync for {workspace_id}/{connection_id} [{sync_id}]")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    engine = connection.get("engine", "")
    host_hash = connection.get("host_hash", "")
    database_name = connection.get("database_name", "")
    scope_config = connection.get("scope_config") or {}

    if not (scope_config.get("introspection") or {}).get("include"):
        logger.warning(f"No schemas selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_scope_selected")
        return {"status": "skipped", "reason": "No schemas selected"}

    if not engine or not host_hash:
        logger.error(f"Connection {connection_id} missing engine/host_hash")
        update_connection_status(
            workspace_id, connection_id, "error",
            error="Connection record incomplete",
        )
        return {"status": "error", "error": "Missing engine/host_hash"}

    update_connection_status(workspace_id, connection_id, "syncing")

    # Decrypt credential — this is the moment we emit the
    # DB_CREDENTIAL_USED audit row, regardless of whether the
    # downstream introspection call succeeds.
    credential = get_credential(connection_id)
    if not credential:
        logger.error(f"No credential found for connection {connection_id}")
        update_connection_status(
            workspace_id, connection_id, "auth_error",
            error="Credential missing or unreadable",
        )
        return {"status": "error", "error": "Credential missing"}

    _emit_credential_used_audit(
        workspace_id=workspace_id,
        connection_id=connection_id,
        engine=engine,
        host_hash=host_hash,
        database_name=database_name,
    )

    # Per-source repo-limit enforcement (ADR-019). Use engine+host_hash
    # as the stable identifier — same physical DB across workspaces
    # counts once.
    repo_project = f"database:{engine}:{host_hash}"
    limit_result = check_repo_limit(repo=repo_project)
    if not limit_result.allowed:
        logger.warning(
            f"[database-sync] Repo limit reached for {repo_project}: "
            f"{limit_result.reason} "
            f"(current={limit_result.current}/{limit_result.limit})"
        )
        update_connection_status(
            workspace_id, connection_id, "blocked_license_limit",
            error=limit_result.reason or "Repo limit reached",
        )
        return {"status": "skipped", "reason": "license_limit"}

    try:
        build_result = _run_schema_introspection(connection, credential)
    except Exception as e:
        # DATABAO-SPECIFIC: keep the upstream name in the log line so
        # upstream-issue triage stays fast.
        logger.error(f"Databao build failed: {e}")
        update_connection_status(
            workspace_id, connection_id, "error",
            error=f"schema introspection failed: {e}",
        )
        return {"status": "error", "error": f"schema introspection failed: {e}"}

    tables = build_result.get("tables", [])
    columns_count = build_result.get("columns_count", 0)
    current_table_ids: List[str] = []
    tables_queued = 0

    for tbl in tables:
        catalog = tbl.get("catalog", "")
        schema = tbl.get("schema", "")
        table_name = tbl.get("table", "")
        yaml_chunk = tbl.get("yaml", "")
        if not table_name or not yaml_chunk:
            continue

        table_id_raw = f"{catalog}.{schema}.{table_name}"
        current_table_ids.append(sanitize_filename(table_id_raw))

        queue_table_ingestion(
            workspace_id=workspace_id,
            connection_id=connection_id,
            engine=engine,
            host_hash=host_hash,
            catalog=catalog,
            schema=schema,
            table=table_name,
            table_yaml=yaml_chunk,
            sync_id=sync_id,
        )
        tables_queued += 1

    # Per-source usage tracking (ADR-019). Non-blocking.
    track_repo_active(repo=repo_project)

    update_connection_status(
        workspace_id, connection_id, "completed",
        tables_count=len(current_table_ids),
        known_table_ids=current_table_ids,
    )

    queue_orphan_cleanup(workspace_id, connection_id, current_table_ids)

    log_source_sync(
        audit_table_name=os.environ.get("AUDIT_TABLE", ""),
        source="database",
        workspace_id=workspace_id,
        target=connection_id,
        trigger=trigger,
        files_synced=tables_queued,
        duration_ms=int((time.time() - sync_start) * 1000),
        extra_details={
            "sync_id": sync_id,
            "engine": engine,
            "host_hash": host_hash,
            "tables_count": len(current_table_ids),
            "columns_count": columns_count,
            "top_values_stripped": True,
            "sampling_enabled": bool(
                (scope_config.get("sampling") or {}).get("enabled")
            ),
        },
    )

    return {
        "status": "success",
        "sync_id": sync_id,
        "tables_queued": tables_queued,
        "tables_count": len(current_table_ids),
    }


def lambda_handler(event: Dict, context: Any) -> Dict:
    """SQS-triggered handler for database sync orchestration."""
    logger.info(f"Database sync handler invoked with {len(event.get('Records', []))} records")

    # License validation (ADR-019). SQS-driven: raise on failure -> retry / DLQ.
    if validate_license is None:
        logger.error(
            "[database-sync] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(os.environ.get("AUDIT_TABLE", ""), "database-sync")
        raise ImportError("outcomeops_license layer not attached")
    try:
        claims = validate_license()
        logger.info(f"[database-sync] License validated: {claims.get('sub')}")
    except Exception as e:
        logger.error(f"[database-sync] License validation failed: {e}")
        raise

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    results = []
    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            result = process_sync_request(message)
            results.append(result)
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in SQS message: {e}")
            results.append({"status": "error", "error": "Invalid JSON"})
        except Exception as e:
            logger.error(f"Error processing sync request: {e}")
            results.append({"status": "error", "error": str(e)})

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"Database sync completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Database sync completed",
            "results": results,
        }),
    }
