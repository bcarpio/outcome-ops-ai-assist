"""
Database Integration Lambda for OutcomeOps AI Assist.

Handles database-connection lifecycle for the new structured-data
source (Postgres / MySQL / MS SQL Server in MVP). Mirrors the OneNote
integration shape; key differences:
  - No OAuth flow. Admin posts engine + host + credentials directly.
  - Credentials stored KMS-encrypted in DDB (see credential_manager.py),
    NOT in AWS Secrets Manager — keeps all credential-access audit
    rows in the unified audit table.
  - Validation step (engine check_datasource_config_connection) before
    persisting the connection record.

API Endpoints:
    POST   /api/database/connections                                  - Create connection
    POST   /api/database/connections/validate                         - Test connection without persist
    GET    /workspaces/{id}/database/connections                      - List connections
    GET    /workspaces/{id}/database/connections/{connId}             - Get connection
    GET    /workspaces/{id}/database/connections/{connId}/schemas     - List catalogs/schemas/tables
    PUT    /workspaces/{id}/database/connections/{connId}/scope       - Update introspection/sampling scope
    POST   /workspaces/{id}/database/connections/{connId}/sync        - Trigger sync
    DELETE /workspaces/{id}/database/connections/{connId}             - Disconnect (queues delete_prefix)

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
The x-user-email header is set by the proxy after Azure AD OIDC validation.
"""

import hashlib
import json
import logging
import os
import secrets
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import (  # noqa: E402
    ACTION_DB_CONNECTION_DELETED,
    ACTION_DB_CONNECTION_ESTABLISHED,
    log_admin_action,
)
from shared.license_repos import get_active_repos_for_customer  # noqa: E402
from shared.region_guard import (  # noqa: E402
    RegionPassiveError,
    guard_active,
    passive_response,
)

# ADR-019: license layer is the source of truth for repo limits. Wrap
# the import in try/except so the module still loads when the layer is
# unattached -- the snapshot helper below treats that as a no-op (the
# heartbeat scheduler is the safety net).
try:
    from outcomeops_license import report_repos_snapshot  # noqa: E402
except ImportError:  # pragma: no cover - deploy bug path, exercised in tests
    report_repos_snapshot = None  # type: ignore[assignment]

from credential_manager import (
    delete_credential,
    save_credential,
)
from introspection_helpers import (
    IntrospectionEngineUnavailable,
    list_schemas as list_db_schemas,
    validate_connection as validate_db_connection,
)

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
DATABASE_SYNC_QUEUE = os.environ.get("DATABASE_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

SUPPORTED_ENGINES = frozenset({"postgres", "mysql", "mssql"})

_dynamodb = None
_sqs = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _snapshot_active_repos() -> None:
    """Refresh the license server's repo-set snapshot after any connect
    or disconnect. Non-blocking on failure -- the hourly heartbeat
    scheduler (license-heartbeat-scheduler) is the safety net for any
    missed snapshot.
    """
    if report_repos_snapshot is None:
        # Layer not attached (deploy-bug path). The validate_license
        # check upstream will have caught this; treat the snapshot
        # call as a no-op here.
        return
    try:
        active = get_active_repos_for_customer(_get_dynamodb(), WORKSPACES_TABLE)
        report_repos_snapshot(active_repos=active)
    except Exception as e:
        logger.warning(f"[license-snapshot] non-blocking failure: {e}")


# ============================================================================
# Response Helpers
# ============================================================================


def _response(status_code: int, body: Dict) -> Dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _error(status_code: int, message: str) -> Dict:
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict) -> Optional[str]:
    headers = event.get("headers") or {}
    return headers.get("x-user-email")


def _parse_path(path: str) -> Tuple[str, ...]:
    parts = [p for p in path.strip("/").split("/") if p]
    return tuple(parts)


def _host_hash(host: str) -> str:
    """Short stable hash of the connection host; used as license-tracking
    repo identity so the same physical DB across many workspace
    connections counts once for license-limit accounting."""
    return hashlib.sha256(host.encode("utf-8")).hexdigest()[:16]


# ============================================================================
# Workspace Helpers (identical pattern to onenote-integration)
# ============================================================================


def _get_membership(table, user_email: str, workspace_id: str) -> Optional[Dict]:
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_email}", "SK": f"WS_MEMBER#{workspace_id}"}
        )
        return response.get("Item")
    except ClientError:
        return None


def _is_org_admin(table, user_email: str) -> bool:
    try:
        resp = table.get_item(Key={"PK": f"USER#{user_email}", "SK": "ORG_MEMBER"})
        item = resp.get("Item")
        return item is not None and item.get("org_role") == "org_admin"
    except ClientError:
        return False


def _can_access_workspace(table, user_email: str, workspace_id: str) -> bool:
    if _get_membership(table, user_email, workspace_id):
        return True
    return _is_org_admin(table, user_email)


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    membership = _get_membership(table, user_email, workspace_id)
    if membership and membership.get("role") == "workspace_admin":
        return True
    return _is_org_admin(table, user_email)


# ============================================================================
# Connection Operations
# ============================================================================


def generate_id() -> str:
    return secrets.token_urlsafe(16)


def _default_scope_config() -> Dict[str, Any]:
    """Default scope at connection-create time. Schemas-only scope as
    of chunk 06: introspection.include and sampling.include both hold
    2-segment 'catalog.schema' keys. Sampling is off by default
    (empty include == off, the regulated-buyer posture). Profiling
    is on so admins get aggregate column stats; top_values are
    stripped post-hoc by sync + file-ingestion.
    """
    return {
        "introspection": {"include": [], "exclude": []},
        "sampling":      {"include": [], "exclude": []},
        "profiling":     {"enabled": True},
    }


def _validate_schema_keys(keys: list, *, field: str) -> None:
    """Strict-validate a list of 'catalog.schema' keys. Raises
    ValueError on any malformed entry.

    Chunk 06: introspection.include and sampling.include both hold
    schema-level keys exclusively. Legacy 3-segment 'catalog.schema.table'
    entries are intentionally rejected — connections were disconnected
    before the chunk-06 deploy so no legacy records survive.
    """
    if not isinstance(keys, list):
        raise ValueError(f"{field} must be a list of 'catalog.schema' strings")
    for key in keys:
        if not isinstance(key, str):
            raise ValueError(f"{field} entry must be a string, got {type(key).__name__}")
        parts = key.split(".", 2)
        if len(parts) != 2 or not parts[0] or not parts[1]:
            raise ValueError(
                f"Malformed {field} entry {key!r}: expected "
                f"'catalog.schema' (2 segments), got {len(parts)}"
            )


def validate_credentials(body: Dict) -> Dict:
    """POST /api/database/connections/validate — test creds before persist."""
    engine = (body.get("engine") or "").lower()
    if engine not in SUPPORTED_ENGINES:
        return _error(400, f"Unsupported engine: {engine}")

    host = body.get("host", "")
    port = int(body.get("port") or 0)
    database_name = body.get("database_name", "")
    credential = body.get("credential") or {}

    if not host or not port or not database_name:
        return _error(400, "host, port, and database_name are required")

    try:
        result = validate_db_connection(
            engine=engine,
            host=host,
            port=port,
            database_name=database_name,
            credential_payload=credential,
        )
    except IntrospectionEngineUnavailable as e:
        logger.error(f"Schema introspection engine unavailable: {e}")
        return _error(500, "Server misconfiguration: schema introspection engine unavailable")

    if result.valid:
        return _response(200, {
            "valid": True,
            "server_version": result.server_version,
        })
    return _response(200, {
        "valid": False,
        "error": result.error or "Connection failed",
    })


def create_connection(user_email: str, body: Dict) -> Dict:
    """POST /api/database/connections."""
    workspace_id = body.get("workspace_id", "")
    if not workspace_id:
        return _error(400, "workspace_id required")

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can configure integrations")

    engine = (body.get("engine") or "").lower()
    if engine not in SUPPORTED_ENGINES:
        return _error(400, f"Unsupported engine: {engine}")

    host = body.get("host", "")
    port = int(body.get("port") or 0)
    database_name = body.get("database_name", "")
    display_label = body.get("display_label", "") or f"{engine}://{host}/{database_name}"
    credential = body.get("credential") or {}

    if not host or not port or not database_name:
        return _error(400, "host, port, and database_name are required")
    if not credential:
        return _error(400, "credential payload required")

    # Pre-flight validation - don't persist a connection we can't reach.
    try:
        validation = validate_db_connection(
            engine=engine,
            host=host,
            port=port,
            database_name=database_name,
            credential_payload=credential,
        )
    except IntrospectionEngineUnavailable as e:
        logger.error(f"Schema introspection engine unavailable: {e}")
        return _error(500, "Server misconfiguration: schema introspection engine unavailable")

    if not validation.valid:
        return _error(400, f"Connection test failed: {validation.error}")

    connection_id = generate_id()
    host_hash = _host_hash(host)
    now = datetime.now(timezone.utc).isoformat()

    connection = {
        "PK": f"WS#{workspace_id}",
        "SK": f"DATABASE_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "engine": engine,
        "host": host,
        "port": port,
        "database_name": database_name,
        "display_label": display_label,
        "host_hash": host_hash,
        "scope_config": _default_scope_config(),
        "last_sync_at": None,
        "last_sync_status": "needs_configuration",
        "tables_count": 0,
        "known_table_ids": [],
        "created_by": user_email,
        "created_at": now,
        "updated_at": now,
    }

    try:
        dual_write.ddb_put_item(WORKSPACES_TABLE, connection)
        save_credential(connection_id, credential)
        logger.info(
            f"Created database connection {connection_id} "
            f"({engine} {host}/{database_name}) for workspace {workspace_id}"
        )

        try:
            log_admin_action(
                os.environ.get("AUDIT_TABLE", ""),
                ACTION_DB_CONNECTION_ESTABLISHED,
                workspace_id=workspace_id,
                user_email=user_email,
                source_ip="",
                user_agent="",
                details={
                    "provider": "database",
                    "connection_id": connection_id,
                    "engine": engine,
                    "host_hash": host_hash,
                    "database_name": database_name,
                    "display_label": display_label,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit DB_CONNECTION_ESTABLISHED audit row: {e}")

        # Refresh the license server's active-repo snapshot (license layer
        # v2.0.0 desired-state model). DB connections are deduped across
        # workspaces by host_hash, so two workspaces pointing at the same
        # physical DB count once.
        _snapshot_active_repos()

        # Return connection without sensitive fields
        public = {k: v for k, v in connection.items() if k not in ("PK", "SK")}
        return _response(201, {"connection": public})

    except ClientError as e:
        logger.error(f"Failed to create connection: {e}")
        return _error(500, "Failed to create connection")


def list_connections(user_email: str, workspace_id: str) -> Dict:
    """List database connections for a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _get_membership(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "DATABASE_CONNECTION#",
            },
        )
        connections = response.get("Items", [])

        for conn in connections:
            sk = conn.pop("SK", None)
            conn.pop("PK", None)
            # Strip large internal-state fields from the list response
            conn.pop("known_table_ids", None)
            if not conn.get("connection_id") and sk:
                conn["connection_id"] = sk.replace("DATABASE_CONNECTION#", "")

        return _response(200, {"connections": connections})
    except ClientError as e:
        logger.error(f"Failed to list connections: {e}")
        return _error(500, "Failed to list connections")


def get_connection(user_email: str, workspace_id: str, connection_id: str) -> Optional[Dict]:
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _get_membership(table, user_email, workspace_id):
        return None

    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError:
        return None


def get_schemas(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """List catalogs/schemas/tables for the picker.

    Calls introspection_helpers.list_schemas which does a metadata-only
    introspection (no row sampling, no column profiling) — cheap
    enough for picker latency on small/medium DBs. Large warehouses
    may need a DDB-backed cache; flagged in plan/PROGRESS.md.
    """
    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    from credential_manager import get_credential
    credential = get_credential(connection_id)
    if not credential:
        return _error(500, "Credential not found for connection")

    try:
        result = list_db_schemas(
            engine=connection["engine"],
            host=connection["host"],
            port=connection["port"],
            database_name=connection["database_name"],
            credential_payload=credential,
        )
    except IntrospectionEngineUnavailable as e:
        logger.error(f"Schema introspection engine unavailable: {e}")
        return _error(500, "Server misconfiguration: schema introspection engine unavailable")

    # Distinguish "user truly has zero in-scope schemas" (200 with
    # empty list) from "introspection failed" (502 with the upstream
    # message in the body) so the picker UI can render the actual
    # failure cause instead of a generic SELECT-grants hint.
    if result.error_type == "introspection_failed":
        return _response(502, {
            "error": result.error or "Schema introspection failed",
            "error_type": result.error_type,
        })

    payload = [
        {
            "catalog": s.catalog,
            "schema": s.schema,
            "table": s.table,
            "row_count_estimate": s.row_count_estimate,
        }
        for s in result.schemas
    ]
    return _response(200, {"schemas": payload})


def update_scope(
    user_email: str,
    workspace_id: str,
    connection_id: str,
    body: Dict,
) -> Dict:
    """PUT scope_config; queues a sync if introspection.include is non-empty."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update database scope")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    scope = body.get("scope_config") or {}
    # Defensive defaults if admin sent partial scope
    scope.setdefault("introspection", {"include": [], "exclude": []})
    scope.setdefault("sampling", {"include": [], "exclude": []})
    scope.setdefault("profiling", {"enabled": True})

    # Strict 2-segment validation (chunk 06). Reject the legacy
    # 'catalog.schema.table' shape — connections were disconnected
    # pre-deploy, no legacy records survive.
    try:
        _validate_schema_keys(
            scope.get("introspection", {}).get("include") or [],
            field="introspection.include",
        )
        _validate_schema_keys(
            scope.get("sampling", {}).get("include") or [],
            field="sampling.include",
        )
    except ValueError as e:
        return _error(400, str(e))

    now = datetime.now(timezone.utc).isoformat()
    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            },
            UpdateExpression=(
                "SET scope_config = :scope, "
                "updated_at = :now, "
                "last_sync_status = :status"
            ),
            ExpressionAttributeValues={
                ":scope": scope,
                ":now": now,
                ":status": "queued",
            },
        )

        if DATABASE_SYNC_QUEUE and scope["introspection"].get("include"):
            _queue_sync(workspace_id, connection_id)

        return _response(200, {"message": "Scope updated"})
    except ClientError as e:
        logger.error(f"Failed to update scope: {e}")
        return _error(500, "Failed to update scope")


def trigger_sync(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can trigger sync")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    scope = connection.get("scope_config") or {}
    if not (scope.get("introspection") or {}).get("include"):
        return _error(400, "No schemas selected for introspection")

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            },
            UpdateExpression="SET last_sync_status = :status, updated_at = :now",
            ExpressionAttributeValues={
                ":status": "queued",
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
        _queue_sync(workspace_id, connection_id)
        return _response(200, {"message": "Sync queued", "status": "queued"})
    except ClientError as e:
        logger.error(f"Failed to trigger sync: {e}")
        return _error(500, "Failed to trigger sync")


def delete_connection(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """Delete a database connection - removes DDB record + credential +
    queues delete_prefix to wipe S3 artifacts + vectors."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can delete connections")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    try:
        if DELETE_QUEUE:
            sqs = _get_sqs()
            sqs.send_message(
                QueueUrl=DELETE_QUEUE,
                MessageBody=json.dumps({
                    "action": "delete_prefix",
                    "workspace_id": workspace_id,
                    "source": "database",
                    "prefix": f"{connection_id}/",
                }),
            )
            logger.info(f"Queued deletion of database artifacts for connection {connection_id}")

        dual_write.ddb_delete_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"DATABASE_CONNECTION#{connection_id}",
            },
        )
        delete_credential(connection_id)

        try:
            log_admin_action(
                os.environ.get("AUDIT_TABLE", ""),
                ACTION_DB_CONNECTION_DELETED,
                workspace_id=workspace_id,
                user_email=user_email,
                source_ip="",
                user_agent="",
                details={
                    "provider": "database",
                    "connection_id": connection_id,
                    "engine": connection.get("engine"),
                    "host_hash": connection.get("host_hash"),
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit DB_CONNECTION_DELETED audit row: {e}")

        logger.info(f"Deleted database connection {connection_id}")

        # Refresh the license server's active-repo snapshot (license layer
        # v2.0.0 desired-state model). If the same physical DB is still
        # connected from another workspace, the identifier remains in the
        # set (dedupe by host_hash).
        _snapshot_active_repos()

        return _response(200, {"message": "Connection deleted"})
    except ClientError as e:
        logger.error(f"Failed to delete connection: {e}")
        return _error(500, "Failed to delete connection")


def _queue_sync(workspace_id: str, connection_id: str):
    if not DATABASE_SYNC_QUEUE:
        logger.warning("DATABASE_SYNC_QUEUE not configured")
        return
    sqs = _get_sqs()
    try:
        sqs.send_message(
            QueueUrl=DATABASE_SYNC_QUEUE,
            MessageBody=json.dumps({
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
        logger.info(f"Queued sync for database connection {connection_id}")
    except ClientError as e:
        logger.error(f"Failed to queue sync: {e}")


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    logger.info(f"Database API: {http_method} {path}")

    parts = _parse_path(path)
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    # POST /api/database/connections/validate (test creds, no persist)
    if (
        parts == ("api", "database", "connections", "validate")
        and http_method == "POST"
    ):
        return validate_credentials(body)

    # POST /api/database/connections (create)
    if parts == ("api", "database", "connections") and http_method == "POST":
        try:
            guard_active("database_create_connection", user_email=user_email)
        except RegionPassiveError:
            return passive_response("database_create_connection")
        return create_connection(user_email, body)

    # GET /workspaces/{id}/database/connections
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "database"
        and parts[3] == "connections"
        and http_method == "GET"
    ):
        return list_connections(user_email, parts[1])

    # GET /workspaces/{id}/database/connections/{connId}/schemas
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "database"
        and parts[3] == "connections"
        and parts[5] == "schemas"
        and http_method == "GET"
    ):
        return get_schemas(user_email, parts[1], parts[4])

    # PUT /workspaces/{id}/database/connections/{connId}/scope
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "database"
        and parts[3] == "connections"
        and parts[5] == "scope"
        and http_method == "PUT"
    ):
        try:
            guard_active(
                "database_update_scope",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("database_update_scope")
        return update_scope(user_email, parts[1], parts[4], body)

    # POST /workspaces/{id}/database/connections/{connId}/sync
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "database"
        and parts[3] == "connections"
        and parts[5] == "sync"
        and http_method == "POST"
    ):
        try:
            guard_active(
                "database_trigger_sync",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("database_trigger_sync")
        return trigger_sync(user_email, parts[1], parts[4])

    # DELETE /workspaces/{id}/database/connections/{connId}
    if (
        len(parts) == 5
        and parts[0] == "workspaces"
        and parts[2] == "database"
        and parts[3] == "connections"
        and http_method == "DELETE"
    ):
        try:
            guard_active(
                "database_delete_connection",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("database_delete_connection")
        return delete_connection(user_email, parts[1], parts[4])

    return _error(404, "Not found")
