"""
Confluence Integration Lambda for OutcomeOps AI Assist.

Handles OAuth flow and connection management for Confluence Cloud.

API Endpoints:
    GET  /confluence/authorize?workspace_id={id}     - Get OAuth URL
    GET  /confluence/callback                        - OAuth callback (no auth)
    GET  /workspaces/{id}/confluence/connections     - List connections
    GET  /workspaces/{id}/confluence/connections/{connId}/spaces - List spaces
    PUT  /workspaces/{id}/confluence/connections/{connId}/spaces - Update spaces
    POST /workspaces/{id}/confluence/connections/{connId}/sync   - Trigger sync
    DELETE /workspaces/{id}/confluence/connections/{connId}      - Disconnect

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
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
from shared.audit_writer import (  # noqa: E402
    ACTION_OAUTH_CONNECTION_ESTABLISHED,
    log_admin_action,
)

from confluence_api import (
    ConfluenceAPIError,
    ConfluenceAuthError,
    get_accessible_resources,
    list_all_spaces,
)
from token_manager import (
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
CONFLUENCE_SYNC_QUEUE = os.environ.get("CONFLUENCE_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# OAuth configuration (loaded from SSM at startup)
_oauth_config = None

# Atlassian OAuth endpoints
ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"

# OAuth scopes for Confluence
OAUTH_SCOPES = [
    "read:space:confluence",
    "read:page:confluence",
    "read:content-details:confluence",
    "read:comment:confluence",
    "read:attachment:confluence",
    "read:confluence-content.all",
    "readonly:content.attachment:confluence",
    "offline_access",
]

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
    """Load OAuth configuration from SSM."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    ssm = _get_ssm()
    prefix = f"/{ENV}/{APP_NAME}/oauth/atlassian"

    try:
        # Get client ID
        client_id_response = ssm.get_parameter(Name=f"{prefix}/client-id")
        client_id = client_id_response["Parameter"]["Value"]

        # Get client secret (encrypted)
        client_secret_response = ssm.get_parameter(
            Name=f"{prefix}/client-secret",
            WithDecryption=True,
        )
        client_secret = client_secret_response["Parameter"]["Value"]

        # Get callback URL
        callback_url_response = ssm.get_parameter(Name=f"{prefix}/callback-url")
        callback_url = callback_url_response["Parameter"]["Value"]

        _oauth_config = {
            "client_id": client_id,
            "client_secret": client_secret,
            "callback_url": callback_url,
        }
        return _oauth_config
    except ClientError as e:
        logger.error(f"Failed to load OAuth config from SSM: {e}")
        raise


# ============================================================================
# Response Helpers
# ============================================================================


def _response(status_code: int, body: Dict) -> Dict:
    """Build HTTP response."""
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
        },
        "body": json.dumps(body, default=str),
    }


def _redirect(url: str) -> Dict:
    """Build redirect response."""
    return {
        "statusCode": 302,
        "headers": {"Location": url},
        "body": "",
    }


def _error(status_code: int, message: str) -> Dict:
    """Build error response."""
    return _response(status_code, {"error": message})


def _get_user_from_request(event: Dict) -> Optional[str]:
    """Extract user email from request headers."""
    headers = event.get("headers") or {}
    return headers.get("x-user-email")


def _parse_path(path: str) -> Tuple[str, ...]:
    """Parse path into components."""
    parts = [p for p in path.strip("/").split("/") if p]
    return tuple(parts)


# ============================================================================
# Workspace Helpers
# ============================================================================


def _get_membership(table, user_email: str, workspace_id: str) -> Optional[Dict]:
    """Get user's membership in a workspace."""
    try:
        response = table.get_item(
            Key={"PK": f"USER#{user_email}", "SK": f"WS_MEMBER#{workspace_id}"}
        )
        return response.get("Item")
    except ClientError:
        return None


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    """Check if user is a workspace admin or org admin."""
    membership = _get_membership(table, user_email, workspace_id)
    if membership and membership.get("role") == "workspace_admin":
        return True
    # Check org admin
    try:
        resp = table.get_item(Key={"PK": f"USER#{user_email}", "SK": "ORG_MEMBER"})
        item = resp.get("Item")
        return item is not None and item.get("org_role") == "org_admin"
    except ClientError:
        return False


# ============================================================================
# Connection Operations
# ============================================================================


def generate_id() -> str:
    """Generate a unique ID."""
    return secrets.token_urlsafe(16)


def get_authorize_url(workspace_id: str) -> Dict:
    """
    Generate OAuth authorization URL.

    Args:
        workspace_id: Workspace to connect

    Returns:
        Response with auth_url
    """
    config = _load_oauth_config()

    # State contains workspace context
    state = json.dumps({"workspace_id": workspace_id})
    encoded_state = urllib.parse.quote(state)

    params = {
        "audience": "api.atlassian.com",
        "client_id": config["client_id"],
        "scope": " ".join(OAUTH_SCOPES),
        "redirect_uri": config["callback_url"],
        "state": encoded_state,
        "response_type": "code",
        "prompt": "consent",
    }

    auth_url = f"{ATLASSIAN_AUTH_URL}?{urllib.parse.urlencode(params)}"

    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict, body: Dict = None) -> Dict:
    """
    Handle OAuth callback from Atlassian.

    Creates or updates connection for the workspace.
    Accepts params from query string (GET) or body (POST).
    """
    # Parse parameters from query string or body
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
        # State might be URL-encoded or plain JSON
        if state.startswith("%"):
            state = urllib.parse.unquote(state)
        state_data = json.loads(state)
        workspace_id = state_data.get("workspace_id")
    except (json.JSONDecodeError, KeyError):
        return _response(400, {"redirect": "/?error=invalid_state"})

    config = _load_oauth_config()

    # Exchange code for tokens
    access_token, refresh_token, expires_in = exchange_code_for_tokens(
        code=code,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
        redirect_uri=config["callback_url"],
    )

    if not access_token:
        return _response(200, {"redirect": "/?error=token_exchange_failed"})

    # Get accessible Confluence sites
    try:
        resources = get_accessible_resources(access_token)
    except ConfluenceAPIError as e:
        logger.error(f"Failed to get accessible resources: {e}")
        return _response(200, {"redirect": "/?error=api_error"})

    if not resources:
        return _response(200, {"redirect": "/?error=no_confluence_access"})

    # Use the first Confluence site (most common case)
    resource = resources[0]
    cloud_id = resource.get("id")
    site_name = resource.get("name", "Confluence")
    site_url = resource.get("url", "")

    # Create connection
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    connection_id = generate_id()
    now = datetime.now(timezone.utc).isoformat()

    connection = {
        "PK": f"WS#{workspace_id}",
        "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "cloud_id": cloud_id,
        "site_name": site_name,
        "site_url": site_url,
        "selected_spaces": [],
        "include_attachments": False,
        "last_sync_at": None,
        "last_sync_status": "needs_configuration",
        "page_count": 0,
        "attachment_count": 0,
        "created_at": now,
        "updated_at": now,
    }

    try:
        table.put_item(Item=connection)

        # Save tokens
        save_tokens(connection_id, access_token, refresh_token, expires_in)

        logger.info(f"Created Confluence connection {connection_id} for workspace {workspace_id}")

        # Emit OAUTH_CONNECTION_ESTABLISHED audit row.
        try:
            log_admin_action(
                os.environ.get("AUDIT_TABLE", ""),
                ACTION_OAUTH_CONNECTION_ESTABLISHED,
                workspace_id=workspace_id,
                user_email="",  # OAuth callback - user identity comes from cookie at ALB
                source_ip="",
                user_agent="",
                details={
                    "provider": "confluence",
                    "connection_id": connection_id,
                    "account_label": site_name,
                    "site_url": site_url,
                    "cloud_id": cloud_id,
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit OAUTH_CONNECTION_ESTABLISHED: {e}")

        # Return redirect URL for configuration page
        return _response(200, {"redirect": f"/workspaces/{workspace_id}/settings?tab=integrations&configure={connection_id}"})

    except ClientError as e:
        logger.error(f"Failed to create connection: {e}")
        return _response(200, {"redirect": "/?error=database_error"})


def list_connections(user_email: str, workspace_id: str) -> Dict:
    """List Confluence connections for a workspace."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    # Check membership
    membership = _get_membership(table, user_email, workspace_id)
    if not membership:
        return _error(403, "Not a member of this workspace")

    try:
        response = table.query(
            KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
            ExpressionAttributeValues={
                ":pk": f"WS#{workspace_id}",
                ":sk": "CONFLUENCE_CONNECTION#",
            },
        )
        connections = response.get("Items", [])

        # Remove internal fields, backfill connection_id from SK if missing
        for conn in connections:
            sk = conn.pop("SK", None)
            conn.pop("PK", None)
            if not conn.get("connection_id") and sk:
                conn["connection_id"] = sk.replace("CONFLUENCE_CONNECTION#", "")
            # Ensure selected_spaces is always a list
            if conn.get("selected_spaces") is None:
                conn["selected_spaces"] = []

        return _response(200, {"connections": connections})
    except ClientError as e:
        logger.error(f"Failed to list connections: {e}")
        return _error(500, "Failed to list connections")


def get_connection(user_email: str, workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a connection by ID."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    # Check membership
    membership = _get_membership(table, user_email, workspace_id)
    if not membership:
        return None

    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError:
        return None


def list_spaces(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """List available Confluence spaces for a connection."""
    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    config = _load_oauth_config()
    cloud_id = connection.get("cloud_id")

    # Get fresh access token
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=config["client_id"],
        client_secret=config["client_secret"],
    )

    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        spaces = list_all_spaces(access_token, cloud_id)

        # Add selected status
        selected_ids = {s["id"] for s in connection.get("selected_spaces", [])}
        for space in spaces:
            space["selected"] = space.get("id") in selected_ids

        return _response(200, {
            "spaces": spaces,
            "selected_count": len(selected_ids),
        })
    except ConfluenceAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except ConfluenceAPIError as e:
        logger.error(f"Failed to list spaces: {e}")
        return _error(500, f"Failed to list spaces: {e}")


def update_spaces(
    user_email: str,
    workspace_id: str,
    connection_id: str,
    body: Dict,
) -> Dict:
    """Update selected spaces and optionally trigger sync."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    # Admin only
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update spaces")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    selected_spaces = body.get("selected_spaces", [])
    include_attachments = body.get("include_attachments", False)

    now = datetime.now(timezone.utc).isoformat()

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
            },
            UpdateExpression="SET selected_spaces = :spaces, include_attachments = :attach, updated_at = :now, last_sync_status = :status",
            ExpressionAttributeValues={
                ":spaces": selected_spaces,
                ":attach": include_attachments,
                ":now": now,
                ":status": "queued",
            },
        )

        # Queue sync job
        if CONFLUENCE_SYNC_QUEUE and selected_spaces:
            _queue_sync(workspace_id, connection_id)

        return _response(200, {
            "message": "Spaces updated",
            "selected_count": len(selected_spaces),
        })
    except ClientError as e:
        logger.error(f"Failed to update spaces: {e}")
        return _error(500, "Failed to update spaces")


def trigger_sync(user_email: str, workspace_id: str, connection_id: str) -> Dict:
    """Manually trigger a sync for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    # Admin only
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can trigger sync")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    if not connection.get("selected_spaces"):
        return _error(400, "No spaces selected for sync")

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
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
    """Delete a Confluence connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    # Admin only
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can delete connections")

    connection = get_connection(user_email, workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    try:
        # Queue deletion of synced documents for each selected space
        selected_spaces = connection.get("selected_spaces", [])
        if DELETE_QUEUE and selected_spaces:
            sqs = _get_sqs()
            for space in selected_spaces:
                space_name = space.get("name", space.get("key", ""))
                if space_name:
                    sqs.send_message(
                        QueueUrl=DELETE_QUEUE,
                        MessageBody=json.dumps({
                            "action": "delete_prefix",
                            "workspace_id": workspace_id,
                            "source": "confluence",
                            "prefix": space_name + "/",
                        }),
                    )
            logger.info(f"Queued deletion of {len(selected_spaces)} spaces for connection {connection_id}")

        # Delete connection
        table.delete_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
            }
        )

        # Delete tokens
        delete_tokens(connection_id)

        logger.info(f"Deleted Confluence connection {connection_id}")
        return _response(200, {"message": "Connection deleted"})
    except ClientError as e:
        logger.error(f"Failed to delete connection: {e}")
        return _error(500, "Failed to delete connection")


def _queue_sync(workspace_id: str, connection_id: str):
    """Queue a sync job to SQS."""
    if not CONFLUENCE_SYNC_QUEUE:
        logger.warning("CONFLUENCE_SYNC_QUEUE not configured")
        return

    sqs = _get_sqs()
    try:
        sqs.send_message(
            QueueUrl=CONFLUENCE_SYNC_QUEUE,
            MessageBody=json.dumps({
                "workspace_id": workspace_id,
                "connection_id": connection_id,
                "queued_at": datetime.now(timezone.utc).isoformat(),
            }),
        )
        logger.info(f"Queued sync for connection {connection_id}")
    except ClientError as e:
        logger.error(f"Failed to queue sync: {e}")


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    Main Lambda handler for Confluence integration.
    Routes requests based on HTTP method and path.
    """
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    logger.info(f"Confluence API: {http_method} {path}")

    # Parse path
    parts = _parse_path(path)

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}

    # OAuth routes
    # GET /confluence/authorize?workspace_id=...
    if parts == ("confluence", "authorize") and http_method == "GET":
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

    # GET or POST /confluence/callback
    if parts == ("confluence", "callback") and http_method in ("GET", "POST"):
        return handle_callback(event, body)

    # All other routes require authentication
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    # GET /workspaces/{id}/confluence/connections
    if (
        len(parts) == 4
        and parts[0] == "workspaces"
        and parts[2] == "confluence"
        and parts[3] == "connections"
        and http_method == "GET"
    ):
        return list_connections(user_email, parts[1])

    # GET /workspaces/{id}/confluence/connections/{connId}/spaces
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "confluence"
        and parts[3] == "connections"
        and parts[5] == "spaces"
        and http_method == "GET"
    ):
        return list_spaces(user_email, parts[1], parts[4])

    # PUT /workspaces/{id}/confluence/connections/{connId}/spaces
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "confluence"
        and parts[3] == "connections"
        and parts[5] == "spaces"
        and http_method == "PUT"
    ):
        return update_spaces(user_email, parts[1], parts[4], body)

    # POST /workspaces/{id}/confluence/connections/{connId}/sync
    if (
        len(parts) == 6
        and parts[0] == "workspaces"
        and parts[2] == "confluence"
        and parts[3] == "connections"
        and parts[5] == "sync"
        and http_method == "POST"
    ):
        return trigger_sync(user_email, parts[1], parts[4])

    # DELETE /workspaces/{id}/confluence/connections/{connId}
    if (
        len(parts) == 5
        and parts[0] == "workspaces"
        and parts[2] == "confluence"
        and parts[3] == "connections"
        and http_method == "DELETE"
    ):
        return delete_connection(user_email, parts[1], parts[4])

    return _error(404, "Not found")
