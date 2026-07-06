"""
Azure DevOps Integration Lambda for OutcomeOps AI Assist.

Handles Entra ID OAuth flow + connection / repo management against
the customer's Azure DevOps tenant.

ADO differs from the other integrations in two important ways:

1. **Single-tenant OAuth.** The Entra ID app registration is in the
   *customer's* tenant. The authorize/callback flow hits
   ``login.microsoftonline.com/{tenant_id}/...`` -- never ``/common/``,
   which Entra ID rejects for single-tenant apps. ``tenant_id`` flows
   from tfvars (``var.azure_devops_tenant_id``) through this Lambda's
   env var ``AZURE_DEVOPS_TENANT_ID`` down to every token_manager call.
2. **Three-level namespace.** ADO repos live under
   ``{org}/{project}/{repo}`` (vs GitHub's ``{owner}/{repo}``). The
   DDB SK is ``AZDO_REPO#{org}/{project}/{repo}`` per chunk 01.

API Endpoints:
    GET    /azure-devops/authorize?workspace_id={id}     -- OAuth URL
    GET    /azure-devops/callback                        -- OAuth callback
    GET    /workspaces/{id}/azure-devops/connections     -- List connections
    GET    /workspaces/{id}/azure-devops/connections/{cid}/orgs
    GET    /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects
    GET    /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects/{prj}/repos
    POST   /workspaces/{id}/azure-devops/repos           -- Add repos
    GET    /workspaces/{id}/azure-devops/repos           -- List workspace repos
    PUT    /workspaces/{id}/azure-devops/repos/{slug}    -- Update settings
    DELETE /workspaces/{id}/azure-devops/repos/{slug}    -- Remove repo
    POST   /workspaces/{id}/azure-devops/sync            -- Trigger manual sync
    DELETE /workspaces/{id}/azure-devops/connections/{cid} -- Disconnect

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

# ADR-019: license layer is the source of truth for repo limits. Wrap
# the import in try/except so the module still loads when the layer is
# unattached -- the snapshot helper below treats that as a no-op (the
# heartbeat scheduler is the safety net).
try:
    from outcomeops_license import report_repos_snapshot  # noqa: E402
except ImportError:  # pragma: no cover - deploy bug path
    report_repos_snapshot = None  # type: ignore[assignment]

from azure_devops_api import (  # noqa: E402
    AzureDevOpsAPIError,
    AzureDevOpsAuthError,
    AzureDevOpsNotFoundError,
    AzureDevOpsScopeError,
    get_default_branch,
    get_latest_commit_sha,
    get_repo as ado_get_repo,
    list_accessible_orgs,
    list_branches as ado_list_branches,
    list_projects as ado_list_projects,
    list_repos as ado_list_repos,
)
from token_manager import (  # noqa: E402
    AZDO_SCOPES,
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
AZURE_DEVOPS_SYNC_QUEUE = os.environ.get("AZURE_DEVOPS_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# Per-customer tenant + client config (set at deploy time from tfvars).
AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)
AZURE_DEVOPS_REDIRECT_URI = os.environ.get("AZURE_DEVOPS_REDIRECT_URI", "")

# Cached clients
_dynamodb = None
_ssm = None
_sqs = None
_client_secret: Optional[str] = None


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


def _get_client_secret() -> str:
    """Read the ADO Entra ID app client secret from SSM.

    Cached for the container lifetime. The operator owns the SSM
    parameter value; terraform only knows the parameter *name*.
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
    """Refresh the license server's active-repo snapshot after any
    connect, disconnect, add-repos, or remove-repo. Non-blocking on
    failure -- the hourly heartbeat scheduler is the safety net.
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
    # Include redirect URL in body for the Express proxy -- Lambda
    # Function URLs do not reliably pass the Location header through.
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
# Auth helpers (mirrors github-integration / sharepoint-integration)
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
    """Per-customer single-tenant authorize endpoint.

    ADO uses the specific tenant URL, NOT ``/common/`` -- Entra ID
    rejects ``/common/`` for single-tenant app registrations. The
    tenant comes from tfvars at deploy time.
    """
    return (
        f"https://login.microsoftonline.com/{AZURE_DEVOPS_TENANT_ID}"
        "/oauth2/v2.0/authorize"
    )


def handle_authorize(event: Dict) -> Dict:
    """GET /azure-devops/authorize?workspace_id=..."""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    query_params = event.get("queryStringParameters") or {}
    workspace_id = query_params.get("workspace_id")
    if not workspace_id:
        return _error(400, "workspace_id is required")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can connect Azure DevOps")

    if not (
        AZURE_DEVOPS_TENANT_ID
        and AZURE_DEVOPS_CLIENT_ID
        and AZURE_DEVOPS_REDIRECT_URI
    ):
        return _error(500, "Azure DevOps OAuth app not configured")

    state = json.dumps({"workspace_id": workspace_id, "user_email": user_email})

    params = {
        "client_id": AZURE_DEVOPS_CLIENT_ID,
        "scope": AZDO_SCOPES,
        "redirect_uri": AZURE_DEVOPS_REDIRECT_URI,
        "state": urllib.parse.quote(state),
        "response_type": "code",
        "response_mode": "query",
        "prompt": "consent",
    }
    auth_url = f"{_microsoft_authorize_url()}?{urllib.parse.urlencode(params)}"
    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict) -> Dict:
    """GET /azure-devops/callback - Entra ID OAuth callback."""
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
        logger.error("ADO OAuth error: %s", error)
        return _redirect(f"{base_redirect}?azure_devops_error=oauth_denied")

    if not code or not state:
        return _redirect(f"{base_redirect}?azure_devops_error=invalid_callback")

    try:
        if state.startswith("%"):
            state = urllib.parse.unquote(state)
        state_data = json.loads(state)
        workspace_id = state_data["workspace_id"]
        callback_user_email = state_data["user_email"]
    except (json.JSONDecodeError, KeyError):
        return _redirect(f"{base_redirect}?azure_devops_error=invalid_state")

    try:
        client_secret = _get_client_secret()
    except Exception:
        logger.exception("Failed to read ADO client secret from SSM")
        return _redirect(f"{base_redirect}?azure_devops_error=config_error")

    access_token, refresh_token, expires_in = exchange_code_for_tokens(
        code=code,
        tenant_id=AZURE_DEVOPS_TENANT_ID,
        client_id=AZURE_DEVOPS_CLIENT_ID,
        client_secret=client_secret,
        redirect_uri=AZURE_DEVOPS_REDIRECT_URI,
    )
    if not access_token:
        return _redirect(f"{base_redirect}?azure_devops_error=token_exchange_failed")

    profile = get_user_profile(access_token)
    if not profile:
        return _redirect(f"{base_redirect}?azure_devops_error=profile_lookup_failed")

    display_name = profile.get("displayName") or callback_user_email
    user_email_external = (
        profile.get("emailAddress") or callback_user_email
    )
    member_id = profile.get("id")

    connection_id = secrets.token_urlsafe(16)
    now = _now_iso()

    connection_row = {
        "PK": f"WS#{workspace_id}",
        "SK": f"AZDO_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
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
        logger.exception("Failed to persist ADO connection")
        return _redirect(f"{base_redirect}?azure_devops_error=database_error")

    try:
        log_admin_action(
            os.environ.get("AUDIT_TABLE", ""),
            ACTION_OAUTH_CONNECTION_ESTABLISHED,
            workspace_id=workspace_id,
            user_email=callback_user_email,
            source_ip="",
            user_agent="",
            details={
                "provider": "azure_devops",
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
        f"&azure_devops_connected={connection_id}"
    )


# ---------------------------------------------------------------------------
# Connection / discovery routes
# ---------------------------------------------------------------------------


def _get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_CONNECTION#{connection_id}",
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
    """GET /workspaces/{id}/azure-devops/connections"""
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
            ":sk": "AZDO_CONNECTION#",
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
    """GET /workspaces/{id}/azure-devops/connections/{cid}/orgs"""
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
    except AzureDevOpsAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except AzureDevOpsAPIError as e:
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


def handle_list_projects(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects"""
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
    except AzureDevOpsAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except AzureDevOpsNotFoundError:
        return _error(404, "Organization not found")
    except AzureDevOpsAPIError as e:
        return _error(502, f"Azure DevOps API error: {e}")

    return _response(200, {
        "projects": [
            {"id": p.get("id"), "name": p.get("name")}
            for p in projects
        ]
    })


def handle_list_project_repos(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects/{prj}/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")
    org = path_params.get("org")
    project = path_params.get("project")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _get_connection(workspace_id, connection_id):
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        repos = ado_list_repos(access_token, org, project)
    except AzureDevOpsAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except AzureDevOpsNotFoundError:
        return _error(404, "Project not found")
    except AzureDevOpsAPIError as e:
        return _error(502, f"Azure DevOps API error: {e}")

    # Already-added repos for this workspace
    added_slugs = {r["slug"] for r in _list_workspace_repos(workspace_id)}

    return _response(200, {
        "repos": [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "default_branch": (
                    r.get("defaultBranch", "").replace("refs/heads/", "")
                    if r.get("defaultBranch") else None
                ),
                "already_added": f"{org}/{project}/{r.get('name')}" in added_slugs,
            }
            for r in repos
        ]
    })


# ---------------------------------------------------------------------------
# Workspace repo set
# ---------------------------------------------------------------------------


def _slug_to_parts(slug: str) -> Optional[Tuple[str, str, str]]:
    """Split ``org/project/repo`` into a 3-tuple. Returns None if the
    slug is malformed (wrong segment count)."""
    parts = slug.split("/")
    if len(parts) != 3:
        return None
    return parts[0], parts[1], parts[2]


def _list_workspace_repos(workspace_id: str) -> List[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "AZDO_REPO#",
        },
    )
    out = []
    for item in response.get("Items", []):
        sk = item.get("SK", "")
        slug = sk.replace("AZDO_REPO#", "") if sk else ""
        out.append({**item, "slug": slug})
    return out


def _get_workspace_repo(workspace_id: str, slug: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_REPO#{slug}",
        }
    )
    return response.get("Item")


def _queue_sync_job(workspace_id: str, slug: str, connection_id: str) -> None:
    """Enqueue a sync job. The message shape must match what the
    azure-devops-sync worker reads from message["org"|"project"|"repo"]
    (KeyError otherwise). Mirror the chunk-06 scheduler's body shape."""
    if not AZURE_DEVOPS_SYNC_QUEUE:
        logger.warning("AZURE_DEVOPS_SYNC_QUEUE not configured")
        return
    parts = _slug_to_parts(slug)
    if not parts:
        logger.warning("Malformed slug %s -- not enqueuing sync", slug)
        return
    org, project, repo = parts
    _get_sqs().send_message(
        QueueUrl=AZURE_DEVOPS_SYNC_QUEUE,
        MessageBody=json.dumps({
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "slug": slug,
            "org": org,
            "project": project,
            "repo": repo,
            "triggered_by": "manual",
        }),
    )
    logger.info("Queued ADO sync for %s/%s", workspace_id, slug)


def _queue_delete_job(workspace_id: str, slug: str) -> None:
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured")
        return
    _get_sqs().send_message(
        QueueUrl=DELETE_QUEUE,
        MessageBody=json.dumps({
            "action": "delete_prefix",
            "workspace_id": workspace_id,
            "source": "azure_devops",
            "prefix": f"azure_devops/{slug}/",
        }),
    )
    logger.info("Queued ADO deletion for %s/%s", workspace_id, slug)


def handle_add_repos(event: Dict) -> Dict:
    """POST /workspaces/{id}/azure-devops/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can add repos")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    connection_id = body.get("connection_id")
    repos = body.get("repos", [])
    if not connection_id:
        return _error(400, "connection_id is required")
    if not isinstance(repos, list) or not repos:
        return _error(400, "At least one repo is required")

    connection = _get_connection(workspace_id, connection_id)
    if not connection:
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    added: List[Dict] = []
    skipped: List[Dict] = []

    for repo in repos:
        if not isinstance(repo, dict):
            continue
        org = repo.get("org")
        project = repo.get("project")
        repo_name = repo.get("repo")
        repo_type = repo.get("repo_type", "application")
        include_code_maps = repo.get("include_code_maps", True)
        # UI may pass an explicit branch override (customer picked
        # `develop` in the auto-detect + confirmable widget). If not
        # provided, resolve via ADO API. Whichever wins gets persisted
        # to the DDB row; downstream syncs read from there.
        branch_override: Optional[str] = repo.get("default_branch")
        if not (org and project and repo_name):
            skipped.append({"repo": repo, "reason": "missing org/project/repo"})
            continue

        slug = f"{org}/{project}/{repo_name}"
        if _get_workspace_repo(workspace_id, slug):
            skipped.append({"slug": slug, "reason": "already exists"})
            continue

        try:
            default_branch = branch_override or get_default_branch(
                access_token, org, project, repo_name,
            )
            latest_sha = (
                get_latest_commit_sha(access_token, org, project, repo_name, default_branch)
                if default_branch else None
            )
        except AzureDevOpsAuthError:
            return _error(401, "Authentication failed. Please reconnect.")
        except AzureDevOpsNotFoundError:
            skipped.append({"slug": slug, "reason": "not found in ADO"})
            continue
        except AzureDevOpsAPIError as e:
            skipped.append({"slug": slug, "reason": f"ADO error: {e}"})
            continue

        now = _now_iso()
        item = {
            "PK": f"WS#{workspace_id}",
            "SK": f"AZDO_REPO#{slug}",
            "workspace_id": workspace_id,
            "provider": "azure_devops",
            "org": org,
            "project": project,
            "repo": repo_name,
            "slug": slug,
            "connection_id": connection_id,
            "default_branch": default_branch,
            "last_commit_sha": latest_sha,
            "enabled": True,
            "include_code_maps": include_code_maps,
            "repo_type": repo_type,
            "last_sync_at": None,
            "last_sync_status": "pending",
            "last_sync_error": None,
            "added_at": now,
            "added_by": user_email,
        }
        dual_write.ddb_put_item(WORKSPACES_TABLE, item)
        added.append(item)

        if include_code_maps:
            _queue_sync_job(workspace_id, slug, connection_id)

    if added:
        _snapshot_active_repos()

    return _response(201, {
        "added": len(added),
        "skipped": skipped,
        "repos": [
            {
                "slug": r["slug"],
                "default_branch": r.get("default_branch"),
                "repo_type": r.get("repo_type"),
                "include_code_maps": r.get("include_code_maps"),
            }
            for r in added
        ],
    })


def handle_list_repos(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    workspace_id = event.get("pathParameters", {}).get("id")
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    repos = _list_workspace_repos(workspace_id)
    return _response(200, {
        "repos": [
            {
                "slug": r.get("slug"),
                "org": r.get("org"),
                "project": r.get("project"),
                "repo": r.get("repo"),
                "connection_id": r.get("connection_id"),
                "default_branch": r.get("default_branch"),
                "enabled": r.get("enabled", True),
                "include_code_maps": r.get("include_code_maps", True),
                "repo_type": r.get("repo_type", "application"),
                "last_sync_at": r.get("last_sync_at"),
                "last_sync_status": r.get("last_sync_status", "pending"),
                "last_sync_error": r.get("last_sync_error"),
                "added_at": r.get("added_at"),
                "added_by": r.get("added_by"),
            }
            for r in repos
            if r.get("slug")
        ]
    })


def handle_update_repo(event: Dict) -> Dict:
    """PUT /workspaces/{id}/azure-devops/repos/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected org/project/repo")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update repos")

    repo = _get_workspace_repo(workspace_id, slug)
    if not repo:
        return _error(404, "Repo not found")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    include_code_maps = body.get("include_code_maps")
    enabled = body.get("enabled")
    default_branch = body.get("default_branch")
    if default_branch is not None:
        default_branch = str(default_branch).strip() or None

    set_parts = ["updated_at = :now"]
    expr_values = {":now": _now_iso()}
    if include_code_maps is not None:
        set_parts.append("include_code_maps = :icm")
        expr_values[":icm"] = bool(include_code_maps)
    if enabled is not None:
        set_parts.append("enabled = :enabled")
        expr_values[":enabled"] = bool(enabled)
    if default_branch is not None:
        set_parts.append("default_branch = :db")
        expr_values[":db"] = default_branch

    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_REPO#{slug}"},
        UpdateExpression="SET " + ", ".join(set_parts),
        ExpressionAttributeValues=expr_values,
    )

    updated = _get_workspace_repo(workspace_id, slug)
    return _response(200, {
        "repo": {
            "slug": slug,
            "enabled": updated.get("enabled", True),
            "include_code_maps": updated.get("include_code_maps", True),
            "repo_type": updated.get("repo_type", "application"),
        }
    })


def handle_list_repo_branches(event: Dict) -> Dict:
    """GET /workspaces/{id}/azure-devops/repos/{slug}/branches

    Returns available branches for a repo + the provider's *current*
    default branch. Powers the edit-branch modal's dropdown +
    drift-detect badge. Mirror of the GitHub-side endpoint.
    """
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    parts = _slug_to_parts(slug)
    if not parts:
        return _error(400, "Invalid slug; expected org/project/repo")
    org, project, repo_name = parts

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can list branches")

    repo_row = _get_workspace_repo(workspace_id, slug)
    if not repo_row:
        return _error(404, "Repo not found")

    connection_id = repo_row.get("connection_id", "")
    access_token = _connection_access_token(connection_id)
    if not access_token:
        return _error(401, "ADO connection auth failed; please reconnect")

    try:
        branches = ado_list_branches(access_token, org, project, repo_name)
        current_default = get_default_branch(access_token, org, project, repo_name)
    except AzureDevOpsAuthError:
        return _error(401, "Authentication failed. Please reconnect.")
    except AzureDevOpsNotFoundError:
        return _error(404, "Repo not found in ADO")
    except AzureDevOpsAPIError as e:
        return _error(502, f"Azure DevOps API error: {e}")

    return _response(200, {
        "branches": branches,
        "current_default": current_default,
        "stored_default": repo_row.get("default_branch"),
    })


def handle_delete_repo(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/azure-devops/repos/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected org/project/repo")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can remove repos")

    repo = _get_workspace_repo(workspace_id, slug)
    if not repo:
        return _error(404, "Repo not found")

    _queue_delete_job(workspace_id, slug)
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_REPO#{slug}"},
    )
    _snapshot_active_repos()
    return _response(200, {"message": "Repo removed"})


def handle_trigger_sync(event: Dict) -> Dict:
    """POST /workspaces/{id}/azure-devops/sync

    Body is optional. ``{"slug": "org/project/repo"}`` to sync one
    repo; omitted to sync everything in the workspace.
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
        repo = _get_workspace_repo(workspace_id, requested_slug)
        if not repo:
            return _error(404, "Repo not found")
        if not repo.get("enabled", True):
            return _error(400, "Repo is disabled")
        _queue_sync_job(workspace_id, requested_slug, repo["connection_id"])
        return _response(200, {"message": "Sync queued", "repos": 1})

    repos = _list_workspace_repos(workspace_id)
    queued = 0
    for repo in repos:
        if not repo.get("enabled", True):
            continue
        if not repo.get("include_code_maps", True):
            continue
        _queue_sync_job(workspace_id, repo["slug"], repo["connection_id"])
        queued += 1
    return _response(200, {"message": "Sync queued", "repos": queued})


def handle_disconnect(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/azure-devops/connections/{cid}"""
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

    # Cascade-remove all repos tied to this connection.
    for repo in _list_workspace_repos(workspace_id):
        if repo.get("connection_id") != connection_id:
            continue
        _queue_delete_job(workspace_id, repo["slug"])
        dual_write.ddb_delete_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"AZDO_REPO#{repo['slug']}"},
        )

    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"AZDO_CONNECTION#{connection_id}"},
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
    logger.info(f"AzureDevOps API: {http_method} {path}")
    parts = _parse_path(path)

    try:
        # OAuth (unauthenticated)
        if parts == ("azure-devops", "authorize") and http_method == "GET":
            return handle_authorize(event)
        if parts == ("azure-devops", "callback") and http_method in ("GET", "POST"):
            return handle_callback(event)

        # All other routes require auth + workspace membership.
        if len(parts) >= 3 and parts[0] == "workspaces" and parts[2] == "azure-devops":
            workspace_id = parts[1]

            # /workspaces/{id}/azure-devops/connections
            if len(parts) == 4 and parts[3] == "connections" and http_method == "GET":
                event["pathParameters"] = {"id": workspace_id}
                return handle_list_connections(event)

            # /workspaces/{id}/azure-devops/connections/{cid}
            if (
                len(parts) == 5
                and parts[3] == "connections"
                and http_method == "DELETE"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                try:
                    guard_active("azure_devops_disconnect", workspace_id=workspace_id)
                except RegionPassiveError:
                    return passive_response("azure_devops_disconnect")
                return handle_disconnect(event)

            # /workspaces/{id}/azure-devops/connections/{cid}/orgs
            if (
                len(parts) == 6
                and parts[3] == "connections"
                and parts[5] == "orgs"
                and http_method == "GET"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                return handle_list_orgs(event)

            # /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects
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
                return handle_list_projects(event)

            # /workspaces/{id}/azure-devops/connections/{cid}/orgs/{org}/projects/{prj}/repos
            if (
                len(parts) == 10
                and parts[3] == "connections"
                and parts[5] == "orgs"
                and parts[7] == "projects"
                and parts[9] == "repos"
                and http_method == "GET"
            ):
                event["pathParameters"] = {
                    "id": workspace_id,
                    "connectionId": parts[4],
                    "org": parts[6],
                    "project": parts[8],
                }
                return handle_list_project_repos(event)

            # /workspaces/{id}/azure-devops/repos
            if len(parts) == 4 and parts[3] == "repos":
                event["pathParameters"] = {"id": workspace_id}
                if http_method == "GET":
                    return handle_list_repos(event)
                if http_method == "POST":
                    try:
                        guard_active("azure_devops_add_repos", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("azure_devops_add_repos")
                    return handle_add_repos(event)

            # Sub-resource: /workspaces/{id}/azure-devops/repos/{slug...}/branches
            # slug is org/project/repo (2 slashes), branches appended;
            # parts[-1] == "branches", slug spans parts[4:-1]. Must
            # match before the greedy /{slug} route below.
            if (
                len(parts) >= 8
                and parts[3] == "repos"
                and parts[-1] == "branches"
                and http_method == "GET"
            ):
                slug = "/".join(parts[4:-1])
                event["pathParameters"] = {"id": workspace_id, "slug": slug}
                return handle_list_repo_branches(event)

            # /workspaces/{id}/azure-devops/repos/{slug...}
            # ``slug`` is org/project/repo and contains TWO slashes,
            # so the slug spans parts[4:].
            if len(parts) >= 7 and parts[3] == "repos":
                slug = "/".join(parts[4:])
                event["pathParameters"] = {"id": workspace_id, "slug": slug}
                if http_method == "PUT":
                    try:
                        guard_active("azure_devops_update_repo", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("azure_devops_update_repo")
                    return handle_update_repo(event)
                if http_method == "DELETE":
                    try:
                        guard_active("azure_devops_delete_repo", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("azure_devops_delete_repo")
                    return handle_delete_repo(event)

            # /workspaces/{id}/azure-devops/sync
            if len(parts) == 4 and parts[3] == "sync" and http_method == "POST":
                event["pathParameters"] = {"id": workspace_id}
                try:
                    guard_active("azure_devops_trigger_sync", workspace_id=workspace_id)
                except RegionPassiveError:
                    return passive_response("azure_devops_trigger_sync")
                return handle_trigger_sync(event)

        return _error(404, "Not found")
    except ClientError:
        logger.exception("DynamoDB error")
        return _error(500, "Database error")
    except AzureDevOpsAPIError as e:
        logger.exception("Azure DevOps API error")
        return _error(502, f"Azure DevOps API error: {e}")
    except Exception:
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")
