# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
OneDrive Integration Lambda for OutcomeOps AI Assist.

Handles OAuth flow, folder-scope picker, and connection management for
Microsoft OneDrive. Mirrors the SharePoint integration shape; key
difference is "folders" instead of "sites" - OneDrive users pick
sub-folders to limit what gets ingested.

API Endpoints:
    GET  /onedrive/authorize?workspace_id={id}                       - Get OAuth URL
    GET  /onedrive/callback                                          - OAuth callback (no auth)
    GET  /workspaces/{id}/onedrive/connections                       - List connections
    GET  /workspaces/{id}/onedrive/connections/{connId}              - Get connection details
    GET  /workspaces/{id}/onedrive/connections/{connId}/folders      - List available folders
    PUT  /workspaces/{id}/onedrive/connections/{connId}/folders      - Update selected folders
    POST /workspaces/{id}/onedrive/connections/{connId}/sync         - Trigger sync
    DELETE /workspaces/{id}/onedrive/connections/{connId}            - Disconnect

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
The x-user-email header is set by the proxy after Azure AD OIDC validation.
"""

import json
import logging
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
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


from onedrive_api import (
    OneDriveAPIError,
    OneDriveAuthError,
    get_user_drive,
    get_user_profile,
    list_root_children,
    is_folder,
)
from token_manager import (
    ONEDRIVE_SCOPES,
    delete_tokens,
    exchange_code_for_tokens,
    refresh_access_token,
    save_tokens,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
ONEDRIVE_SYNC_QUEUE = os.environ.get("ONEDRIVE_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# OAuth configuration (loaded from SSM at startup)
_oauth_config = None

# Microsoft OAuth endpoints
MICROSOFT_AUTH_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/authorize"

# Cached clients
_dynamodb = None
_ssm = None
_sqs = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs")
    return _sqs


def _load_oauth_config() -> Dict[str, str]:
    """Load Microsoft OAuth configuration from SSM."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    ssm = _get_ssm()
    prefix = f"/{ENV}/{APP_NAME}/oauth/microsoft"

    try:
        client_id_response = ssm.get_parameter(Name=f"{prefix}/client-id")
        client_id = client_id_response["Parameter"]["Value"]

        client_secret_response = ssm.get_parameter(
            Name=f"{prefix}/client-secret",
            WithDecryption=True,
        )
        client_secret = client_secret_response["Parameter"]["Value"]

        callback_url_response = ssm.get_parameter(Name=f"{prefix}/onedrive-callback-url")
        callback_url = callback_url_response["Parameter"]["Value"]

        _oauth_config = {
            "client_id": client_id,
            "client_secret": client_secret,
            "callback_url": callback_url,
        }
        return _oauth_config
    except ClientError as e:
        logger.error(f"Failed to load Microsoft OAuth config from SSM: {e}")
        raise


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


def _redirect(url: str) -> Dict:
    return {
        "statusCode": 302,
        "headers": {"Location": url},
        "body": "",
    }


def _error(status_code: int, message: str) -> Dict:
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict) -> Optional[str]:
    headers = event.get("headers") or {}
    return headers.get("x-user-email")


def _parse_path(path: str) -> Tuple[str, ...]:
    parts = [p for p in path.strip("/").split("/") if p]
    return tuple(parts)


# ============================================================================
# Workspace Helpers (identical pattern to sharepoint-integration)
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


def get_authorize_url(workspace_id: str) -> Dict:
    """Generate Microsoft OAuth authorization URL for OneDrive."""
    config = _load_oauth_config()

    state = json.dumps({"workspace_id": workspace_id})
    encoded_state = urllib.parse.quote(state)

    params = {
        "client_id": config["client_id"],
        "scope": ONEDRIVE_SCOPES,
        "redirect_uri": config["callback_url"],
        "state": encoded_state,
        "response_type": "code",
        "response_mode": "query",
        "prompt": "consent",
    }

    auth_url = f"{MICROSOFT_AUTH_URL}?{urllib.parse.urlencode(params)}"

    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict, body: Dict = None) -> Dict:
    """Handle OAuth callback from Microsoft for OneDrive."""
    query_params = event.get("queryStringParameters") or {}
    body = body or {}

    code = body.get("code") or query_params.get("code")
    state = body.get("state") or query_params.get("state")
    error = body.get("error") or query_params.get("error")

    if error:
        logger.error("OAuth error: %s", error)
        return _response(200, {"redirect": "/?error=oauth_denied"})

    if not code or not state:
        return _response(400, {"redirect": "/?error=invalid_callback"})

    try:
        if state.startswith("%"):
            state = urllib.parse.unquote(state)
        state_data = json.loads(state)
        workspace_id = state_data.get("workspace_id")
    except (json.JSONDecodeError, KeyError):
        return _response(400, {"redirect": "/?error=invalid_state"})

    config = _load_oauth_config()

    access_token, refresh_token, expires_in = exchange_code_for_tokens(
        code=code,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["callback_url"],
    )

    if not access_token:
        return _response(200, {"redirect": "/?error=token_exchange_failed"})

    try:
        profile = get_user_profile(access_token)
        drive = get_user_drive(access_token)
    except OneDriveAPIError as e:
        logger.error(f"Failed to get user profile / drive: {e}")
        return _response(200, {"redirect": "/?error=api_error"})

    user_email = profile.get("mail") or profile.get("userPrincipalName", "")
    display_name = profile.get("displayName", user_email)
    drive_id = drive.get("id", "")
    drive_name = drive.get("name", "OneDrive")

    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    connection_id = generate_id()
    now = datetime.now(timezone.utc).isoformat()

    connection = {
        "PK": f"WS#{workspace_id}",
        "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "user_email": user_email,
        "display_name": display_name,
        "drive_id": drive_id,
        "drive_name": drive_name,
        "selected_folders": [],     # list of {folder_id, folder_name, folder_path}; "root" = entire drive
        "delta_tokens": {},         # per-folder delta_token (full URL strings)
        "last_sync_at": None,
        "last_sync_status": "needs_configuration",
        "file_count": 0,
        "created_at": now,
        "updated_at": now,
    }

    try:
        dual_write.ddb_put_item(WORKSPACES_TABLE, connection)
        save_tokens(connection_id, access_token, refresh_token, expires_in)

        logger.info(f"Created OneDrive connection {connection_id} for workspace {workspace_id}")

        try:
            log_admin_action(
                os.environ.get("AUDIT_TABLE", ""),
                ACTION_OAUTH_CONNECTION_ESTABLISHED,
                workspace_id=workspace_id,
                user_email=user_email or "",
                source_ip="",
                user_agent="",
                details={
                    "provider": "onedrive",
                    "connection_id": connection_id,
                    "account_label": display_name,
                    "user_email_external": user_email,
                    "drive_id": drive_id,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit OAUTH_CONNECTION_ESTABLISHED: {e}")


        return _response(200, {"redirect": f"/workspaces/{workspace_id}/settings?tab=integrations&configure_onedrive={connection_id}"})

    except ClientError as e:
        logger.error(f"Failed to create connection: {e}")
        return _response(200, {"redirect": "/?error=database_error"})


def list_connections(user_email: str, workspace_id: str) -> Dict:
    """List OneDrive connections for a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    membership = _get_membership(table, user_email, workspace_id)
    if not membership:
        return _error(403, "Not a member of this workspace")

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "ONEDRIVE_CONNECTION#",
            },
        )
        connections = response.get("Items", [])

        for conn in connections:
            sk = conn.pop("SK", None)
            conn.pop("PK", None)
            conn.pop("delta_tokens", None)
            if not conn.get("connection_id") and sk:
                conn["connection_id"] = sk.replace("ONEDRIVE_CONNECTION#", "")

        return _response(200, {"connections": connections})
    except ClientError as e:
        logger.error(f"Failed to list connections: {e}")
        return _error(500, "Failed to list connections")


def get_connection(user_email: str, workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a connection by ID. Returns None if user can't access workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    membership = _get_membership(table, user_email, workspace_id)
    if not membership:
        return None

    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError:
        return None


def get_folders(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """List top-level folders in the user's OneDrive for the picker.

    Always includes a synthetic "root" folder so users can opt to sync
    files at the drive root (not inside any folder). Other entries are
    real folders the user has at their drive root.
    """
    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    config = _load_oauth_config()

    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
    )

    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        children = list_root_children(access_token)

        folders = [
            {
                "folder_id": "root",
                "folder_name": "(root)",
                "folder_path": "/",
            }
        ]
        for item in children:
            if not is_folder(item):
                continue
            folders.append({
                "folder_id": item.get("id"),
                "folder_name": item.get("name", ""),
                "folder_path": f"/{item.get('name', '')}",
            })

        # Mark which are already selected
        selected_ids = {f.get("folder_id") for f in connection.get("selected_folders", [])}
        for f in folders:
            f["selected"] = f["folder_id"] in selected_ids

        return _response(200, {"folders": folders})
    except OneDriveAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except OneDriveAPIError as e:
        logger.error(f"Failed to list folders: {e}")
        return _error(500, f"Failed to list folders: {e}")


def update_folders(
    user_email: str,
    workspace_id: str,
    connection_id: str,
    body: Dict,
) -> Dict:
    """Update selected folders and optionally trigger sync."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update folders")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    selected_folders = body.get("selected_folders", [])

    now = datetime.now(timezone.utc).isoformat()

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
            },
            UpdateExpression="SET selected_folders = :folders, updated_at = :now, last_sync_status = :status",
            ExpressionAttributeValues={
                ":folders": selected_folders,
                ":now": now,
                ":status": "queued",
            },
        )

        if ONEDRIVE_SYNC_QUEUE and selected_folders:
            _queue_sync(workspace_id, connection_id)

        return _response(200, {
            "message": "Folders updated",
            "selected_count": len(selected_folders),
        })
    except ClientError as e:
        logger.error(f"Failed to update folders: {e}")
        return _error(500, "Failed to update folders")


def trigger_sync(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """Manually trigger a sync for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can trigger sync")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    if not connection.get("selected_folders"):
        return _error(400, "No folders selected for sync")

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
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
    """Delete a OneDrive connection - removes DDB record, tokens, and queues
    delete_prefix to wipe S3 objects + vectors."""
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
                    "source": "onedrive",
                    "prefix": f"{connection_id}/",
                }),
            )
            logger.info(f"Queued deletion of OneDrive documents for connection {connection_id}")

        dual_write.ddb_delete_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
            },
        )

        delete_tokens(connection_id)

        logger.info(f"Deleted OneDrive connection {connection_id}")


        return _response(200, {"message": "Connection deleted"})
    except ClientError as e:
        logger.error(f"Failed to delete connection: {e}")
        return _error(500, "Failed to delete connection")


def _queue_sync(workspace_id: str, connection_id: str):
    """Queue a sync job to SQS."""
    if not ONEDRIVE_SYNC_QUEUE:
        logger.warning("ONEDRIVE_SYNC_QUEUE not configured")
        return

    sqs = _get_sqs()
    try:
        sqs.send_message(
            QueueUrl=ONEDRIVE_SYNC_QUEUE,
            MessageBody=json.dumps({
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
        logger.info(f"Queued sync for OneDrive connection {connection_id}")
    except ClientError as e:
        logger.error(f"Failed to queue sync: {e}")


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler for OneDrive integration."""
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    logger.info(f"OneDrive API: {http_method} {path}")

    parts = _parse_path(path)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    # OAuth routes
    if parts == ("onedrive", "authorize") and http_method == "GET":
        user_email = _get_user_from_request(event)
        if not user_email:
            return _error(401, "User email not provided")
        query_params = event.get("queryStringParameters") or {}
        workspace_id = query_params.get("workspace_id")
        if not workspace_id:
            return _error(400, "workspace_id required")
        dynamodb = _get_dynamodb()
        table = dynamodb.Table(os.environ.get("WORKSPACES_TABLE", ""))
        if not _is_workspace_admin(table, user_email, workspace_id):
            return _error(403, "Only workspace admins can configure integrations")
        return get_authorize_url(workspace_id)

    if parts == ("onedrive", "callback") and http_method in ("GET", "POST"):
        return handle_callback(event, body)

    # All other routes require authentication
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    # GET /workspaces/{id}/onedrive/connections
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "onedrive"
        and parts[3] == "connections"
        and http_method == "GET"
    ):
        return list_connections(user_email, parts[1])

    # GET /workspaces/{id}/onedrive/connections/{connId}/folders
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "onedrive"
        and parts[3] == "connections"
        and parts[5] == "folders"
        and http_method == "GET"
    ):
        return get_folders(user_email, parts[1], parts[4])

    # PUT /workspaces/{id}/onedrive/connections/{connId}/folders
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "onedrive"
        and parts[3] == "connections"
        and parts[5] == "folders"
        and http_method == "PUT"
    ):
        try:
            guard_active(
                "onedrive_update_folders",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("onedrive_update_folders")
        return update_folders(user_email, parts[1], parts[4], body)

    # POST /workspaces/{id}/onedrive/connections/{connId}/sync
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "onedrive"
        and parts[3] == "connections"
        and parts[5] == "sync"
        and http_method == "POST"
    ):
        try:
            guard_active(
                "onedrive_trigger_sync",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("onedrive_trigger_sync")
        return trigger_sync(user_email, parts[1], parts[4])

    # DELETE /workspaces/{id}/onedrive/connections/{connId}
    if (
        len(parts) == 5
        and parts[0] == "workspaces"
        and parts[2] == "onedrive"
        and parts[3] == "connections"
        and http_method == "DELETE"
    ):
        try:
            guard_active(
                "onedrive_delete_connection",
                user_email=user_email,
                workspace_id=parts[1],
            )
        except RegionPassiveError:
            return passive_response("onedrive_delete_connection")
        return delete_connection(user_email, parts[1], parts[4])

    return _error(404, "Not found")
