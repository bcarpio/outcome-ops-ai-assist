"""
Thin wrapper around Databao Context Engine calls.

Keeps the Databao API surface contained to one module so future swaps
or version bumps are cheap. All exported functions accept generic
engine + credential params and translate to Databao datasource config
shapes internally.

This module is intentionally light on logic — Databao does the real
work. The wrappers exist so the integration / sync handlers don't
import `databao_context_engine` directly and the surface stays auditable.

All `databao_context_engine` imports are deferred to call time so the
integration Lambda can import this module cleanly without the package
present (unit tests run without it). When the package is missing,
these functions raise DatabaoLayerNotAttached at call time.
"""

import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DatabaoLayerNotAttached(RuntimeError):
    """Raised when Databao is called but the Lambda layer isn't attached.

    Catch-and-audit per ADR-019 — the sync Lambda should emit a
    LICENSE_FAIL_OPEN-style "deploy bug" audit row when it sees this.
    """


@dataclass
class ValidationResult:
    """Result of a connection-validation attempt."""
    valid: bool
    error: Optional[str] = None
    server_version: Optional[str] = None


@dataclass
class SchemaInfo:
    """One catalog/schema/table entry returned by the picker."""
    catalog: str
    schema: str
    table: str
    row_count_estimate: Optional[int] = None


def _import_databao():
    """Deferred import. Raises DatabaoLayerNotAttached if the package
    isn't installed in the runtime image."""
    try:
        import databao_context_engine  # noqa: F401
        return databao_context_engine
    except ImportError as e:
        raise DatabaoLayerNotAttached(
            "databao_context_engine import failed - layer not attached "
            "(see terraform/lambda-databao-layer.tf): " + str(e)
        )


def _build_config(
    dce,
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_payload: Dict[str, Any],
    datasource_name: str = "ooat",
    *,
    enable_sampling: bool = False,
):
    """Build the engine-specific ConfigFile for the given connection params.

    Reads optional fields from credential_payload that the UI exposes:
      - ssl_mode (Postgres / MySQL): "require", "disable", etc.
      - encrypt (MSSQL): bool — maps to MSSQL Encrypt=yes/no
      - trust_server_certificate (MSSQL): bool — for RDS / self-signed
        certs, skips cert validation. Default False (secure).

    Sampling is hard-coded off by default. Databao defaults
    SamplingConfig.enabled=True, which would pull real row data on every
    introspection - inconsistent with OutcomeOps's regulated-buyer
    posture. Callers that need sampling (admin per-table opt-in via
    scope_config in the sync handler) pass enable_sampling=True.
    """
    from databao_context_engine.plugins.databases.sampling_scope import SamplingConfig

    user = credential_payload.get("username")
    password = credential_payload.get("password")
    sampling = SamplingConfig(enabled=bool(enable_sampling))

    if engine == "postgres":
        from databao_context_engine.plugins.databases.postgresql.config_file import (
            PostgresConfigFile, PostgresConnectionProperties,
        )
        additional = {}
        ssl_mode = credential_payload.get("ssl_mode")
        if ssl_mode and ssl_mode != "disable":
            additional["ssl"] = ssl_mode
        return PostgresConfigFile(
            name=datasource_name,
            connection=PostgresConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                additional_properties=additional,
            ),
            sampling=sampling,
        )

    if engine == "mysql":
        from databao_context_engine.plugins.databases.mysql.config_file import (
            MySQLConfigFile, MySQLConnectionProperties,
        )
        additional = {}
        ssl_mode = credential_payload.get("ssl_mode")
        if ssl_mode and ssl_mode != "disable":
            # pymysql expects ssl as a dict; empty dict enables SSL with defaults.
            additional["ssl"] = {}
        return MySQLConfigFile(
            name=datasource_name,
            connection=MySQLConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                additional_properties=additional,
            ),
            sampling=sampling,
        )

    if engine == "mssql":
        from databao_context_engine.plugins.databases.mssql.config_file import (
            MSSQLConfigFile, MSSQLConnectionProperties,
        )
        encrypt_flag = credential_payload.get("encrypt", True)
        encrypt_value = "yes" if encrypt_flag else "no"
        additional = {}
        if credential_payload.get("trust_server_certificate"):
            additional["trust_server_certificate"] = True
        return MSSQLConfigFile(
            name=datasource_name,
            connection=MSSQLConnectionProperties(
                host=host, port=port, database=database_name,
                user=user, password=password,
                encrypt=encrypt_value,
                additional_properties=additional,
            ),
            sampling=sampling,
        )

    raise ValueError(f"Unsupported engine: {engine!r}")


def _datasource_type(dce, engine: str):
    """Return the Databao DatasourceType for the given engine string.

    Plugin IDs (jetbrains/postgres etc.) declare `supported = {"postgres"}`
    so the simple lowercase engine name is the right full_type value.
    """
    return dce.DatasourceType(full_type=engine)


def validate_connection(
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_payload: Dict[str, Any],
) -> ValidationResult:
    """Open a connection, run a trivial query, close. Used by the UI
    "Test connection" button before persisting credentials, and by the
    integration handler at connection-create time.

    Args:
        engine: "postgres" | "mysql" | "mssql"
        host, port, database_name: connection target
        credential_payload: engine-specific auth dict (see
            credential_manager.py for shapes)

    Returns:
        ValidationResult with valid=True on success, or valid=False
        with an error message on failure.
    """
    dce = _import_databao()
    try:
        config = _build_config(
            dce, engine, host, port, database_name, credential_payload,
            datasource_name="ooat-check",
        )
    except (ValueError, ImportError) as e:
        return ValidationResult(valid=False, error=str(e))

    # check_datasource_config_connection lives on the domain manager but
    # doesn't persist the config - it's the right call for a one-shot
    # validation. mkdtemp keeps the domain dir disposable.
    try:
        with tempfile.TemporaryDirectory(prefix="dce-check-") as domain_dir:
            mgr = dce.init_dce_domain(Path(domain_dir))
            result = mgr.check_datasource_config_connection(
                datasource_type=_datasource_type(dce, engine),
                datasource_name="ooat-check",
                config_content=config,
            )
    except Exception as e:
        logger.exception("Databao check_datasource_config_connection raised")
        return ValidationResult(valid=False, error=f"{type(e).__name__}: {e}")

    if result.connection_status == dce.DatasourceConnectionStatus.VALID:
        return ValidationResult(valid=True)

    # INVALID or UNKNOWN — surface the most useful detail we have. The
    # summary is short ("Connection with the datasource can not be
    # established"); full_message has the engine-specific detail
    # (auth failure, network refused, SSL cert problem, etc.).
    detail = result.full_message or result.summary or str(result.connection_status.value)
    return ValidationResult(valid=False, error=detail)


def list_schemas(
    engine: str,
    host: str,
    port: int,
    database_name: str,
    credential_payload: Dict[str, Any],
) -> List[SchemaInfo]:
    """Metadata-only enumeration for the UI schema/table picker.

    Drives Databao's build_context with sampling off, indexing off,
    and enrichment off so the run is metadata-only and cheap. Parses
    the per-datasource YAML artifact for the catalog/schema/table
    tree and returns it as a flat list of SchemaInfo.

    For very large warehouses we may need to add a cache layer
    (DDB-backed) - flagged in plan/PROGRESS.md as an open question.
    """
    dce = _import_databao()
    try:
        config = _build_config(
            dce, engine, host, port, database_name, credential_payload,
            datasource_name="ooat-picker",
            enable_sampling=False,
        )
    except (ValueError, ImportError) as e:
        logger.error(f"list_schemas config build failed: {e}")
        return []

    try:
        with tempfile.TemporaryDirectory(prefix="dce-picker-") as domain_dir:
            mgr = dce.init_dce_domain(Path(domain_dir))
            configured = mgr.create_datasource_config(
                datasource_type=_datasource_type(dce, engine),
                datasource_name="ooat-picker",
                config_content=config,
            )
            results = mgr.build_context(
                datasource_ids=[configured.datasource.id],
                should_index=False,
                should_enrich_context=False,
            )
            if not results:
                return []

            return _parse_schema_tree(results[0].context_file_path)
    except Exception:
        logger.exception("Databao build_context for picker failed")
        return []


def _parse_schema_tree(context_file_path) -> List[SchemaInfo]:
    """Walk the Databao YAML artifact and return a flat catalog/schema/table list.

    YAML shape (per build_context output, verified against Databao
    0.x for postgres / mysql / mssql):

        context:
          catalogs:
            - name: <catalog>
              schemas:
                - name: <schema>
                  tables:
                    - name: <table>
                      kind: table | view
                      ...
    """
    import yaml

    if not context_file_path or not Path(context_file_path).exists():
        return []

    data = yaml.safe_load(Path(context_file_path).read_text())
    out: List[SchemaInfo] = []
    for catalog in (data.get("context", {}) or {}).get("catalogs", []) or []:
        cat_name = catalog.get("name") or ""
        for schema in catalog.get("schemas", []) or []:
            sch_name = schema.get("name") or ""
            for table in schema.get("tables", []) or []:
                tbl_name = table.get("name") or ""
                if not tbl_name:
                    continue
                out.append(SchemaInfo(catalog=cat_name, schema=sch_name, table=tbl_name))
    return out


# build_context lives in database-sync (heavy operation, long timeout
# Lambda needed). Not exposed here.
