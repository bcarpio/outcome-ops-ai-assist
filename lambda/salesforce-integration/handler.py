# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Salesforce Integration Lambda for OutcomeOps AI Assist.

Handles OAuth authorize/callback + connection lifecycle for the deploy's
configured Salesforce Connected App. One Connected App per deploy;
workspace admins connect any Salesforce org (prod or sandbox) via the
same flow. Sandbox flag is chosen at connect time and persists on the
connection row.

API surface:

    POST   /workspaces/{ws}/salesforce/connect     (body: {is_sandbox})
    GET    /workspaces/{ws}/salesforce/authorize
    GET    /salesforce/callback                    (no workspace auth; state-signed)
    GET    /workspaces/{ws}/salesforce/connections
    POST   /workspaces/{ws}/salesforce/connections/{id}/sync
    DELETE /workspaces/{ws}/salesforce/connections/{id}
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
from shared.oauth import salesforce as salesforce_oauth  # noqa: E402


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
AUDIT_TABLE = os.environ.get("AUDIT_TABLE")
SALESFORCE_SYNC_QUEUE = os.environ.get("SALESFORCE_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
SALESFORCE_STATE_TTL_SECONDS = int(
    os.environ.get("SALESFORCE_STATE_TTL_SECONDS", "600")
)
SALESFORCE_CALLBACK_URL = os.environ.get("SALESFORCE_CALLBACK_URL", "")


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
    return {"statusCode": 302, "headers": {"Location": url}, "body": ""}


def _error(status_code: int, message: str) -> Dict[str, Any]:
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict[str, Any]) -> Optional[str]:
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    return headers.get("x-user-email")


def _parse_path(path: str) -> Tuple[str, ...]:
    return tuple(p for p in path.strip("/").split("/") if p)


# ============================================================================
# Auth helpers (workspace-admin gate)
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
        (datetime.now(timezone.utc) + timedelta(seconds=SALESFORCE_STATE_TTL_SECONDS)).timestamp()
    )
    _get_dynamodb().Table(WORKSPACES_TABLE).put_item(
        Item={
            "PK": f"SALESFORCE_STATE#{state}",
            "SK": f"SALESFORCE_STATE#{state}",
            "entity_type": "salesforce_state",
            "payload": payload,
            "ttl": ttl_epoch,
        }
    )


def _consume_oauth_state(state: str) -> Optional[Dict[str, Any]]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    key = {"PK": f"SALESFORCE_STATE#{state}", "SK": f"SALESFORCE_STATE#{state}"}
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


_SF_CONN_SK = "SALESFORCE_CONNECTION#"


def handle_connect(event: Dict, body: Dict, user_email: str, workspace_id: str) -> Dict:
    """POST /workspaces/{ws}/salesforce/connect

    Creates a pending connection row with the workspace admin's chosen
    sandbox flag. The subsequent GET /authorize call picks the right
    login host based on this. Idempotent -- if there's already a
    pending connection with the SAME sandbox flag, reuse it.
    """
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can configure integrations")

    is_sandbox = bool(body.get("is_sandbox", False))

    # Look for an existing needs_oauth row with matching sandbox flag.
    connection_id = _find_pending_connection(table, workspace_id, is_sandbox)
    if not connection_id:
        connection_id = str(uuid.uuid4())

    now = datetime.now(timezone.utc).isoformat()
    dual_write.ddb_put_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"{_SF_CONN_SK}{connection_id}",
            "GSI2PK": "SALESFORCE#connections",
            "GSI2SK": f"WS#{workspace_id}#CONN#{connection_id}",
            "entity_type": "salesforce_connection",
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "is_sandbox": is_sandbox,
            "last_sync_status": "needs_oauth",
            "config": {"cursors": {}},
            "created_at": now,
            "updated_at": now,
            "created_by": user_email,
        },
    )
    return _response(200, {"connection_id": connection_id, "next": "authorize"})


def handle_authorize(event: Dict, user_email: str, workspace_id: str) -> Dict:
    """GET /workspaces/{ws}/salesforce/authorize?connection_id={id}

    Returns the Salesforce authorize URL for the pending connection's
    sandbox flag. Requires a prior POST /connect to have created the
    connection row (or a needs_oauth row to already exist)."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can configure integrations")

    if not SALESFORCE_CALLBACK_URL:
        return _error(500, "SALESFORCE_CALLBACK_URL not configured; deploy needs a domain")
    try:
        salesforce_oauth.get_client_id()
    except ValueError as e:
        return _error(500, f"Salesforce integration not fully configured: {e}")

    query = event.get("queryStringParameters") or {}
    connection_id = query.get("connection_id")
    if not connection_id:
        return _error(400, "connection_id required (POST /connect first)")

    conn = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"{_SF_CONN_SK}{connection_id}",
        }
    ).get("Item")
    if not conn:
        return _error(404, "connection not found; run POST /connect first")

    is_sandbox = bool(conn.get("is_sandbox"))
    state = secrets.token_urlsafe(32)
    _put_oauth_state(
        state,
        {
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "user_email": user_email,
            "is_sandbox": is_sandbox,
        },
    )
    authorize_url = salesforce_oauth.build_authorize_url(
        is_sandbox=is_sandbox,
        redirect_uri=SALESFORCE_CALLBACK_URL,
        state=state,
    )
    return _response(200, {"authorize_url": authorize_url, "connection_id": connection_id})


def _find_pending_connection(
    table, workspace_id: str, is_sandbox: bool,
) -> Optional[str]:
    """Look for an existing needs_oauth row with matching sandbox flag."""
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":sk": _SF_CONN_SK,
        },
    }
    while True:
        page = table.query(**query_kwargs)
        for item in page.get("Items", []) or []:
            if (
                item.get("last_sync_status") == "needs_oauth"
                and bool(item.get("is_sandbox")) == is_sandbox
            ):
                return item.get("connection_id")
        if "LastEvaluatedKey" not in page:
            return None
        query_kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]


def handle_callback(event: Dict) -> Dict:
    """/salesforce/callback

    Accepts code + state from EITHER querystring (GET-direct) or JSON
    body (POST-via-Express-proxy). The proxy POSTs the params to
    sidestep SigV4 signature-vs-querystring quirks on the Function URL.
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
        logger.warning(f"Salesforce OAuth error: {err}")
        return _redirect("/workspaces?salesforce_error=denied")
    if not code or not state:
        return _error(400, "code and state are required")

    payload = _consume_oauth_state(state)
    if not payload:
        return _error(400, "invalid or expired state")
    workspace_id = payload["workspace_id"]
    connection_id = payload["connection_id"]
    is_sandbox = bool(payload.get("is_sandbox"))

    try:
        token_response = salesforce_oauth.exchange_code_for_tokens(
            connection_id=connection_id,
            is_sandbox=is_sandbox,
            code=code,
            redirect_uri=SALESFORCE_CALLBACK_URL,
        )
    except ValueError as e:
        logger.warning(f"Salesforce token exchange failed: {e}")
        return _redirect(f"/workspaces/{workspace_id}?salesforce_error=exchange_failed")

    refresh_token = token_response.get("refresh_token") or ""
    if refresh_token:
        salesforce_oauth.save_refresh_token(connection_id, refresh_token)
    else:
        logger.warning(
            f"Salesforce token response missing refresh_token for "
            f"connection {connection_id}"
        )

    instance_url = token_response.get("instance_url") or ""
    identity_url = token_response.get("id") or ""
    salesforce_org_id, salesforce_user_id = salesforce_oauth.parse_org_and_user_id(
        identity_url,
    )

    now = datetime.now(timezone.utc).isoformat()
    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"{_SF_CONN_SK}{connection_id}",
        },
        UpdateExpression=(
            "SET last_sync_status = :s, "
            "instance_url = :url, "
            "salesforce_org_id = :oid, "
            "salesforce_user_id = :uid, "
            "identity_url = :idurl, "
            "updated_at = :now"
        ),
        ExpressionAttributeValues={
            ":s": "queued",
            ":url": instance_url,
            ":oid": salesforce_org_id,
            ":uid": salesforce_user_id,
            ":idurl": identity_url,
            ":now": now,
        },
    )


    log_admin_action(
        AUDIT_TABLE,
        ACTION_OAUTH_CONNECTION_ESTABLISHED,
        workspace_id=workspace_id,
        user_email=payload.get("user_email", ""),
        details={"provider": "salesforce", "connection_id": connection_id},
    )

    _queue_sync(workspace_id, connection_id)

    return _redirect(f"/workspaces/{workspace_id}?tab=integrations&salesforce=connected")


def list_connections(user_email: str, workspace_id: str) -> Dict:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can view integrations")

    connections: List[Dict] = []
    query_kwargs = {
        "KeyConditionExpression": "PK = :pk AND begins_with(SK, :sk)",
        "ExpressionAttributeValues": {
            ":pk": f"WS#{workspace_id}",
            ":sk": _SF_CONN_SK,
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
            "SK": f"{_SF_CONN_SK}{connection_id}",
        }
    ).get("Item")
    if not conn:
        return _error(404, "connection not found")
    _queue_sync(
        workspace_id,
        connection_id,
        cursors=conn.get("config", {}).get("cursors") or {},
    )
    return _response(202, {"queued": True})


def delete_connection(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can disconnect")

    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"{_SF_CONN_SK}{connection_id}",
        },
    )
    salesforce_oauth.delete_tokens(connection_id)

    # delete-worker dispatches on the `action` field; without it the
    # worker logs "Unknown action: None" and skips, leaving orphaned
    # objects/vectors for every disconnected connection. Matches the
    # sync worker's write prefix at
    # workspaces/{ws}/salesforce/{conn}/{object_type}/... so
    # delete_prefix cleans up records + files in one message.
    # delete-worker dispatches on the `action` field; without it the
    # worker logs "Unknown action: None" and skips, leaving orphaned
    # objects/vectors for every disconnect. The prefix is APPENDED to
    # workspaces/{ws}/{source}/ by handle_delete_prefix, so passing
    # "{conn}/" cleans all records + files under the connection in
    # one message (workspaces/{ws}/salesforce/{conn}/*).
    _get_sqs().send_message(
        QueueUrl=DELETE_QUEUE,
        MessageBody=json.dumps(
            {
                "action": "delete_prefix",
                "source": "salesforce",
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "prefix": f"{connection_id}/",
            }
        ),
    )


    return _response(200, {"disconnected": True})


def _queue_sync(
    workspace_id: str,
    connection_id: str,
    cursors: Optional[Dict[str, str]] = None,
) -> None:
    _get_sqs().send_message(
        QueueUrl=SALESFORCE_SYNC_QUEUE,
        MessageBody=json.dumps(
            {
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "cursors": cursors or {},
            }
        ),
    )


def _scrub(item: Dict[str, Any]) -> Dict[str, Any]:
    """Strip DDB internals from a connection row before returning to UI.

    Legacy rows written before schema stabilization may lack the
    connection_id attribute; extract it from the SK.
    """
    view = {
        k: v for k, v in item.items()
        if k not in ("PK", "SK", "GSI2PK", "GSI2SK", "entity_type")
    }
    if not view.get("connection_id"):
        sk = item.get("SK", "")
        if sk.startswith(_SF_CONN_SK):
            view["connection_id"] = sk[len(_SF_CONN_SK):]
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
    logger.info(f"Salesforce API: {http_method} {path}")

    parts = _parse_path(path)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    # /salesforce/callback -- state row carries workspace_id.
    if parts == ("salesforce", "callback") and http_method in ("GET", "POST"):
        return handle_callback(event)

    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    # POST /workspaces/{ws}/salesforce/connect
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "salesforce"
        and parts[3] == "connect"
        and http_method == "POST"
    ):
        try:
            guard_active("salesforce_connect", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("salesforce_connect")
        return handle_connect(event, body, user_email, parts[1])

    # GET /workspaces/{ws}/salesforce/authorize
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "salesforce"
        and parts[3] == "authorize"
        and http_method == "GET"
    ):
        try:
            guard_active("salesforce_authorize", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("salesforce_authorize")
        return handle_authorize(event, user_email, parts[1])

    # GET /workspaces/{ws}/salesforce/connections
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "salesforce"
        and parts[3] == "connections"
        and http_method == "GET"
    ):
        return list_connections(user_email, parts[1])

    # POST /workspaces/{ws}/salesforce/connections/{id}/sync
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "salesforce"
        and parts[3] == "connections"
        and parts[5] == "sync"
        and http_method == "POST"
    ):
        try:
            guard_active("salesforce_trigger_sync", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("salesforce_trigger_sync")
        return trigger_sync(user_email, parts[1], parts[4])

    # DELETE /workspaces/{ws}/salesforce/connections/{id}
    if (
        len(parts) == 5
        and parts[0] == "workspaces"
        and parts[2] == "salesforce"
        and parts[3] == "connections"
        and http_method == "DELETE"
    ):
        try:
            guard_active("salesforce_disconnect", user_email=user_email, workspace_id=parts[1])
        except RegionPassiveError:
            return passive_response("salesforce_disconnect")
        return delete_connection(user_email, parts[1], parts[4])

    return _error(404, f"Not found: {http_method} {path}")
