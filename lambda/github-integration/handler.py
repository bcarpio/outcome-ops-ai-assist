"""
GitHub App Integration Lambda for OutcomeOps AI Assist.

Handles GitHub App OAuth flow and connection management.
Supports per-workspace repo selection with code-map generation.

API Endpoints:
    GET  /github/authorize?workspace_id={id}                    - Get GitHub App install URL
    GET  /github/callback                                       - Installation callback
    GET  /workspaces/{id}/github/installations                  - List installations
    GET  /workspaces/{id}/github/installations/{instId}/repos   - List repos for installation
    POST /workspaces/{id}/github/repos                          - Add repos to workspace
    GET  /workspaces/{id}/github/repos                          - List workspace repos
    PUT  /workspaces/{id}/github/repos/{name}                   - Update repo settings
    DELETE /workspaces/{id}/github/repos/{name}                 - Remove repo
    POST /workspaces/{id}/github/sync                           - Trigger manual sync

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

# Make shared/ importable; deploy package now bundles lambda/shared/
# at zip root via terraform source_path (see lambda-github.tf).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.delete_dispatch import dispatch_repo_delete, region_from_sqs_url  # noqa: E402
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

from github_app import (
    GitHubAppError,
    get_app_installations,
    get_github_config,
    get_installation_repos,
)

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
GITHUB_SYNC_QUEUE = os.environ.get("GITHUB_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
# Secondary-region delete queue (HA). Repo-delete messages go to both so
# each region's delete-worker cleans its own stores (delete-bug 02).
# Empty in single-region mode.
DELETE_QUEUE_SECONDARY = os.environ.get("DELETE_QUEUE_SECONDARY")

# Cached clients
_dynamodb = None
_sqs = None
_sqs_secondary = None


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


def _get_sqs_secondary():
    """SQS client targeting the secondary-region delete queue, or None
    when not configured. Region is derived from the queue URL."""
    global _sqs_secondary
    if not DELETE_QUEUE_SECONDARY:
        return None
    if _sqs_secondary is None:
        region = region_from_sqs_url(DELETE_QUEUE_SECONDARY)
        _sqs_secondary = boto3.client("sqs", region_name=region) if region else None
    return _sqs_secondary


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
    # Include redirect URL in body for Express proxy (Lambda Function URLs
    # may not pass Location header through properly)
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            "Content-Type": "application/json",
        },
        "body": json.dumps({"redirect": url}),
    }


# ============================================================================
# Auth Helpers
# ============================================================================


def _get_user_from_request(event: Dict) -> Optional[str]:
    """Extract user email from request headers (set by Fargate proxy)."""
    headers = event.get("headers", {})
    email = headers.get("x-user-email") or headers.get("X-User-Email")
    return email.lower() if email else None


def _is_workspace_admin(table, user_email: str, workspace_id: str) -> bool:
    """Check if user is a workspace admin (or org admin)."""
    # Check workspace membership
    response = table.get_item(
        Key={
            "PK": f"USER#{user_email}",
            "SK": f"WS_MEMBER#{workspace_id}",
        }
    )
    item = response.get("Item")
    if item and item.get("role") == "workspace_admin":
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


def _now_iso() -> str:
    """Get current timestamp in ISO format."""
    return datetime.now(timezone.utc).isoformat()


def save_installation(
    workspace_id: str,
    installation_id: str,
    account_login: str,
    account_type: str,
    permissions: Dict,
    created_by: str,
) -> None:
    """Save a GitHub App installation to DynamoDB."""
    now = _now_iso()

    dual_write.ddb_put_item(
        WORKSPACES_TABLE,
        {
            "PK": f"WS#{workspace_id}",
            "SK": f"GITHUB_INSTALLATION#{installation_id}",
            "installation_id": installation_id,
            "workspace_id": workspace_id,
            "account_login": account_login,
            "account_type": account_type,
            "permissions": permissions,
            "created_by": created_by,
            "created_at": now,
        },
    )
    logger.info("Saved GitHub installation %s for workspace %s", installation_id, workspace_id)


def get_installation(workspace_id: str, installation_id: str) -> Optional[Dict]:
    """Get an installation by ID."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"GITHUB_INSTALLATION#{installation_id}"}
    )
    return response.get("Item")


def list_installations(workspace_id: str) -> List[Dict]:
    """List all GitHub installations for a workspace."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "GITHUB_INSTALLATION#",
        },
    )
    return response.get("Items", [])


def delete_installation(workspace_id: str, installation_id: str) -> None:
    """Delete an installation."""
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_INSTALLATION#{installation_id}"},
    )
    logger.info(f"Deleted GitHub installation {installation_id}")


def save_repo(
    workspace_id: str,
    repo_name: str,
    installation_id: str,
    include_code_maps: bool,
    added_by: str,
    repo_type: str = "application",
) -> Dict:
    """Save a GitHub repo to workspace."""
    now = _now_iso()

    item = {
        "PK": f"WS#{workspace_id}",
        "SK": f"GITHUB_REPO#{repo_name}",
        "workspace_id": workspace_id,
        "repo_name": repo_name,
        "installation_id": installation_id,
        "enabled": True,
        "include_code_maps": include_code_maps,
        "repo_type": repo_type,  # application or standards
        "last_sync_at": None,
        "last_sync_status": "pending",
        "last_sync_error": None,
        "last_commit_sha": None,
        "added_at": now,
        "added_by": added_by,
    }

    dual_write.ddb_put_item(WORKSPACES_TABLE, item)
    logger.info(f"Saved GitHub repo {repo_name} (type={repo_type}) for workspace {workspace_id}")
    return item


def get_repo(workspace_id: str, repo_name: str) -> Optional[Dict]:
    """Get a repo by name."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"}
    )
    return response.get("Item")


def list_repos(workspace_id: str) -> List[Dict]:
    """List all GitHub repos for a workspace."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "GITHUB_REPO#",
        },
    )
    return response.get("Items", [])


def update_repo(
    workspace_id: str,
    repo_name: str,
    include_code_maps: Optional[bool] = None,
    enabled: Optional[bool] = None,
) -> None:
    """Update repo settings."""
    update_parts = ["updated_at = :now"]
    expr_values = {":now": _now_iso()}

    if include_code_maps is not None:
        update_parts.append("include_code_maps = :icm")
        expr_values[":icm"] = include_code_maps
    if enabled is not None:
        update_parts.append("enabled = :enabled")
        expr_values[":enabled"] = enabled

    dual_write.ddb_update_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
        UpdateExpression="SET " + ", ".join(update_parts),
        ExpressionAttributeValues=expr_values,
    )


def update_repo_sync_status(
    workspace_id: str,
    repo_name: str,
    status: str,
    error: Optional[str] = None,
    commit_sha: Optional[str] = None,
) -> None:
    """Update sync status for a repo."""
    set_parts = ["last_sync_status = :status", "updated_at = :now"]
    remove_parts = []
    expr_values = {
        ":status": status,
        ":now": _now_iso(),
    }

    if status in ("success", "failed"):
        set_parts.append("last_sync_at = :sync_at")
        expr_values[":sync_at"] = _now_iso()

    if error is not None:
        set_parts.append("last_sync_error = :error")
        expr_values[":error"] = error
    elif status == "success":
        remove_parts.append("last_sync_error")

    if commit_sha is not None:
        set_parts.append("last_commit_sha = :sha")
        expr_values[":sha"] = commit_sha

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(repo_name)",
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning("Skipping sync status update for deleted repo %s in workspace %s", repo_name, workspace_id)
        else:
            raise


def delete_repo(workspace_id: str, repo_name: str) -> None:
    """Delete a repo from workspace."""
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"GITHUB_REPO#{repo_name}"},
    )
    logger.info(f"Deleted GitHub repo {repo_name} from workspace {workspace_id}")


# ============================================================================
# SQS Operations
# ============================================================================


def queue_sync_job(workspace_id: str, repo_name: str, installation_id: str) -> None:
    """Queue a sync job to SQS."""
    if not GITHUB_SYNC_QUEUE:
        logger.warning("GITHUB_SYNC_QUEUE not configured")
        return

    sqs = _get_sqs()
    sqs.send_message(
        QueueUrl=GITHUB_SYNC_QUEUE,
        MessageBody=json.dumps({
            "workspace_id": workspace_id,
            "repo_name": repo_name,
            "installation_id": installation_id,
            "triggered_by": "manual",
        }),
    )
    logger.info("Queued GitHub sync for %s/%s", workspace_id, repo_name)


def queue_delete_job(workspace_id: str, repo_name: str) -> None:
    """Queue full cleanup for a removed repo.

    ``repo_name`` is owner-prefixed ("owner/repo"). A repo's data spans
    three shapes that a single owner-prefix delete misses:
    - KB docs/vectors under the owner prefix (github/{owner}/{repo}/)
    - legacy bare-name KB objects (github/{repo}/, e.g. the
      architectural-summary.txt) — NOT under the owner prefix
    - symbol-graph facts + the code-maps pointer (DynamoDB, keyed by the
      BARE repo name) — delete_prefix only touches S3 + s3vectors
    Queue all three so removal is complete (delete-bug 01/03).
    """
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured")
        return

    # Dispatch the full cleanup (owner + bare KB prefixes + symbol graph)
    # to the primary queue and, under HA, the secondary queue — so each
    # region's delete-worker cleans its own stores (delete-bug 01/02/03).
    targets = [(_get_sqs(), DELETE_QUEUE)]
    secondary = _get_sqs_secondary()
    if secondary is not None:
        targets.append((secondary, DELETE_QUEUE_SECONDARY))

    sent = dispatch_repo_delete(workspace_id, repo_name, targets)
    logger.info(
        f"Queued full cleanup for {workspace_id}/{repo_name}: {sent} "
        f"message(s) across {len(targets)} region(s)"
    )


# ============================================================================
# Route Handlers
# ============================================================================


def handle_authorize(event: Dict) -> Dict:
    """
    GET /github/authorize?workspace_id=...

    Returns the GitHub App installation URL.
    The user will be redirected to GitHub to install the App.
    """
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
        return _error(403, "Only workspace admins can connect GitHub")

    # Get GitHub config to get app name
    config = get_github_config()
    app_id = config.get("app_id", "")

    if not app_id or app_id == "PLACEHOLDER_CONFIGURE_ME":
        return _error(500, "GitHub App not configured")

    app_slug = config.get("app_slug", "")
    if not app_slug or app_slug == "PLACEHOLDER_CONFIGURE_ME":
        return _error(500, "GitHub App slug not configured")

    # Build install URL with state for callback
    state = json.dumps({
        "workspace_id": workspace_id,
        "user_email": user_email,
    })

    # Pin GitHub's post-install redirect to THIS environment's callback
    # URL. When a single GitHub App is shared across multiple
    # workspaces / environments (prd + openai, etc.), the App config
    # carries multiple callback URLs. Without an explicit redirect_uri
    # parameter, GitHub uses the FIRST callback URL registered on the
    # App -- so every install from openai was falling back to prd's
    # outcomeops.outcomeops.ai. Surfaced 2026-06-04 on the openai
    # workspace's first GitHub connect attempt.
    callback_url = config.get("callback_url", "")
    install_url = (
        f"https://github.com/apps/{app_slug}/installations/new"
        f"?state={urllib.parse.quote(state, safe='')}"
    )
    if callback_url and callback_url != "PLACEHOLDER_CONFIGURE_ME":
        # GitHub validates redirect_uri by EXACT match against the
        # registered Callback URLs on the App. urllib.parse.quote's
        # default `safe='/'` leaves slashes raw, producing
        # `https%3A//host/...` -- which GitHub's validator does not
        # match against the registered `https://host/...`, silently
        # falling back to the first registered URL. Fully URL-encode
        # via `safe=''` so the colon AND slashes are %-escaped (the
        # standard convention for redirect_uri values in OAuth flows).
        # state is also fully encoded above for the same reason
        # (defensive consistency; state is opaque to GitHub).
        install_url += f"&redirect_uri={urllib.parse.quote(callback_url, safe='')}"

    return _response(200, {"install_url": install_url})


def handle_callback(event: Dict) -> Dict:
    """
    GET/POST /github/callback - GitHub App installation callback.

    Called after user installs the GitHub App on their org/repos.
    GitHub sends: installation_id, setup_action, state

    Params can come from query string (direct GitHub redirect) or
    body (proxied through Express).
    """
    query_params = event.get("queryStringParameters") or {}

    # Also check body for POST requests (Express proxy sends JSON body)
    body_params = {}
    if event.get("body"):
        try:
            body_params = json.loads(event.get("body", "{}"))
        except json.JSONDecodeError:
            pass

    # Prefer query params, fall back to body
    installation_id = query_params.get("installation_id") or body_params.get("installation_id")
    setup_action = query_params.get("setup_action") or body_params.get("setup_action", "install")
    state = query_params.get("state") or body_params.get("state")

    # Derive redirect base URL from callback_url SSM parameter
    config = get_github_config()
    callback_url = config.get("callback_url", "")
    if callback_url and callback_url != "PLACEHOLDER_CONFIGURE_ME":
        from urllib.parse import urlparse
        parsed = urlparse(callback_url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
    else:
        return _error(500, "GitHub callback URL not configured")

    if not installation_id:
        return _redirect(f"{base_url}/workspaces?github_error=missing_installation_id")

    if not state:
        return _redirect(f"{base_url}/workspaces?github_error=missing_state")

    try:
        state_data = json.loads(state)
        workspace_id = state_data["workspace_id"]
        user_email = state_data["user_email"]
    except (json.JSONDecodeError, KeyError):
        return _redirect(f"{base_url}/workspaces?github_error=invalid_state")

    try:
        # Get installation details from GitHub API
        installations = get_app_installations()
        installation = next(
            (i for i in installations if str(i["id"]) == str(installation_id)),
            None
        )

        if not installation:
            return _redirect(f"{base_url}/workspaces?github_error=installation_not_found")

        # Save installation to DynamoDB
        account = installation.get("account", {})
        save_installation(
            workspace_id=workspace_id,
            installation_id=str(installation_id),
            account_login=account.get("login", ""),
            account_type=account.get("type", "Organization"),
            permissions=installation.get("permissions", {}),
            created_by=user_email,
        )

        # Redirect to workspace settings
        return _redirect(
            f"{base_url}/workspaces/{workspace_id}/settings?tab=integrations&github_connected=true"
        )

    except GitHubAppError as e:
        logger.exception("GitHub callback error")
        return _redirect(f"{base_url}/workspaces?github_error={urllib.parse.quote(str(e))}")
    except Exception as e:
        logger.exception("GitHub callback error")
        return _redirect(f"{base_url}/workspaces?github_error=callback_failed")


def handle_list_installations(event: Dict) -> Dict:
    """GET /workspaces/{id}/github/installations"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    installations = list_installations(workspace_id)

    return _response(200, {
        "installations": [
            {
                "installation_id": i["installation_id"],
                "account_login": i.get("account_login", ""),
                "account_type": i.get("account_type", "Organization"),
                "created_at": i.get("created_at"),
            }
            for i in installations
        ]
    })


def handle_list_installation_repos(event: Dict) -> Dict:
    """GET /workspaces/{id}/github/installations/{instId}/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    installation_id = path_params.get("installationId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    # Verify installation belongs to workspace
    installation = get_installation(workspace_id, installation_id)
    if not installation:
        return _error(404, "Installation not found")

    try:
        repos = get_installation_repos(installation_id)

        # Get already-added repos for this workspace
        workspace_repos = list_repos(workspace_id)
        added_names = {r["repo_name"] for r in workspace_repos if "repo_name" in r}

        return _response(200, {
            "repos": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "full_name": r["full_name"],
                    "description": r.get("description", ""),
                    "private": r["private"],
                    "default_branch": r.get("default_branch", "main"),
                    "already_added": r["full_name"] in added_names,
                }
                for r in repos
            ]
        })

    except GitHubAppError as e:
        return _error(502, f"GitHub API error: {e}")


def handle_add_repos(event: Dict) -> Dict:
    """POST /workspaces/{id}/github/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can add repos")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    repos = body.get("repos", [])
    if not repos:
        return _error(400, "At least one repo is required")

    added = []
    for repo in repos:
        repo_name = repo.get("repo_name") or repo.get("full_name")
        installation_id = repo.get("installation_id")
        include_code_maps = repo.get("include_code_maps", True)
        repo_type = repo.get("repo_type", "application")  # application, standards, infrastructure

        if not repo_name or not installation_id:
            continue

        # Verify installation belongs to workspace
        installation = get_installation(workspace_id, installation_id)
        if not installation:
            continue

        # Check if already exists
        existing = get_repo(workspace_id, repo_name)
        if existing:
            continue

        item = save_repo(
            workspace_id=workspace_id,
            repo_name=repo_name,
            installation_id=installation_id,
            include_code_maps=include_code_maps,
            added_by=user_email,
            repo_type=repo_type,
        )
        added.append(item)

        # Queue sync if code maps enabled
        if include_code_maps:
            update_repo_sync_status(workspace_id, repo_name, "queued")
            queue_sync_job(workspace_id, repo_name, installation_id)

    # Refresh the license server's active-repo snapshot if any repo was
    # actually added (license layer v2.0.0 desired-state model).
    if added:
        _snapshot_active_repos()

    return _response(201, {
        "added": len(added),
        "repos": [
            {
                "repo_name": r["repo_name"],
                "installation_id": r["installation_id"],
                "include_code_maps": r["include_code_maps"],
                "repo_type": r.get("repo_type", "application"),
            }
            for r in added
        ]
    })


def handle_list_repos(event: Dict) -> Dict:
    """GET /workspaces/{id}/github/repos"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    repos = list_repos(workspace_id)

    return _response(200, {
        "repos": [
            {
                "repo_name": r.get("repo_name", ""),
                "installation_id": r.get("installation_id", ""),
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
            if r.get("repo_name")
        ]
    })


def handle_update_repo(event: Dict) -> Dict:
    """PUT /workspaces/{id}/github/repos/{name}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    repo_name = path_params.get("repoName")

    if not repo_name:
        return _error(400, "Repo name is required")

    # URL decode repo name (may contain /)
    repo_name = urllib.parse.unquote(repo_name)

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can update repos")

    repo = get_repo(workspace_id, repo_name)
    if not repo:
        return _error(404, "Repo not found")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    include_code_maps = body.get("include_code_maps")
    enabled = body.get("enabled")

    update_repo(
        workspace_id=workspace_id,
        repo_name=repo_name,
        include_code_maps=include_code_maps,
        enabled=enabled,
    )

    updated = get_repo(workspace_id, repo_name)
    return _response(200, {
        "repo": {
            "repo_name": updated["repo_name"],
            "installation_id": updated.get("installation_id", ""),
            "enabled": updated.get("enabled", True),
            "include_code_maps": updated.get("include_code_maps", True),
            "repo_type": updated.get("repo_type", "application"),
            "last_sync_at": updated.get("last_sync_at"),
            "last_sync_status": updated.get("last_sync_status", "pending"),
            "last_sync_error": updated.get("last_sync_error"),
            "added_at": updated.get("added_at"),
            "added_by": updated.get("added_by"),
        }
    })


def handle_delete_repo(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/github/repos/{name}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    repo_name = path_params.get("repoName")

    if not repo_name:
        return _error(400, "Repo name is required")

    # URL decode repo name (may contain /)
    repo_name = urllib.parse.unquote(repo_name)

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can remove repos")

    repo = get_repo(workspace_id, repo_name)
    if not repo:
        return _error(404, "Repo not found")

    # Queue deletion of documents using full repo name (owner/repo)
    # which matches how github-sync stores files in S3
    queue_delete_job(workspace_id, repo_name)

    # Delete repo record
    delete_repo(workspace_id, repo_name)

    # Refresh the license server's active-repo snapshot (license layer
    # v2.0.0 desired-state model).
    _snapshot_active_repos()

    return _response(200, {"message": "Repo removed"})


def handle_trigger_sync(event: Dict) -> Dict:
    """POST /workspaces/{id}/github/sync"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        body = {}

    # Optionally sync specific repo
    repo_name = body.get("repo_name")

    if repo_name:
        # Sync single repo
        repo = get_repo(workspace_id, repo_name)
        if not repo:
            return _error(404, "Repo not found")

        if not repo.get("include_code_maps", True):
            return _error(400, "Code maps not enabled for this repo")

        status = repo.get("last_sync_status")
        if status in ("queued", "in_progress"):
            return _response(200, {"message": "Sync already in progress", "status": status})

        update_repo_sync_status(workspace_id, repo_name, "queued")
        queue_sync_job(workspace_id, repo_name, repo["installation_id"])

        return _response(200, {"message": "Sync queued", "repos": 1})
    else:
        # Sync all repos with code maps enabled
        repos = list_repos(workspace_id)
        queued = 0

        for repo in repos:
            if not repo.get("include_code_maps", True):
                continue
            if repo.get("last_sync_status") in ("queued", "in_progress"):
                continue

            update_repo_sync_status(workspace_id, repo["repo_name"], "queued")
            queue_sync_job(workspace_id, repo["repo_name"], repo["installation_id"])
            queued += 1

        return _response(200, {"message": "Sync queued", "repos": queued})


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
    """Main Lambda handler for GitHub integration."""
    http_method = (
        event.get("httpMethod")
        or event.get("requestContext", {}).get("http", {}).get("method", "GET")
    )
    path = (
        event.get("path")
        or event.get("requestContext", {}).get("http", {}).get("path", "/")
    )

    logger.info(f"GitHub API: {http_method} {path}")

    parts = _parse_path(path)

    try:
        # OAuth routes
        if parts == ("github", "authorize") and http_method == "GET":
            return handle_authorize(event)

        if parts == ("github", "callback") and http_method in ("GET", "POST"):
            return handle_callback(event)

        # Installation routes: /workspaces/{id}/github/installations
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "github"
            and parts[3] == "installations"
            and http_method == "GET"
        ):
            event["pathParameters"] = {"id": parts[1]}
            return handle_list_installations(event)

        # Installation repos: /workspaces/{id}/github/installations/{instId}/repos
        if (
            len(parts) == 6
            and parts[0] == "workspaces"
            and parts[2] == "github"
            and parts[3] == "installations"
            and parts[5] == "repos"
            and http_method == "GET"
        ):
            event["pathParameters"] = {"id": parts[1], "installationId": parts[4]}
            return handle_list_installation_repos(event)

        # Repo routes: /workspaces/{id}/github/repos
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "github"
            and parts[3] == "repos"
        ):
            event["pathParameters"] = {"id": parts[1]}
            if http_method == "GET":
                return handle_list_repos(event)
            if http_method == "POST":
                try:
                    guard_active("github_add_repos", workspace_id=parts[1])
                except RegionPassiveError:
                    return passive_response("github_add_repos")
                return handle_add_repos(event)

        # Single repo routes: /workspaces/{id}/github/repos/{name}
        # Note: name may contain slashes (owner/repo), so we need to join remaining parts
        if (
            len(parts) >= 5
            and parts[0] == "workspaces"
            and parts[2] == "github"
            and parts[3] == "repos"
        ):
            repo_name = "/".join(parts[4:])
            event["pathParameters"] = {"id": parts[1], "repoName": repo_name}
            if http_method == "PUT":
                try:
                    guard_active("github_update_repo", workspace_id=parts[1])
                except RegionPassiveError:
                    return passive_response("github_update_repo")
                return handle_update_repo(event)
            if http_method == "DELETE":
                try:
                    guard_active("github_delete_repo", workspace_id=parts[1])
                except RegionPassiveError:
                    return passive_response("github_delete_repo")
                return handle_delete_repo(event)

        # Sync route: /workspaces/{id}/github/sync
        if (
            len(parts) == 4
            and parts[0] == "workspaces"
            and parts[2] == "github"
            and parts[3] == "sync"
            and http_method == "POST"
        ):
            event["pathParameters"] = {"id": parts[1]}
            try:
                guard_active("github_trigger_sync", workspace_id=parts[1])
            except RegionPassiveError:
                return passive_response("github_trigger_sync")
            return handle_trigger_sync(event)

        return _error(404, "Not found")

    except ClientError as e:
        logger.exception("DynamoDB error")
        return _error(500, "Database error")
    except GitHubAppError as e:
        logger.exception("GitHub API error")
        return _error(502, f"GitHub API error: {e}")
    except Exception as e:
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")
