"""
ServiceNow Integration Lambda for OutcomeOps AI Assist.

Handles OAuth authorize/callback + connection lifecycle for the deploy's
configured ServiceNow instance. One OAuth app per deploy: instance URL +
client_id come from tfvars (env vars on this Lambda) and client_secret
comes from ``/{env}/{app}/oauth/servicenow/client-secret`` SSM SecureString.

API surface:

    GET    /workspaces/{ws}/servicenow/authorize
    GET    /servicenow/callback              (no workspace auth; state-signed)
    GET    /workspaces/{ws}/servicenow/connections
    POST   /workspaces/{ws}/servicenow/connections/{id}/sync
    DELETE /workspaces/{ws}/servicenow/connections/{id}
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import (  # noqa: E402
    ACTION_OAUTH_CONNECTION_ESTABLISHED,
    log_admin_action,
)
from shared.region_guard import (  # noqa: E402
    RegionPassiveError,
    guard_active,
    passive_response,
)
from shared.oauth import servicenow as servicenow_oauth  # noqa: E402

from shared.servicenow_api import (  # noqa: E402
    ServiceNowRateLimitedError,
    get_current_user,
)

try:
    from outcomeops_license import report_repos_snapshot  # noqa: E402
except ImportError:  # pragma: no cover
    report_repos_snapshot = None  # type: ignore[assignment]

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE")
SERVICENOW_SYNC_QUEUE = os.environ.get("SERVICENOW_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
SERVICENOW_STATE_TTL_SECONDS = int(os.environ.get("SERVICENOW_STATE_TTL_SECONDS", "600"))
SERVICENOW_CALLBACK_URL = os.environ.get("SERVICENOW_CALLBACK_URL", "")


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


# ============================================================================
# HTTP helpers
# ============================================================================


def _response(status_code: int, body: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "statusCode": status_code,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body),
    }


def _redirect(url: str) -> Dict[str, Any]:
    return {
        "statusCode": 302,
        "headers": {"Location": url},
        "body": "",
    }


def _error(status_code: int, message: str) -> Dict[str, Any]:
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict[str, Any]) -> Optional[str]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("x-user-email")


def _parse_path(path: str) -> Tuple[str, ...]:
    return tuple(p for p in path.strip("/").split("/") if p)


# ============================================================================
# Auth helpers (workspace-admin gate mirrors confluence-integration)
# ============================================================================


def _get_membership(table, user_email: str, workspace_id: str) -> Optional[Dict]:
    resp = table.get_item(
        Key={"PK": f"USER#{user_email}", "SK": f"WS_MEMBER#{workspace_id}"}
    )
    return resp.get("Item")


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    if not user_email:
        return False
    m = _get_membership(table, user_email, workspace_id)
    if m and m.get("role") == "workspace_admin":
        return True
    org = table.get_item(
        Key={"PK": f"USER#{user_email}", "SK": "ORG_MEMBER"}
    ).get("Item")
    return bool(org and org.get("org_role") == "org_admin")


# ============================================================================
# OAuth state cache (DDB TTL row)
# ============================================================================


def _put_oauth_state(state: str, payload: Dict[str, Any]) -> None:
    ttl_epoch = int(
        (datetime.now(timezone.utc) + timedelta(seconds=SERVICENOW_STATE_TTL_SECONDS)).timestamp()
    )
    _get_dynamodb().Table(WORKSPACES_TABLE).put_item(
        Item={
            "PK": f"SERVICENOW_STATE#{state}",
            "SK": f"SERVICENOW_STATE#{state}",
            "entity_type": "servicenow_state",
            "payload": payload,
            "ttl": ttl_epoch,
        }
    )


def _consume_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    key = {"PK": f"SERVICENOW_STATE#{state}", "SK": f"SERVICENOW_STATE#{state}"}
    resp = table.get_item(Key=key)
    item = resp.get("Item")
    if not item:
        return None
    try:
        table.delete_item(Key=key)
    except Exception:
        logger.exception("Failed to delete OAuth state row (non-fatal)")
    return item.get("payload") or {}


# ============================================================================
# Connection lifecycle
# ============================================================================


def handle_authorize(event: Dict, user_email: str, workspace_id: str) -> Dict:
    """GET /workspaces/{ws}/servicenow/authorize

    Creates a pending connection row (or reuses one waiting for OAuth),
    generates state, returns the ServiceNow authorize URL. Same UX as
    Confluence/Jira: workspace admin clicks Connect, the browser lands
    on the OAuth consent screen without an intermediate form.
    """
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can configure integrations")

    if not SERVICENOW_CALLBACK_URL:
        return _error(500, "SERVICENOW_CALLBACK_URL not configured; deploy needs a domain")
    try:
        servicenow_oauth.get_instance_url()
        servicenow_oauth.get_client_id()
    except ValueError as e:
        return _error(500, f"ServiceNow integration not fully configured: {e}")

    connection_id = _find_pending_connection(table, workspace_id) or str(uuid.uuid4())
    now = datetime.now(timezone.utc).isoformat()
    dual_write.ddb_put_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
            "GSI2PK": "SERVICENOW#connections",
            "GSI2SK": f"WS#{workspace_id}#CONN#{connection_id}",
            "entity_type": "servicenow_connection",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "last_sync_status": "needs_oauth",
            "config": {"cursor": None},
            "created_at": now,
            "updated_at": now,
            "created_by": user_email,
        },
    )

    state = secrets.token_urlsafe(32)
    _put_oauth_state(
        state,
        {
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "user_email": user_email,
        },
    )
    authorize_url = servicenow_oauth.build_authorize_url(
        redirect_uri=SERVICENOW_CALLBACK_URL,
        state=state,
    )
    return _response(200, {"authorize_url": authorize_url, "connection_id": connection_id})


def _find_pending_connection(table, workspace_id: str) -> Optional[str]:
    """Look for an existing needs_oauth row so a retried Connect click
    reuses the same connection_id."""
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":sk": "SERVICENOW_CONNECTION#",
        },
    }
    while True:
        page = table.query(**query_kwargs)
        for item in page.get("Items", []) or []:
            if item.get("last_sync_status") == "needs_oauth":
                return item.get("connection_id")
        if "LastEvaluatedKey" not in page:
            return None
        query_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handle_callback(event: Dict) -> Dict:
    """/servicenow/callback

    Accepts code + state from EITHER querystring (GET-direct) or JSON body
    (POST-via-Express-proxy). The proxy POSTs the params to sidestep
    SigV4 signature-vs-querystring quirks on the Function URL.

    No workspace_id in path -- state row carries it.
    """
    query = event.get("queryStringParameters") or {}
    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}
    code = query.get("code") or body.get("code")
    state = query.get("state") or body.get("state")
    err = query.get("error") or body.get("error")
    if err:
        logger.warning(f"ServiceNow OAuth error: {err}")
        return _redirect("/workspaces?servicenow_error=denied")
    if not code or not state:
        return _error(400, "code and state are required")

    payload = _consume_oauth_state(state)
    if not payload:
        return _error(400, "invalid or expired state")
    workspace_id = payload["workspace_id"]
    connection_id = payload["connection_id"]

    try:
        access_token, refresh_token, expires_in = servicenow_oauth.exchange_code_for_tokens(
            connection_id=connection_id,
            code=code,
            redirect_uri=SERVICENOW_CALLBACK_URL,
        )
    except ValueError as e:
        logger.warning(f"ServiceNow token exchange failed: {e}")
        return _redirect(f"/workspaces/{workspace_id}?servicenow_error=exchange_failed")

    if refresh_token:
        servicenow_oauth.save_refresh_token(connection_id, refresh_token)

    instance_url = servicenow_oauth.get_instance_url()
    try:
        me = get_current_user(instance_url, access_token)
    except Exception:
        logger.exception("get_current_user failed; continuing without identity capture")
        me = {}

    now = datetime.now(timezone.utc).isoformat()
    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
        },
        UpdateExpression=(
            "SET last_sync_status = :s, "
            "servicenow_user_id = :uid, "
            "servicenow_user_login = :ulogin, "
            "updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":s": "queued",
            ":uid": me.get("sys_id", ""),
            ":ulogin": me.get("user_name", ""),
            ":now": now,
        },
    )

    if report_repos_snapshot is not None:
        try:
            report_repos_snapshot()
        except Exception:
            logger.exception("report_repos_snapshot failed (non-fatal)")

    log_admin_action(
        AUDIT_TABLE,
        ACTION_OAUTH_CONNECTION_ESTABLISHED,
        workspace_id=workspace_id,
        user_email=payload.get("user_email", ""),
        details={"provider": "servicenow", "connection_id": connection_id},
    )

    _queue_sync(workspace_id, connection_id)

    return _redirect(f"/workspaces/{workspace_id}?tab=integrations&servicenow=connected")


def list_connections(user_email: str, workspace_id: str) -> Dict:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can view integrations")

    connections: List[Dict] = []
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":sk": "SERVICENOW_CONNECTION#",
        },
    }
    while True:
        page = table.query(**query_kwargs)
        for item in page.get("Items", []) or []:
            connections.append(_scrub(item))
        if "LastEvaluatedKey" not in page:
            break
        query_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]

    return _response(200, {"connections": connections})


def trigger_sync(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can trigger syncs")
    conn = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
        }
    ).get("Item")
    if not conn:
        return _error(404, "connection not found")
    _queue_sync(workspace_id, connection_id, cursor=conn.get("config", {}).get("cursor"))
    return _response(202, {"queued": True})


def delete_connection(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can disconnect")

    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
        },
    )
    servicenow_oauth.delete_tokens(connection_id)

    _get_sqs().send_message(
        QueueUrl=DELETE_QUEUE,
        MessageBody=json.dumps(
            {
                "source": "servicenow",
                "workspace_id": workspace_id,
                "connection_id": connection_id,
            }
        ),
    )

    if report_repos_snapshot is not None:
        try:
            report_repos_snapshot()
        except Exception:
            logger.exception("report_repos_snapshot failed (non-fatal)")

    return _response(200, {"disconnected": True})


def _queue_sync(workspace_id: str, connection_id: str, cursor: Optional[str] = None) -> None:
    _get_sqs().send_message(
        QueueUrl=SERVICENOW_SYNC_QUEUE,
        MessageBody=json.dumps(
            {
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "cursor": cursor or "",
            }
        ),
    )


def _scrub(item: Dict[str, Any]) -> Dict[str, Any]:
    """Strip DDB internals from a connection row before returning to UI."""
    view = {k: v for k, v in item.items() if k not in ("PK", "SK", "GSI2PK", "GSI2SK", "entity_type")}
    return view


# ============================================================================
# Entrypoint
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
    logger.info(f"ServiceNow API: {http_method} {path}")

    parts = _parse_path(path)

    # /servicenow/callback -- no workspace_id in path, state row carries it.
    if parts == ("servicenow", "callback") and http_method in ("GET", "POST"):
        return handle_callback(event)

    # Everything else requires x-user-email.
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    # GET /workspaces/{ws}/servicenow/authorize
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "servicenow"
        and parts[3] == "authorize"
        and http_method == "GET"
    ):
        try:
            guard_active("servicenow_authorize", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("servicenow_authorize")
        return handle_authorize(event, user_email, parts[1])

    # GET /workspaces/{ws}/servicenow/connections
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "servicenow"
        and parts[3] == "connections"
        and http_method == "GET"
    ):
        return list_connections(user_email, parts[1])

    # POST /workspaces/{ws}/servicenow/connections/{id}/sync
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "servicenow"
        and parts[3] == "connections"
        and parts[5] == "sync"
        and http_method == "POST"
    ):
        try:
            guard_active("servicenow_trigger_sync", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("servicenow_trigger_sync")
        return trigger_sync(user_email, parts[1], parts[4])

    # DELETE /workspaces/{ws}/servicenow/connections/{id}
    if (
        len(parts) == 5
        and parts[0] == "workspaces"
        and parts[2] == "servicenow"
        and parts[3] == "connections"
        and http_method == "DELETE"
    ):
        try:
            guard_active("servicenow_disconnect", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("servicenow_disconnect")
        return delete_connection(user_email, parts[1], parts[4])

    return _error(404, f"Not found: {http_method} {path}")
