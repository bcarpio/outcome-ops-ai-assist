# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Jira Integration Lambda for OutcomeOps AI Assist.

Handles OAuth flow and connection management for Jira Cloud.

API Endpoints:
    GET  /jira/authorize?workspace_id={id}              - Get OAuth URL
    GET  /jira/callback                                 - OAuth callback (no auth)
    GET  /workspaces/{id}/jira/connections              - List connections
    GET  /workspaces/{id}/jira/connections/{connId}/projects - List projects
    PUT  /workspaces/{id}/jira/connections/{connId}/projects - Update projects
    POST /workspaces/{id}/jira/connections/{connId}/sync     - Trigger sync
    DELETE /workspaces/{id}/jira/connections/{connId}        - Disconnect

Security: Invoked via IAM-authenticated Lambda Function URL from Fargate.
"""

import json
import logging
import os
import secrets
import sys
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


from jira_api import (
    JiraAPIError,
    JiraAuthError,
    get_accessible_resources,
    list_all_projects,
)
from token_manager import (
    delete_tokens,
    exchange_code_for_tokens,
    get_oauth_config,
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
JIRA_SYNC_QUEUE = os.environ.get("JIRA_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# OAuth configuration
ATLASSIAN_AUTH_URL = "https://auth.atlassian.com/authorize"

# Jira OAuth scopes
JIRA_SCOPES = [
    "read:jira-work",        # Read issues, projects, boards
    "read:jira-user",        # Read user info for assignee/reporter
    "offline_access",        # Refresh tokens
]

# Cached clients
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


def _error(status_code: int, message: str) -> Dict:
    """Build error response."""
    return _response(status_code, {"error": message})


def _redirect(url: str) -> Dict:
    """Build redirect response."""
    return {
        "statusCode": 302,
        "headers": {"Location": url},
        "body": "",
    }


# ============================================================================
# Auth Helpers
# ============================================================================


def _get_user_from_request(event: Dict) -> Optional[str]:
    """Extract user email from request headers (set by Fargate proxy)."""
    headers = event.get("headers", {})
    return headers.get("x-user-email") or headers.get("X-User-Email")


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    """Check if user is a workspace admin."""
    response = table.get_item(
        Key={
            "PK": f"USER#{user_email}",
            "SK": f"WS_MEMBER#{workspace_id}",
        }
    )
    item = response.get("Item")
    if not item:
        return False
    return item.get("role") in ("workspace_admin", "org_admin")


def _can_access_workspace(table, user_email: str, workspace_id: str) -> bool:
    """Check if user can access workspace (member or org admin)."""
    # Check direct membership
    response = table.get_item(
        Key={
            "PK": f"USER#{user_email}",
            "SK": f"WS_MEMBER#{workspace_id}",
        }
    )
    if response.get("Item"):
        return True

    # Check org admin
    response = table.get_item(
        Key={
            "PK": f"USER#{user_email}",
            "SK": "ORG_MEMBER",
        }
    )
    item = response.get("Item")
    return item and item.get("org_role") == "org_admin"


# ============================================================================
# DynamoDB Operations
# ============================================================================


def _generate_connection_id() -> str:
    """Generate a unique connection ID."""
    return f"jira_{secrets.token_urlsafe(16)}"


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def save_connection(
    connection_id: str,
    workspace_id: str,
    cloud_id: str,
    site_name: str,
    site_url: str,
    created_by: str,
) -> None:
    """Save a Jira connection."""
    now = _now_iso()

    dual_write.ddb_put_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"JIRA_CONNECTION#{connection_id}",
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "cloud_id": cloud_id,
            "site_name": site_name,
            "site_url": site_url,
            "selected_projects": [],
            "include_attachments": True,
            "include_comments": True,
            "last_sync_at": None,
            "last_sync_status": "needs_configuration",
            "last_sync_error": None,
            "issue_count": 0,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        },
    )
    logger.info("Saved Jira connection %s for workspace %s", connection_id, workspace_id)


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a connection by ID."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"JIRA_CONNECTION#{connection_id}"}
    )
    return response.get("Item")


def list_connections(workspace_id: str) -> List[Dict]:
    """List all Jira connections for a workspace."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "JIRA_CONNECTION#",
        },
    )
    return response.get("Items", [])


def delete_connection(workspace_id: str, connection_id: str) -> None:
    """Delete a connection."""
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"JIRA_CONNECTION#{connection_id}"},
    )
    logger.info(f"Deleted Jira connection {connection_id}")


def update_connection_projects(
    workspace_id: str,
    connection_id: str,
    selected_projects: List[Dict],
    include_attachments: bool,
    include_comments: bool,
) -> None:
    """Update selected projects for a connection."""
    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"JIRA_CONNECTION#{connection_id}"},
        UpdateExpression="SET selected_projects = :projects, include_attachments = :attachments, include_comments = :comments, updated_at = :now",
        ExpressionAttributeValues={
            ":projects": selected_projects,
            ":attachments": include_attachments,
            ":comments": include_comments,
            ":now": _now_iso(),
        },
    )


def update_sync_status(
    workspace_id: str,
    connection_id: str,
    status: str,
    error: Optional[str] = None,
    issue_count: Optional[int] = None,
) -> None:
    """Update sync status for a connection."""
    update_expr = "SET last_sync_status = :status, updated_at = :now"
    expr_values = {
        ":status": status,
        ":now": _now_iso(),
    }

    if status in ("success", "failed", "auth_error"):
        update_expr += ", last_sync_at = :sync_at"
        expr_values[":sync_at"] = _now_iso()

    if error is not None:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    if issue_count is not None:
        update_expr += ", issue_count = :count"
        expr_values[":count"] = issue_count

    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"JIRA_CONNECTION#{connection_id}"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )


# ============================================================================
# SQS Operations
# ============================================================================


def queue_sync_job(
    workspace_id: str,
    connection_id: str,
) -> None:
    """Queue a sync job to SQS."""
    sqs = _get_sqs()
    sqs.send_message(
        QueueUrl=JIRA_SYNC_QUEUE,
        MessageBody=json.dumps({
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "triggered_by": "manual",
        }),
    )
    logger.info(f"Queued Jira sync for {workspace_id}/{connection_id}")


def queue_delete_job(workspace_id: str, connection_id: str) -> None:
    """Queue a delete job for Jira documents."""
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured")
        return

    sqs = _get_sqs()
    sqs.send_message(
        QueueUrl=DELETE_QUEUE,
        MessageBody=json.dumps({
            "action": "delete_prefix",
            "workspace_id": workspace_id,
            "source": "jira",
            "prefix": "",  # Delete all Jira content for this workspace
        }),
    )
    logger.info(f"Queued Jira delete job for {workspace_id}")


# ============================================================================
# Route Handlers
# ============================================================================


def handle_authorize(event: Dict) -> Dict:
    """GET /jira/authorize?workspace_id=..."""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    query_params = event.get("queryStringParameters") or {}
    workspace_id = query_params.get("workspace_id")

    if not workspace_id:
        return _error(400, "workspace_id is required")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)

    # Check workspace admin access
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can connect Jira")

    # Build OAuth URL
    config = get_oauth_config()
    state = json.dumps({
        "workspace_id": workspace_id,
        "user_email": user_email,
    })

    params = {
        "audience": "api.atlassian.com",
        "client_id": config["client_id"],
        "redirect_uri": config["callback_url"],
        "response_type": "code",
        "scope": " ".join(JIRA_SCOPES),
        "state": state,
        "prompt": "consent",
    }

    auth_url = f"{ATLASSIAN_AUTH_URL}?{urllib.parse.urlencode(params)}"

    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict) -> Dict:
    """GET or POST /jira/callback - OAuth callback."""
    # Support both query params (GET) and body (POST for SigV4 compatibility)
    query_params = event.get("queryStringParameters") or {}
    code = query_params.get("code")
    state = query_params.get("state")
    error = query_params.get("error")

    # Try body if not in query params (POST callback)
    if not code and event.get("body"):
        try:
            body = json.loads(event.get("body", "{}"))
            code = code or body.get("code")
            state = state or body.get("state")
            error = error or body.get("error")
        except json.JSONDecodeError:
            pass

    # Determine redirect base URL
    if ENV == "prd":
        base_url = "https://outcomeops.outcomeops.ai"
    else:
        base_url = "https://dev.outcomeops.outcomeops.ai"

    if error:
        logger.warning("OAuth error: %s", error)
        return _redirect(f"{base_url}/workspaces?jira_error={error}")

    if not code or not state:
        return _redirect(f"{base_url}/workspaces?jira_error=missing_params")

    try:
        state_data = json.loads(state)
        workspace_id = state_data["workspace_id"]
        user_email = state_data["user_email"]
    except (json.JSONDecodeError, KeyError):
        return _redirect(f"{base_url}/workspaces?jira_error=invalid_state")

    try:
        config = get_oauth_config()

        # Exchange code for tokens
        access_token, refresh_token, expires_in = exchange_code_for_tokens(
            code, config["callback_url"]
        )

        if not access_token or not refresh_token:
            return _redirect(f"{base_url}/workspaces?jira_error=token_exchange_failed")

        # Get accessible sites
        sites = get_accessible_resources(access_token)

        if not sites:
            return _redirect(f"{base_url}/workspaces?jira_error=no_sites")

        # Use first site
        site = sites[0]
        cloud_id = site["id"]
        site_name = site["name"]
        site_url = site["url"]

        # Check for existing connection
        existing = list_connections(workspace_id)
        existing_for_site = [c for c in existing if c.get("cloud_id") == cloud_id]

        if existing_for_site:
            # Update existing connection's tokens
            connection_id = existing_for_site[0]["connection_id"]
            save_tokens(connection_id, access_token, refresh_token, expires_in)
            new_connection = False
        else:
            # Create new connection
            connection_id = _generate_connection_id()
            save_connection(
                connection_id=connection_id,
                workspace_id=workspace_id,
                cloud_id=cloud_id,
                site_name=site_name,
                site_url=site_url,
                created_by=user_email,
            )
            save_tokens(connection_id, access_token, refresh_token, expires_in)
            new_connection = True

        # Emit OAUTH_CONNECTION_ESTABLISHED on new connections only
        # (token refresh on an existing connection is captured by the
        # OAUTH_TOKEN_REFRESHED event in the refresh path).
        if new_connection:
            try:
                log_admin_action(
                    os.environ.get("AUDIT_TABLE", ""),
                    ACTION_OAUTH_CONNECTION_ESTABLISHED,
                    workspace_id=workspace_id,
                    user_email=user_email or "",
                    source_ip="",
                    user_agent="",
                    details={
                        "provider": "jira",
                        "connection_id": connection_id,
                        "account_label": site_name,
                        "site_url": site_url,
                        "cloud_id": cloud_id,
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to emit OAUTH_CONNECTION_ESTABLISHED: {e}")

        # Redirect to workspace settings
        return _redirect(f"{base_url}/workspaces/{workspace_id}/settings?tab=jira&connection={connection_id}")

    except Exception as e:
        logger.exception("OAuth callback error")
        return _redirect(f"{base_url}/workspaces?jira_error=callback_failed")


def handle_list_connections(event: Dict) -> Dict:
    """GET /workspaces/{id}/jira/connections"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    connections = list_connections(workspace_id)

    return _response(200, {
        "connections": [
            {
                "connection_id": c.get("connection_id") or c.get("SK", "").replace("JIRA_CONNECTION#", ""),
                "cloud_id": c.get("cloud_id"),
                "site_name": c.get("site_name", ""),
                "site_url": c.get("site_url", ""),
                "selected_projects": c.get("selected_projects", []),
                "include_attachments": c.get("include_attachments", True),
                "include_comments": c.get("include_comments", True),
                "last_sync_at": c.get("last_sync_at"),
                "last_sync_status": c.get("last_sync_status"),
                "last_sync_error": c.get("last_sync_error"),
                "issue_count": c.get("issue_count", 0),
                "created_at": c.get("created_at"),
            }
            for c in connections
        ]
    })


def handle_list_projects(event: Dict) -> Dict:
    """GET /workspaces/{id}/jira/connections/{connId}/projects"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    cloud_id = connection.get("cloud_id")

    try:
        access_token = refresh_access_token(connection_id)
        if not access_token:
            return _error(401, "Failed to refresh token - reconnect required")

        projects = list_all_projects(access_token, cloud_id)

        # Mark selected
        selected_keys = {p["key"] for p in connection.get("selected_projects", [])}

        return _response(200, {
            "projects": [
                {
                    "id": p["id"],
                    "key": p.get("key", ""),
                    "name": p.get("name", ""),
                    "projectTypeKey": p.get("projectTypeKey", "software"),
                    "selected": p.get("key", "") in selected_keys,
                }
                for p in projects
            ],
            "selected_count": len(selected_keys),
        })

    except JiraAuthError as e:
        update_sync_status(workspace_id, connection_id, "auth_error", str(e))
        return _error(401, "Authentication failed - reconnect required")
    except JiraAPIError as e:
        return _error(502, f"Jira API error: {e}")


def handle_update_projects(event: Dict) -> Dict:
    """PUT /workspaces/{id}/jira/connections/{connId}/projects"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update projects")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    project_keys = body.get("project_keys", [])
    include_attachments = body.get("include_attachments", True)
    include_comments = body.get("include_comments", True)

    if not project_keys:
        return _error(400, "At least one project must be selected")

    cloud_id = connection.get("cloud_id")

    try:
        access_token = refresh_access_token(connection_id)
        if not access_token:
            return _error(401, "Failed to refresh token")

        all_projects = list_all_projects(access_token, cloud_id)
        project_map = {p["key"]: p for p in all_projects}

        selected_projects = []
        for key in project_keys:
            if key in project_map:
                p = project_map[key]
                selected_projects.append({
                    "id": p["id"],
                    "key": p.get("key", ""),
                    "name": p.get("name", ""),
                })

        if not selected_projects:
            return _error(400, "No valid projects selected")

        update_connection_projects(
            workspace_id, connection_id, selected_projects,
            include_attachments, include_comments
        )

        # Queue sync
        update_sync_status(workspace_id, connection_id, "queued")
        queue_sync_job(workspace_id, connection_id)


        return _response(200, {
            "message": "Projects updated, sync queued",
            "selected_count": len(selected_projects),
        })

    except JiraAuthError as e:
        return _error(401, "Authentication failed - reconnect required")
    except JiraAPIError as e:
        return _error(502, f"Jira API error: {e}")


def handle_sync(event: Dict) -> Dict:
    """POST /workspaces/{id}/jira/connections/{connId}/sync"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    if not connection.get("selected_projects"):
        return _error(400, "No projects selected")

    status = connection.get("last_sync_status")
    if status in ("queued", "in_progress"):
        return _response(200, {"message": "Sync already in progress", "status": status})

    update_sync_status(workspace_id, connection_id, "queued")
    queue_sync_job(workspace_id, connection_id)

    return _response(200, {"message": "Sync queued", "status": "queued"})


def handle_disconnect(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/jira/connections/{connId}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can disconnect")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    # Queue document deletion
    queue_delete_job(workspace_id, connection_id)

    # Delete tokens and connection
    delete_tokens(connection_id)
    delete_connection(workspace_id, connection_id)


    return _response(200, {"message": "Disconnected successfully"})


# ============================================================================
# Path Parsing
# ============================================================================


def _parse_path(path: str) -> tuple:
    """Parse path into tuple of segments."""
    parts = path.strip("/").split("/")
    return tuple(parts) if parts[0] else ()


# ============================================================================
# Main Handler
# ============================================================================


def lambda_handler(event: Dict, context: Any) -> Dict:
    """Main Lambda handler for Jira integration."""
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    logger.info(f"Jira API: {http_method} {path}")

    parts = _parse_path(path)

    try:
        # OAuth routes
        if parts == ("jira", "authorize") and http_method == "GET":
            return handle_authorize(event)

        # GET or POST for callback (POST for SigV4 compatibility)
        if parts == ("jira", "callback") and http_method in ("GET", "POST"):
            return handle_callback(event)

        # Connection routes: /workspaces/{id}/jira/connections
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "jira"
            and parts[3] == "connections"
            and http_method == "GET"
        ):
            event["pathParameters"] = {"id": parts[1]}
            return handle_list_connections(event)

        # Projects routes: /workspaces/{id}/jira/connections/{connId}/projects
        if (
            len(parts) == 6
            and parts[0] == "workspaces"
            and parts[2] == "jira"
            and parts[3] == "connections"
            and parts[5] == "projects"
        ):
            event["pathParameters"] = {"id": parts[1], "connectionId": parts[4]}
            if http_method == "GET":
                return handle_list_projects(event)
            if http_method == "PUT":
                try:
                    guard_active("jira_update_projects", workspace_id=parts[1])
                except RegionPassiveError:
                    return passive_response("jira_update_projects")
                return handle_update_projects(event)

        # Sync route: /workspaces/{id}/jira/connections/{connId}/sync
        if (
            len(parts) == 6
            and parts[0] == "workspaces"
            and parts[2] == "jira"
            and parts[3] == "connections"
            and parts[5] == "sync"
            and http_method == "POST"
        ):
            event["pathParameters"] = {"id": parts[1], "connectionId": parts[4]}
            try:
                guard_active("jira_trigger_sync", workspace_id=parts[1])
            except RegionPassiveError:
                return passive_response("jira_trigger_sync")
            return handle_sync(event)

        # Disconnect route: /workspaces/{id}/jira/connections/{connId}
        if (
            len(parts) == 5
            and parts[0] == "workspaces"
            and parts[2] == "jira"
            and parts[3] == "connections"
            and http_method == "DELETE"
        ):
            event["pathParameters"] = {"id": parts[1], "connectionId": parts[4]}
            try:
                guard_active("jira_disconnect", workspace_id=parts[1])
            except RegionPassiveError:
                return passive_response("jira_disconnect")
            return handle_disconnect(event)

        return _error(404, "Not found")

    except ClientError as e:
        logger.exception("DynamoDB error")
        return _error(500, "Database error")
    except Exception as e:
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")
