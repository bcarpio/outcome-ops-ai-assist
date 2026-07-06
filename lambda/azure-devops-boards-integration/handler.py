"""
Azure DevOps Boards Integration Lambda for OutcomeOps AI Assist.

Handles Entra ID OAuth flow + connection / project management for the
customer's Azure DevOps work items.

Boards mirrors the Repos integration's shape (single-tenant OAuth,
per-org discovery, workspace-scoped project selection, disconnect
cascade). It differs in two structural ways:

1. **Slug is two-part (``org/project``).** Repos operates at repo
   granularity with a three-part ``org/project/repo`` slug; Boards
   operates at project granularity because work items don't belong to
   repos. The ``AZDO_BOARDS_PROJECT#`` DDB SK uses the two-part slug.

2. **Explicit ``vso.work`` scope (not ``.default``).** Boards v1 is
   read-only work items. Using an explicit scope keeps the requested
   permission auditable in Microsoft's per-token-exchange log and
   gives an explicit v2 upgrade path when comment posting ships. See
   ``token_manager.py`` module docstring for the rationale.

API Endpoints:
    GET    /azure-devops-boards/authorize?workspace_id={id}
    GET    /azure-devops-boards/callback
    GET    /workspaces/{id}/azure-devops-boards/connections
    DELETE /workspaces/{id}/azure-devops-boards/connections/{cid}
    GET    /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs
    GET    /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs/{org}/projects
    POST   /workspaces/{id}/azure-devops-boards/projects
    GET    /workspaces/{id}/azure-devops-boards/projects
    PUT    /workspaces/{id}/azure-devops-boards/projects/{slug}
    DELETE /workspaces/{id}/azure-devops-boards/projects/{slug}
    POST   /workspaces/{id}/azure-devops-boards/sync

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
from botocore.config import Config
from botocore.exceptions import ClientError

# Explicit timeouts so a wedged network call can't hang the Lambda until
# its function timeout (python:S7618).
_BOTO_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3})


# Make shared/ importable; deploy package bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import (  # noqa: E402
    ACTION_OAUTH_CONNECTION_ESTABLISHED,
    log_admin_action,
)
from shared.license_repos import get_active_repos_for_customer  # noqa: E402
from shared.region_guard import (  # noqa: E402
    RegionPassiveError,
    guard_active,
    passive_response,
)

# ADR-019: license layer is the source of truth for repo/source limits.
# Wrap the import so the module still loads when the layer is unattached;
# the snapshot helper below treats that as a no-op.
try:
    from outcomeops_license import report_repos_snapshot  # noqa: E402
except ImportError:  # pragma: no cover - deploy bug path
    report_repos_snapshot = None  # type: ignore[assignment]

# Boards Work Items API (chunk 03). Only used for discovery here
# (list_projects); the sync worker in chunk 05 owns WIQL + work-item
# fetch. Repos-side APIs are deliberately NOT imported -- keeping
# the two integrations decoupled.
from azure_devops_work_items_api import (  # noqa: E402
    AzureDevOpsBoardsAPIError,
    AzureDevOpsBoardsAuthError,
    AzureDevOpsBoardsNotFoundError,
    list_projects as ado_list_projects,
)
# The vssps ``list_accessible_orgs`` helper is integration-agnostic
# (Repos + Boards both need to enumerate the same "orgs the user
# can see"). Shipped as an in-directory copy of the Repos client for
# deploy-zip self-containment -- each Lambda ships its own deps.
from shared.azure_devops_api import list_accessible_orgs  # noqa: E402

from shared.oauth.azure_devops_boards import (  # noqa: E402
    AZDO_BOARDS_SCOPES,
    delete_tokens,
    exchange_code_for_tokens,
    get_user_profile,
    refresh_access_token,
    save_tokens,
)


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
AZURE_DEVOPS_BOARDS_SYNC_QUEUE = os.environ.get("AZURE_DEVOPS_BOARDS_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# Per-customer tenant + client config. Reuses the Repos integration's
# tfvars for tenant/client-id/client-secret; Boards has its own redirect
# URI because the Entra ID app registration validates the redirect
# against the exact path Microsoft calls back to.
AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)
AZURE_DEVOPS_BOARDS_REDIRECT_URI = os.environ.get(
    "AZURE_DEVOPS_BOARDS_REDIRECT_URI", ""
)

_dynamodb = None
_ssm = None
_sqs = None
_client_secret: Optional[str] = None


# ---------------------------------------------------------------------------
# Boto3 + config accessors
# ---------------------------------------------------------------------------


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
    return _dynamodb


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", config=_BOTO_CONFIG)
    return _ssm


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", config=_BOTO_CONFIG)
    return _sqs


def _get_client_secret() -> str:
    """Read the ADO Entra ID app client secret from SSM.

    Cached for the container lifetime. Same SSM parameter as the Repos
    integration -- both integrations authenticate against the same app
    registration.
    """
    global _client_secret
    if _client_secret is not None:
        return _client_secret
    if not AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM:
        raise RuntimeError("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured")
    response = _get_ssm().get_parameter(
        Name=AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM,
        WithDecryption=True,
    )
    _client_secret = response["Parameter"]["Value"]
    return _client_secret


def _snapshot_active_repos() -> None:
    """Refresh the license server's active-source snapshot after any
    connect, disconnect, project-add, or project-remove. Non-blocking
    on failure -- the hourly heartbeat scheduler is the safety net.

    Note the "repos" name is historic; the license layer treats every
    integration's sources (repos, projects, spaces, channels, drives)
    as elements of one snapshot. Chunk 01 registered the
    ``AZDO_BOARDS_PROJECT#`` builder so Boards projects appear as
    ``azure_devops_boards:{org}/{project}`` in the snapshot output.
    """
    if report_repos_snapshot is None:
        return
    try:
        active = get_active_repos_for_customer(_get_dynamodb(), WORKSPACES_TABLE)
        report_repos_snapshot(active_repos=active)
    except Exception as e:
        logger.warning(f"[license-snapshot] non-blocking failure: {e}")


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------


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


def _redirect(url: str) -> Dict:
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Content-Type": "application/json",
        },
        "body": json.dumps({"redirect": url}),
    }


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_path(path: str) -> Tuple[str, ...]:
    parts = [p for p in path.strip("/").split("/") if p]
    return tuple(parts)


# ---------------------------------------------------------------------------
# Auth helpers (mirror azure-devops-integration/handler.py)
# ---------------------------------------------------------------------------


def _get_user_from_request(event: Dict) -> Optional[str]:
    headers = event.get("headers") or {}
    email = headers.get("x-user-email") or headers.get("X-User-Email")
    return email.lower() if email else None


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


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def _microsoft_authorize_url() -> str:
    """Per-customer single-tenant authorize endpoint. Same tenant as
    Repos -- both integrations share the customer's Entra ID app.
    """
    return (
        f"https://login.microsoftonline.com/{AZURE_DEVOPS_TENANT_ID}"
        "/oauth2/v2.0/authorize"
    )


def handle_authorize(event: Dict) -> Dict:
    """GET /azure-devops-boards/authorize?workspace_id=..."""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    query_params = event.get("queryStringParameters") or {}
    workspace_id = query_params.get("workspace_id")
    if not workspace_id:
        return _error(400, "workspace_id is required")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can connect Azure DevOps Boards")

    if not (
        AZURE_DEVOPS_TENANT_ID
        and AZURE_DEVOPS_CLIENT_ID
        and AZURE_DEVOPS_BOARDS_REDIRECT_URI
    ):
        return _error(500, "Azure DevOps Boards OAuth app not configured")

    state = json.dumps({"workspace_id": workspace_id, "user_email": user_email})

    params = {
        "client_id": AZURE_DEVOPS_CLIENT_ID,
        "scope": AZDO_BOARDS_SCOPES,
        "redirect_uri": AZURE_DEVOPS_BOARDS_REDIRECT_URI,
        "state": urllib.parse.quote(state),
        "response_type": "code",
        "response_mode": "query",
        "prompt": "consent",
    }
    auth_url = f"{_microsoft_authorize_url()}?{urllib.parse.urlencode(params)}"
    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict) -> Dict:
    """GET /azure-devops-boards/callback"""
    query_params = event.get("queryStringParameters") or {}
    body_params: Dict[str, Any] = {}
    if event.get("body"):
        try:
            body_params = json.loads(event.get("body", "{}"))
        except json.JSONDecodeError:
            body_params = {}

    code = query_params.get("code") or body_params.get("code")
    state = query_params.get("state") or body_params.get("state")
    error = query_params.get("error") or body_params.get("error")

    base_redirect = "/workspaces"

    if error:
        logger.error("ADO Boards OAuth error: %s", error)
        return _redirect(f"{base_redirect}?azure_devops_boards_error=oauth_denied")

    if not code or not state:
        return _redirect(f"{base_redirect}?azure_devops_boards_error=invalid_callback")

    try:
        if state.startswith("%"):
            state = urllib.parse.unquote(state)
        state_data = json.loads(state)
        workspace_id = state_data["workspace_id"]
        callback_user_email = state_data["user_email"]
    except (json.JSONDecodeError, KeyError):
        return _redirect(f"{base_redirect}?azure_devops_boards_error=invalid_state")

    try:
        client_secret = _get_client_secret()
    except Exception:
        logger.exception("Failed to read ADO client secret from SSM")
        return _redirect(f"{base_redirect}?azure_devops_boards_error=config_error")

    access_token, refresh_token, expires_in = exchange_code_for_tokens(
        code=code,
        tenant_id=AZURE_DEVOPS_TENANT_ID,
        client_id=AZURE_DEVOPS_CLIENT_ID,
        client_secret=client_secret,
        redirect_uri=AZURE_DEVOPS_BOARDS_REDIRECT_URI,
    )
    if not access_token:
        return _redirect(
            f"{base_redirect}?azure_devops_boards_error=token_exchange_failed"
        )

    profile = get_user_profile(access_token)
    if not profile:
        return _redirect(
            f"{base_redirect}?azure_devops_boards_error=profile_lookup_failed"
        )

    display_name = profile.get("displayName") or callback_user_email
    user_email_external = (
        profile.get("emailAddress") or callback_user_email
    )
    member_id = profile.get("id")

    connection_id = secrets.token_urlsafe(16)
    now = _now_iso()

    connection_row = {
        "PK": f"WS#{workspace_id}",
        "SK": f"AZDO_BOARDS_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "provider": "azure_devops_boards",
        "user_email": callback_user_email,
        "user_email_external": user_email_external,
        "display_name": display_name,
        "member_id": member_id,
        "tenant_id": AZURE_DEVOPS_TENANT_ID,
        "created_at": now,
        "updated_at": now,
    }

    try:
        dual_write.ddb_put_item(WORKSPACES_TABLE, connection_row)
        save_tokens(connection_id, access_token, refresh_token, expires_in or 3600)
    except ClientError:
        logger.exception("Failed to persist ADO Boards connection")
        return _redirect(f"{base_redirect}?azure_devops_boards_error=database_error")

    try:
        log_admin_action(
            os.environ.get("AUDIT_TABLE", ""),
            ACTION_OAUTH_CONNECTION_ESTABLISHED,
            workspace_id=workspace_id,
            user_email=callback_user_email,
            source_ip="",
            user_agent="",
            details={
                "provider": "azure_devops_boards",
                "connection_id": connection_id,
                "account_label": display_name,
                "user_email_external": user_email_external,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit OAUTH_CONNECTION_ESTABLISHED audit: {e}")

    _snapshot_active_repos()

    return _redirect(
        f"/workspaces/{workspace_id}/settings?tab=integrations"
        f"&azure_devops_boards_connected={connection_id}"
    )


# ---------------------------------------------------------------------------
# Connection / discovery routes
# ---------------------------------------------------------------------------


def _get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_BOARDS_CONNECTION#{connection_id}",
        }
    )
    return response.get("Item")


def _connection_access_token(connection_id: str) -> Optional[str]:
    """Get a fresh access token for the connection. Refreshes if needed."""
    try:
        client_secret = _get_client_secret()
    except Exception:
        logger.exception("Failed to read ADO client secret from SSM")
        return None
    return refresh_access_token(
        connection_id=connection_id,
        tenant_id=AZURE_DEVOPS_TENANT_ID,
        client_id=AZURE_DEVOPS_CLIENT_ID,
        client_secret=client_secret,
    )


def handle_list_connections(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops-boards/connections"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "AZDO_BOARDS_CONNECTION#",
        },
    )
    connections = []
    for item in response.get("Items", []):
        connections.append({
            "connection_id": item.get("connection_id"),
            "display_name": item.get("display_name"),
            "user_email_external": item.get("user_email_external"),
            "tenant_id": item.get("tenant_id"),
            "created_at": item.get("created_at"),
        })
    return _response(200, {"connections": connections})


def handle_list_orgs(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs

    Uses the shared ``list_accessible_orgs`` helper from
    ``azure-devops-sync/azure_devops_api.py`` -- the vssps profile
    endpoint is integration-agnostic and returns every org the user
    can see, regardless of which sub-scope (vso.code, vso.work) their
    token has.
    """
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    connection = _get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    member_id = connection.get("member_id")
    if not member_id:
        return _error(500, "Connection missing member_id; reconnect required")

    try:
        orgs = list_accessible_orgs(access_token, member_id=member_id)
    except Exception as e:
        logger.exception("Failed to list orgs")
        return _error(502, f"Azure DevOps API error: {e}")

    return _response(200, {
        "orgs": [
            {
                "accountName": o.get("accountName"),
                "accountId": o.get("accountId"),
            }
            for o in orgs
        ]
    })


def handle_list_org_projects(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs/{org}/projects

    Returns projects visible to the connection's token in one org.
    Uses the Boards API client's ``list_projects`` (which hits the same
    endpoint as the Repos client but is imported locally to keep the
    integration decoupled from Repos).
    """
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")
    org = path_params.get("org")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _get_connection(workspace_id, connection_id):
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        projects = ado_list_projects(access_token, org)
    except AzureDevOpsBoardsAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except AzureDevOpsBoardsNotFoundError:
        return _error(404, "Organization not found")
    except AzureDevOpsBoardsAPIError as e:
        return _error(502, f"Azure DevOps API error: {e}")

    # Already-added projects for this workspace so the UI can disable
    # the "add" button for them.
    added_slugs = {p["slug"] for p in _list_workspace_projects(workspace_id)}

    return _response(200, {
        "projects": [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "description": p.get("description"),
                "already_added": f"{org}/{p.get('name')}" in added_slugs,
            }
            for p in projects
        ]
    })


# ---------------------------------------------------------------------------
# Workspace project set
# ---------------------------------------------------------------------------


def _slug_to_parts(slug: str) -> Optional[Tuple[str, str]]:
    """Split ``org/project`` into a 2-tuple. Returns None on malformed
    input (wrong segment count)."""
    parts = slug.split("/")
    if len(parts) != 2:
        return None
    return parts[0], parts[1]


def _list_workspace_projects(workspace_id: str) -> List[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "AZDO_BOARDS_PROJECT#",
        },
    )
    out = []
    for item in response.get("Items", []):
        sk = item.get("SK", "")
        slug = sk.replace("AZDO_BOARDS_PROJECT#", "") if sk else ""
        out.append({**item, "slug": slug})
    return out


def _get_workspace_project(workspace_id: str, slug: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_BOARDS_PROJECT#{slug}",
        }
    )
    return response.get("Item")


def _queue_sync_job(workspace_id: str, slug: str, connection_id: str) -> None:
    """Enqueue a sync job. Body shape must match what the
    azure-devops-boards-sync worker (chunk 05) reads from
    message["org"|"project"] (KeyError otherwise).
    """
    if not AZURE_DEVOPS_BOARDS_SYNC_QUEUE:
        logger.warning("AZURE_DEVOPS_BOARDS_SYNC_QUEUE not configured")
        return
    parts = _slug_to_parts(slug)
    if not parts:
        logger.warning("Malformed slug %s -- not enqueuing sync", slug)
        return
    org, project = parts
    _get_sqs().send_message(
        QueueUrl=AZURE_DEVOPS_BOARDS_SYNC_QUEUE,
        MessageBody=json.dumps({
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "slug": slug,
            "org": org,
            "project": project,
            "triggered_by": "manual",
        }),
    )
    logger.info("Queued ADO Boards sync for %s/%s", workspace_id, slug)


def _queue_delete_job(workspace_id: str, slug: str) -> None:
    """Enqueue a delete-prefix job for the project's ingested work-item
    documents. Prefix mirrors chunk 05's S3 write path
    (``azure_devops_boards/{org}/{project}/``).
    """
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured")
        return
    _get_sqs().send_message(
        QueueUrl=DELETE_QUEUE,
        MessageBody=json.dumps({
            "action": "delete_prefix",
            "workspace_id": workspace_id,
            "source": "azure_devops_boards",
            "prefix": f"azure_devops_boards/{slug}/",
        }),
    )
    logger.info("Queued ADO Boards deletion for %s/%s", workspace_id, slug)


def handle_add_projects(event: Dict) -> Dict:
    """POST /workspaces/{id}/azure-devops-boards/projects"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can add Boards projects")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    connection_id = body.get("connection_id")
    projects = body.get("projects", [])
    if not connection_id:
        return _error(400, "connection_id is required")
    if not isinstance(projects, list) or not projects:
        return _error(400, "At least one project is required")

    if not _get_connection(workspace_id, connection_id):
        return _error(404, "Connection not found")

    added: List[Dict] = []
    skipped: List[Dict] = []

    for project in projects:
        if not isinstance(project, dict):
            continue
        org = project.get("org")
        project_name = project.get("project")
        if not (org and project_name):
            skipped.append({"project": project, "reason": "missing org/project"})
            continue

        slug = f"{org}/{project_name}"
        if _get_workspace_project(workspace_id, slug):
            skipped.append({"slug": slug, "reason": "already exists"})
            continue

        now = _now_iso()
        item = {
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_BOARDS_PROJECT#{slug}",
            "workspace_id": workspace_id,
            "provider": "azure_devops_boards",
            "org": org,
            "project": project_name,
            "slug": slug,
            "connection_id": connection_id,
            "enabled": True,
            "sync_status": "pending",
            "last_sync_at": None,
            "last_sync_error": None,
            "last_changed_watermark": None,
            "added_at": now,
            "added_by": user_email,
        }
        dual_write.ddb_put_item(WORKSPACES_TABLE, item)
        added.append(item)

        _queue_sync_job(workspace_id, slug, connection_id)

    if added:
        _snapshot_active_repos()

    return _response(201, {
        "added": len(added),
        "skipped": skipped,
        "projects": [
            {
                "slug": p["slug"],
                "org": p["org"],
                "project": p["project"],
                "enabled": p.get("enabled", True),
            }
            for p in added
        ],
    })


def handle_list_projects(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops-boards/projects"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    projects = _list_workspace_projects(workspace_id)
    return _response(200, {
        "projects": [
            {
                "slug": p.get("slug"),
                "org": p.get("org"),
                "project": p.get("project"),
                "connection_id": p.get("connection_id"),
                "enabled": p.get("enabled", True),
                "sync_status": p.get("sync_status", "pending"),
                "last_sync_at": p.get("last_sync_at"),
                "last_sync_error": p.get("last_sync_error"),
                "last_changed_watermark": p.get("last_changed_watermark"),
                "added_at": p.get("added_at"),
                "added_by": p.get("added_by"),
            }
            for p in projects
            if p.get("slug")
        ]
    })


def handle_update_project(event: Dict) -> Dict:
    """PUT /workspaces/{id}/azure-devops-boards/projects/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected org/project")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update projects")

    project = _get_workspace_project(workspace_id, slug)
    if not project:
        return _error(404, "Project not found")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    enabled = body.get("enabled")

    set_parts = ["updated_at = :now"]
    expr_values = {":now": _now_iso()}
    if enabled is not None:
        set_parts.append("enabled = :enabled")
        expr_values[":enabled"] = bool(enabled)

    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_BOARDS_PROJECT#{slug}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=expr_values,
    )

    updated = _get_workspace_project(workspace_id, slug)
    return _response(200, {
        "project": {
            "slug": slug,
            "enabled": updated.get("enabled", True),
        }
    })


def handle_delete_project(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/azure-devops-boards/projects/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected org/project")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can remove projects")

    project = _get_workspace_project(workspace_id, slug)
    if not project:
        return _error(404, "Project not found")

    _queue_delete_job(workspace_id, slug)
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_BOARDS_PROJECT#{slug}"},
    )
    _snapshot_active_repos()
    return _response(200, {"message": "Project removed"})


def handle_trigger_sync(event: Dict) -> Dict:
    """POST /workspaces/{id}/azure-devops-boards/sync

    Body is optional. ``{"slug": "org/project"}`` to sync one project;
    omitted to sync everything in the workspace.
    """
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    try:
        body = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        body = {}
    requested_slug = body.get("slug")

    if requested_slug:
        project = _get_workspace_project(workspace_id, requested_slug)
        if not project:
            return _error(404, "Project not found")
        if not project.get("enabled", True):
            return _error(400, "Project is disabled")
        _queue_sync_job(workspace_id, requested_slug, project["connection_id"])
        return _response(200, {"message": "Sync queued", "projects": 1})

    projects = _list_workspace_projects(workspace_id)
    queued = 0
    for project in projects:
        if not project.get("enabled", True):
            continue
        _queue_sync_job(workspace_id, project["slug"], project["connection_id"])
        queued += 1
    return _response(200, {"message": "Sync queued", "projects": queued})


def handle_disconnect(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/azure-devops-boards/connections/{cid}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can disconnect")

    connection = _get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    # Cascade-remove all projects tied to this connection.
    for project in _list_workspace_projects(workspace_id):
        if project.get("connection_id") != connection_id:
            continue
        _queue_delete_job(workspace_id, project["slug"])
        dual_write.ddb_delete_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"AZDO_BOARDS_PROJECT#{project['slug']}"},
        )

    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_BOARDS_CONNECTION#{connection_id}"},
    )
    delete_tokens(connection_id)
    _snapshot_active_repos()

    return _response(200, {"message": "Disconnected"})


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def lambda_handler(event: Dict, context: Any) -> Dict:
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )
    logger.info(f"AzureDevOpsBoards API: {http_method} {path}")
    parts = _parse_path(path)

    try:
        # OAuth (unauthenticated)
        if parts == ("azure-devops-boards", "authorize") and http_method == "GET":
            return handle_authorize(event)
        if (
            parts == ("azure-devops-boards", "callback")
            and http_method in ("GET", "POST")
        ):
            return handle_callback(event)

        # All other routes require auth + workspace membership.
        if (
            len(parts) >= 3
            and parts[0] == "workspaces"
            and parts[2] == "azure-devops-boards"
        ):
            workspace_id = parts[1]

            # /workspaces/{id}/azure-devops-boards/connections
            if len(parts) == 4 and parts[3] == "connections" and http_method == "GET":
                event["pathParameters"] = {"id": workspace_id}
                return handle_list_connections(event)

            # /workspaces/{id}/azure-devops-boards/connections/{cid}
            if (
                len(parts) == 5
                and parts[3] == "connections"
                and http_method == "DELETE"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                try:
                    guard_active("azure_devops_boards_disconnect", workspace_id=workspace_id)
                except RegionPassiveError:
                    return passive_response("azure_devops_boards_disconnect")
                return handle_disconnect(event)

            # /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs
            if (
                len(parts) == 6
                and parts[3] == "connections"
                and parts[5] == "orgs"
                and http_method == "GET"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                return handle_list_orgs(event)

            # /workspaces/{id}/azure-devops-boards/connections/{cid}/orgs/{org}/projects
            if (
                len(parts) == 8
                and parts[3] == "connections"
                and parts[5] == "orgs"
                and parts[7] == "projects"
                and http_method == "GET"
            ):
                event["pathParameters"] = {
                    "id": workspace_id,
                    "connectionId": parts[4],
                    "org": parts[6],
                }
                return handle_list_org_projects(event)

            # /workspaces/{id}/azure-devops-boards/projects
            if len(parts) == 4 and parts[3] == "projects":
                event["pathParameters"] = {"id": workspace_id}
                if http_method == "GET":
                    return handle_list_projects(event)
                if http_method == "POST":
                    try:
                        guard_active(
                            "azure_devops_boards_add_projects",
                            workspace_id=workspace_id,
                        )
                    except RegionPassiveError:
                        return passive_response("azure_devops_boards_add_projects")
                    return handle_add_projects(event)

            # /workspaces/{id}/azure-devops-boards/projects/{slug...}
            # ``slug`` is org/project (ONE slash), so slug = parts[4:] joined.
            if len(parts) >= 6 and parts[3] == "projects":
                slug = "/".join(parts[4:])
                event["pathParameters"] = {"id": workspace_id, "slug": slug}
                if http_method == "PUT":
                    try:
                        guard_active(
                            "azure_devops_boards_update_project",
                            workspace_id=workspace_id,
                        )
                    except RegionPassiveError:
                        return passive_response("azure_devops_boards_update_project")
                    return handle_update_project(event)
                if http_method == "DELETE":
                    try:
                        guard_active(
                            "azure_devops_boards_delete_project",
                            workspace_id=workspace_id,
                        )
                    except RegionPassiveError:
                        return passive_response("azure_devops_boards_delete_project")
                    return handle_delete_project(event)

            # /workspaces/{id}/azure-devops-boards/sync
            if len(parts) == 4 and parts[3] == "sync" and http_method == "POST":
                event["pathParameters"] = {"id": workspace_id}
                try:
                    guard_active(
                        "azure_devops_boards_trigger_sync",
                        workspace_id=workspace_id,
                    )
                except RegionPassiveError:
                    return passive_response("azure_devops_boards_trigger_sync")
                return handle_trigger_sync(event)

        return _error(404, "Not found")
    except ClientError:
        logger.exception("DynamoDB error")
        return _error(500, "Database error")
    except AzureDevOpsBoardsAPIError as e:
        logger.exception("Azure DevOps Boards API error")
        return _error(502, f"Azure DevOps Boards API error: {e}")
    except Exception:
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")
