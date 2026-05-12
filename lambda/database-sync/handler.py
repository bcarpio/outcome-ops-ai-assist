"""
Database Sync Lambda for OutcomeOps AI Assist.

SQS-triggered worker that drives Databao introspection against a
customer database connection, strips top_values from the produced
artifact, and queues per-table chunks to file-ingestion.

Flow per SQS record:
  1. License-validate (ADR-019). Raise on failure -> SQS retry / DLQ.
  2. Fetch DATABASE_CONNECTION#{conn_id} from DDB.
  3. Decrypt the DB credential, emit DB_CREDENTIAL_USED audit row.
  4. check_repo_limit("database:{engine}:{host_hash}") - repo identity
     uses engine+host_hash so the same physical DB across many
     workspace connections counts once.
  5. Call databao build_context() in a temp dir.
  6. For each table in the YAML artifact: strip top_values, queue a
     message to FILE_INGESTION_QUEUE.
  7. Update connection status to completed, write known_table_ids.
  8. Queue orphan_cleanup to delete-worker.
  9. Emit DATABASE_SYNCED audit row.

The top_values strip is duplicated defensively in file-ingestion
(chunk 03) so a regression here can't cause column values to land
in S3.

NOTE (chunk 01 scaffold): the actual Databao call is stubbed. The
shape (license validation, credential decrypt + audit, repo-limit
check, file-ingestion enqueue, status update, audit emission) is
fully wired so the integration tests in chunk 05 can exercise the
flow without the Databao layer attached.
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
    frequency). Defensible "statistics" framing from Databao, but for
    regulated-buyer defaults we treat them as the same risk class as
    row sampling — strip before the artifact crosses the SQS boundary.

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


def _run_databao_build(connection: Dict, credential: Dict) -> Dict[str, Any]:
    """Wrapper for Databao's build_context() call.

    Returns a dict with keys:
      - "tables": list of per-table dicts {catalog, schema, table, yaml}
      - "columns_count": total columns introspected
      - "had_top_values": bool — informational; the YAML is already
        stripped by the time it lands in "tables"

    NOTE (chunk 01): real Databao wiring lands with the layer in
    chunk 02. Stubbed here to return an empty result so the rest of
    the flow can be exercised by unit tests.
    """
    try:
        from databao_context_engine import init_dce_domain  # noqa: F401
    except ImportError:
        logger.warning(
            "[database-sync] databao_context_engine not importable — "
            "layer wiring lands in chunk 02; returning empty build result"
        )
        return {"tables": [], "columns_count": 0, "had_top_values": False}

    # The real implementation will:
    # 1. mkdtemp() a domain dir
    # 2. domain_manager.create_datasource_config(...)
    # 3. domain_manager.build_context()
    # 4. parse the per-datasource YAML artifact
    # 5. for each table, slice the YAML into a per-table chunk and
    #    call strip_top_values() on the parsed doc, then yaml.dump()
    # Wiring lands in chunk 02 when the layer is attached.
    return {"tables": [], "columns_count": 0, "had_top_values": False}


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
    # downstream Databao call succeeds.
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
        build_result = _run_databao_build(connection, credential)
    except Exception as e:
        logger.error(f"Databao build failed: {e}")
        update_connection_status(
            workspace_id, connection_id, "error",
            error=f"Databao build failed: {e}",
        )
        return {"status": "error", "error": f"Databao build failed: {e}"}

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
