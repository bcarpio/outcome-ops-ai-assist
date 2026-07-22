"""
GitLab Integration Lambda for OutcomeOps AI Assist.

Handles GitLab OAuth 2.0 (Model A delegated OAuth) + connection / repo
management against the customer's GitLab instance (gitlab.com SaaS by
default; self-managed base URL is a later chunk per ADR-050).

GitLab differs from the Azure DevOps template (which this mirrors) in two
important ways:

1. **Two-level namespace, not three.** GitLab repos live under a group and
   (optionally nested sub)group -> project: the enrollable slug is the full
   ``path_with_namespace`` (e.g. ``group/subgroup/project``) with a VARIABLE
   number of slashes (>= 1 slash, >= 2 segments). ADO is a fixed
   ``{org}/{project}/{repo}``. The discovery cascade is therefore one level
   shallower: connection -> groups -> projects (projects ARE the repos; there
   is no ADO-style third "repos under a project" hop). The DDB SK is
   ``GITLAB_REPO#{slug}`` per ADR-050.
2. **Delegated token plumbing lives in the shared module.** ``shared.oauth.gitlab``
   owns the OAuth HTTP AND the SSM client-secret read AND KMS/DDB token
   persistence (via ``shared.entra_id_oauth``). The handler never touches the
   client secret directly and does not call ``save_tokens`` -- the exchange
   helper persists tokens keyed by ``connection_id`` itself.

API Endpoints:
    GET    /gitlab/authorize?workspace_id={id}            -- OAuth URL
    GET    /gitlab/callback                               -- OAuth callback
    GET    /workspaces/{id}/gitlab/connections            -- List connections
    GET    /workspaces/{id}/gitlab/connections/{cid}/groups
    GET    /workspaces/{id}/gitlab/connections/{cid}/groups/{gid}/projects
    POST   /workspaces/{id}/gitlab/repos                  -- Add repos
    GET    /workspaces/{id}/gitlab/repos                  -- List workspace repos
    PUT    /workspaces/{id}/gitlab/repos/{slug}           -- Update settings
    DELETE /workspaces/{id}/gitlab/repos/{slug}           -- Remove repo
    DELETE /workspaces/{id}/gitlab/connections/{cid}      -- Disconnect

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
from shared import gitlab_api
from shared import gitlab_instances  # noqa: E402
from shared.audit_writer import (  # noqa: E402
    ACTION_OAUTH_CONNECTION_ESTABLISHED,
    log_admin_action,
)
from shared.delete_dispatch import (  # noqa: E402
    dispatch_repo_delete,
    region_from_sqs_url,
)
from shared.license_repos import get_active_repos_for_customer  # noqa: E402
from shared.oauth import gitlab as gitlab_oauth  # noqa: E402
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


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
OAUTH_KMS_KEY_ARN = os.environ.get("OAUTH_KMS_KEY_ARN", "")

# Client + redirect config (set at deploy time from tfvars). The client
# secret is read from SSM by shared.oauth.gitlab itself; the handler only
# needs the client id (for the "app configured" check) + redirect URI.
GITLAB_CLIENT_ID = os.environ.get("GITLAB_CLIENT_ID", "")
GITLAB_REDIRECT_URI = os.environ.get("GITLAB_REDIRECT_URI", "")

# Sync + delete queues (chunk-03). The sync queue receives the initial
# sync job on add-repos; the shared delete queue receives S3-mirror
# prefix cleanup on delete-repo / disconnect.
GITLAB_SYNC_QUEUE = os.environ.get("GITLAB_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")
# Secondary-region delete queue so each region's delete-worker cleans its
# own stores under HA (delete-bug 02). Empty in single-region mode.
DELETE_QUEUE_SECONDARY = os.environ.get("DELETE_QUEUE_SECONDARY")

# Cached clients
_dynamodb = None
_sqs = None
_sqs_secondary = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
    return _dynamodb


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", config=_BOTO_CONFIG)
    return _sqs


def _get_sqs_secondary():
    """SQS client targeting the secondary-region delete queue, or None
    when not configured. Region is derived from the queue URL."""
    global _sqs_secondary
    if not DELETE_QUEUE_SECONDARY:
        return None
    if _sqs_secondary is None:
        region = region_from_sqs_url(DELETE_QUEUE_SECONDARY)
        _sqs_secondary = (
            boto3.client("sqs", region_name=region, config=_BOTO_CONFIG)
            if region
            else None
        )
    return _sqs_secondary


def _queue_sync_job(workspace_id: str, slug: str, connection_id: str) -> None:
    """Enqueue the initial sync job to the GitLab sync queue. The message
    shape must match what the gitlab-sync worker + gitlab-scheduler produce
    (workspace_id, connection_id, slug, group, project, triggered_by)."""
    if not GITLAB_SYNC_QUEUE:
        logger.warning("GITLAB_SYNC_QUEUE not configured")
        return
    parts = _slug_to_parts(slug)
    if not parts:
        logger.warning("Malformed slug %s -- not enqueuing sync", slug)
        return
    group, project = parts
    _get_sqs().send_message(
        QueueUrl=GITLAB_SYNC_QUEUE,
        MessageBody=json.dumps({
            "workspace_id": workspace_id,
            "connection_id": connection_id,
            "slug": slug,
            "group": group,
            "project": project,
            "triggered_by": "manual",
        }),
    )
    logger.info("Queued GitLab sync for %s/%s", workspace_id, slug)


def _queue_delete_job(workspace_id: str, slug: str) -> None:
    """Enqueue COMPLETE cleanup for a removed GitLab repo (ADR-052).

    A repo's data spans three shapes that a single slug-prefix delete
    misses: the source mirror + per-doc chunk vectors under
    ``gitlab/{slug}/``; the bare-name architectural-summary +
    code-map/summary vectors under ``gitlab/{bare}/`` and
    ``{ws}#gitlab#{bare}#...``; and the symbol-graph facts + code-maps
    pointer (keyed by the BARE name). GitLab routes through the same
    ``dispatch_repo_delete`` helper as GitHub so all three are queued in
    every region -- the bare name is the last path segment of the slug,
    so nested subgroups (``group/subgroup/project``) collapse correctly."""
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured")
        return
    targets = [(_get_sqs(), DELETE_QUEUE)]
    secondary = _get_sqs_secondary()
    if secondary is not None:
        targets.append((secondary, DELETE_QUEUE_SECONDARY))
    sent = dispatch_repo_delete(workspace_id, slug, targets, source="gitlab")
    logger.info(
        "Queued full cleanup for %s/%s: %d message(s) across %d region(s)",
        workspace_id, slug, sent, len(targets),
    )


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
# Auth helpers (mirrors github-integration / azure-devops-integration)
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


def handle_authorize(event: Dict) -> Dict:
    """GET /gitlab/authorize?workspace_id=..."""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")

    query_params = event.get("queryStringParameters") or {}
    workspace_id = query_params.get("workspace_id")
    if not workspace_id:
        return _error(400, "workspace_id is required")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can connect GitLab")

    # Which GitLab instance are we connecting? The UI passes ?instance=<id>
    # on a multi-instance deploy; single-instance deploys omit it and get
    # the sole "default" instance. Unknown id -> loud 400 rather than
    # authorizing against the wrong GitLab.
    requested = (event.get("queryStringParameters") or {}).get("instance")
    try:
        instance = gitlab_instances.get_instance(requested)
    except gitlab_instances.GitLabInstanceError as e:
        return _error(400, f"Unknown GitLab instance: {e}")

    if not (instance.client_id and GITLAB_REDIRECT_URI):
        return _error(500, "GitLab OAuth app not configured")

    state = json.dumps({
        "workspace_id": workspace_id,
        "user_email": user_email,
        "instance_id": instance.id,
    })
    auth_url = gitlab_oauth.build_authorize_url(
        redirect_uri=GITLAB_REDIRECT_URI,
        state=urllib.parse.quote(state),
        instance=instance,
    )
    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict) -> Dict:
    """GET /gitlab/callback - GitLab OAuth callback."""
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
        logger.error("GitLab OAuth error: %s", error)
        return _redirect(f"{base_redirect}?gitlab_error=oauth_denied")

    if not code or not state:
        return _redirect(f"{base_redirect}?gitlab_error=invalid_callback")

    try:
        if state.startswith("%"):
            state = urllib.parse.unquote(state)
        state_data = json.loads(state)
        workspace_id = state_data["workspace_id"]
        callback_user_email = state_data["user_email"]
        # Absent on connections started before multi-instance -> "default".
        instance_id = state_data.get("instance_id")
    except (json.JSONDecodeError, KeyError):
        return _redirect(f"{base_redirect}?gitlab_error=invalid_state")

    try:
        instance = gitlab_instances.get_instance(instance_id)
    except gitlab_instances.GitLabInstanceError:
        logger.exception("GitLab callback for unknown instance %s", instance_id)
        return _redirect(f"{base_redirect}?gitlab_error=invalid_state")

    # The connection_id is minted BEFORE the exchange because the shared
    # exchange helper persists the (KMS-encrypted) tokens keyed by
    # connection_id itself.
    connection_id = secrets.token_urlsafe(16)

    try:
        access_token = gitlab_oauth.exchange_code_for_tokens(
            code=code,
            redirect_uri=GITLAB_REDIRECT_URI,
            workspaces_table=WORKSPACES_TABLE,
            kms_key_arn=OAUTH_KMS_KEY_ARN,
            connection_id=connection_id,
            instance=instance,
        )
    except Exception:
        logger.exception("GitLab token exchange failed")
        return _redirect(f"{base_redirect}?gitlab_error=token_exchange_failed")
    if not access_token:
        return _redirect(f"{base_redirect}?gitlab_error=token_exchange_failed")

    try:
        gitlab_user_id, gitlab_username = gitlab_oauth.get_user_profile(
            access_token, base_url_arg=instance.base_url)
    except Exception:
        logger.exception("GitLab profile lookup failed")
        return _redirect(f"{base_redirect}?gitlab_error=profile_lookup_failed")

    display_name = gitlab_username or callback_user_email
    now = _now_iso()

    connection_row = {
        "PK": f"WS#{workspace_id}",
        "SK": f"GITLAB_CONNECTION#{connection_id}",
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "user_email": callback_user_email,
        "gitlab_user_id": gitlab_user_id,
        "gitlab_username": gitlab_username,
        "display_name": display_name,
        "instance_id": instance.id,
        "instance_label": instance.display_name,
        "base_url": instance.base_url,
        "created_at": now,
        "updated_at": now,
    }

    try:
        dual_write.ddb_put_item(WORKSPACES_TABLE, connection_row)
    except ClientError:
        logger.exception("Failed to persist GitLab connection")
        # Tokens were already saved under connection_id; clean them up so we
        # don't orphan a token row without a connection.
        try:
            gitlab_oauth.delete_tokens(WORKSPACES_TABLE, connection_id)
        except Exception:
            logger.warning("Failed to clean up orphaned GitLab tokens")
        return _redirect(f"{base_redirect}?gitlab_error=database_error")

    try:
        log_admin_action(
            os.environ.get("AUDIT_TABLE", ""),
            ACTION_OAUTH_CONNECTION_ESTABLISHED,
            workspace_id=workspace_id,
            user_email=callback_user_email,
            source_ip="",
            user_agent="",
            details={
                "provider": "gitlab",
                "connection_id": connection_id,
                "account_label": display_name,
                "gitlab_username": gitlab_username,
            },
        )
    except Exception as e:
        logger.warning(f"Failed to emit OAUTH_CONNECTION_ESTABLISHED audit: {e}")

    _snapshot_active_repos()

    return _redirect(
        f"/workspaces/{workspace_id}/settings?tab=integrations"
        f"&gitlab_connected={connection_id}"
    )


# ---------------------------------------------------------------------------
# Connection / discovery routes
# ---------------------------------------------------------------------------


def _get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"GITLAB_CONNECTION#{connection_id}",
        }
    )
    return response.get("Item")


def _resolve_connection_instance(workspace_id: str, connection_id: str):
    """The GitLabInstance a connection belongs to, from its stored
    instance_id (absent on pre-multi-instance rows -> resolves to the sole
    instance). Returns None if the connection row is missing."""
    row = _get_connection(workspace_id, connection_id)
    if not row:
        return None
    return gitlab_instances.get_instance(row.get("instance_id"))


def _connection_access_token(connection_id: str,
                             instance=None) -> Optional[str]:
    """Get a usable access token for the connection via the cached,
    single-flight path (ADR-050 v1.1). ``instance`` selects which GitLab's
    OAuth app + secret the refresh uses; None resolves to the sole
    instance (raising on a multi-instance deploy so a caller that forgot
    to pass it fails loudly)."""
    try:
        return gitlab_oauth.get_valid_access_token(
            workspaces_table=WORKSPACES_TABLE,
            kms_key_arn=OAUTH_KMS_KEY_ARN,
            connection_id=connection_id,
            instance=instance,
        )
    except Exception:
        logger.exception("Failed to obtain GitLab access token")
        return None


def handle_list_instances(event: Dict) -> Dict:
    """GET /gitlab/instances -- the GitLab instances this deploy can connect.
    Drives the UI's instance picker; single-instance deploys return one entry
    (the UI then shows a plain "Connect GitLab" button)."""
    try:
        instances = gitlab_instances.list_instances()
    except gitlab_instances.GitLabInstanceError as e:
        return _error(500, f"GitLab instance config error: {e}")
    return _response(200, {
        "instances": [
            {"id": i.id, "display_name": i.display_name, "base_url": i.base_url}
            for i in instances
        ],
        "multi_instance": gitlab_instances.is_multi_instance(),
    })


def handle_list_connections(event: Dict) -> Dict:
    """GET /workspaces/{id}/gitlab/connections"""
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
            ":sk": "GITLAB_CONNECTION#",
        },
    )
    connections = []
    for item in response.get("Items", []):
        connections.append({
            "connection_id": item.get("connection_id"),
            "display_name": item.get("display_name"),
            "gitlab_username": item.get("gitlab_username"),
            "base_url": item.get("base_url"),
            "instance_id": item.get("instance_id", gitlab_instances.DEFAULT_INSTANCE_ID),
            "instance_label": item.get("instance_label"),
            "created_at": item.get("created_at"),
        })
    return _response(200, {"connections": connections})


def handle_list_groups(event: Dict) -> Dict:
    """GET /workspaces/{id}/gitlab/connections/{cid}/groups"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")

    instance = _resolve_connection_instance(workspace_id, connection_id)
    if instance is None:
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id, instance)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        groups = gitlab_api.list_groups(access_token, base_url=instance.base_url)
    except Exception as e:
        logger.exception("GitLab list_groups error")
        return _error(502, f"GitLab API error: {e}")

    return _response(200, {
        "groups": [
            {
                "id": g.get("id"),
                "full_path": g.get("full_path"),
                "name": g.get("name"),
            }
            for g in groups
        ]
    })


def handle_list_group_projects(event: Dict) -> Dict:
    """GET /workspaces/{id}/gitlab/connections/{cid}/groups/{gid}/projects

    Projects here ARE the enrollable repos (GitLab has no ADO-style third
    "repos under a project" hop). ``list_group_projects`` includes nested
    subgroups, so a returned slug can have more than two path segments.
    """
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")
    group_id = path_params.get("groupId")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _can_access_workspace(table, user_email, workspace_id):
        return _error(403, "Not a member of this workspace")
    if not _get_connection(workspace_id, connection_id):
        return _error(404, "Connection not found")

    instance = _resolve_connection_instance(workspace_id, connection_id)
    if instance is None:
        return _error(404, "Connection not found")

    access_token = _connection_access_token(connection_id, instance)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    try:
        projects = gitlab_api.list_group_projects(
            access_token, group_id, base_url=instance.base_url)
    except Exception as e:
        logger.exception("GitLab list_group_projects error")
        return _error(502, f"GitLab API error: {e}")

    # Already-added repos for this workspace
    added_slugs = {r["slug"] for r in _list_workspace_repos(workspace_id)}

    return _response(200, {
        "projects": [
            {
                "id": p.get("id"),
                "slug": p.get("slug"),
                "name": p.get("name"),
                "default_branch": p.get("default_branch"),
                "already_added": p.get("slug") in added_slugs,
            }
            for p in projects
        ]
    })


# ---------------------------------------------------------------------------
# Workspace repo set
# ---------------------------------------------------------------------------


def _slug_to_parts(slug: str) -> Optional[Tuple[str, str]]:
    """Split a GitLab ``group/.../project`` slug into ``(group, project)``.

    GitLab supports nested subgroups, so the slug has a VARIABLE number of
    segments (>= 2). ``project`` is the last segment; ``group`` is everything
    before it (subgroups included). Returns None if the slug has < 2 segments.
    """
    parts = [p for p in slug.split("/") if p]
    if len(parts) < 2:
        return None
    return "/".join(parts[:-1]), parts[-1]


def _list_workspace_repos(workspace_id: str) -> List[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "GITLAB_REPO#",
        },
    )
    out = []
    for item in response.get("Items", []):
        sk = item.get("SK", "")
        slug = sk.replace("GITLAB_REPO#", "") if sk else ""
        out.append({**item, "slug": slug})
    return out


def _get_workspace_repo(workspace_id: str, slug: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"GITLAB_REPO#{slug}",
        }
    )
    return response.get("Item")


def handle_add_repos(event: Dict) -> Dict:
    """POST /workspaces/{id}/gitlab/repos"""
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

    instance = gitlab_instances.get_instance(connection.get("instance_id"))
    access_token = _connection_access_token(connection_id, instance)
    if not access_token:
        return _error(401, "Failed to refresh access token. Please reconnect.")

    # Same-bare-name guard (ADR-050 v2.0 decision): the vector /
    # symbol-graph / code-map keys are {ws}#gitlab#{bare-name}#... with no
    # instance discriminator, so enrolling group/project from gitlab.com AND
    # from a self-managed instance into ONE workspace would silently collide
    # in retrieval. Refuse at enroll time with a clear error rather than
    # corrupt the KB. Precompute the bare names already enrolled from a
    # DIFFERENT instance.
    _bare_from_other_instance = {}
    for existing in _list_workspace_repos(workspace_id):
        ex_slug = existing.get("slug") or ""
        ex_inst = existing.get("instance_id", gitlab_instances.DEFAULT_INSTANCE_ID)
        if ex_slug and ex_inst != instance.id:
            _bare_from_other_instance[ex_slug.rsplit("/", 1)[-1]] = ex_inst

    added: List[Dict] = []
    skipped: List[Dict] = []

    for repo in repos:
        if not isinstance(repo, dict):
            continue
        # The slug is the GitLab full project path (path_with_namespace),
        # which list_group_projects returns directly.
        slug = repo.get("slug")
        if not slug:
            skipped.append({"repo": repo, "reason": "missing slug"})
            continue
        parts = _slug_to_parts(slug)
        if not parts:
            skipped.append({"slug": slug, "reason": "invalid slug; expected group/project"})
            continue
        derived_group, derived_project = parts
        group = repo.get("group") or derived_group
        project = repo.get("project") or derived_project
        repo_type = repo.get("repo_type", "application")
        include_code_maps = repo.get("include_code_maps", True)
        # UI may pass an explicit branch override (customer picked a branch
        # in the auto-detect + confirmable widget). If not provided, resolve
        # via the GitLab API. Whichever wins gets persisted to the DDB row;
        # downstream syncs read from there.
        branch_override: Optional[str] = repo.get("default_branch")

        if _get_workspace_repo(workspace_id, slug):
            skipped.append({"slug": slug, "reason": "already exists"})
            continue

        bare = slug.rsplit("/", 1)[-1]
        if bare in _bare_from_other_instance:
            skipped.append({
                "slug": slug,
                "reason": (
                    f"a project named '{bare}' is already enrolled in this "
                    f"workspace from a different GitLab instance "
                    f"({_bare_from_other_instance[bare]}); enroll it in a "
                    f"separate workspace to avoid a knowledge-base collision"
                ),
            })
            continue

        try:
            default_branch = branch_override or gitlab_api.get_default_branch(
                access_token, slug, base_url=instance.base_url,
            )
            latest_sha = (
                gitlab_api.get_latest_commit_sha(
                    access_token, slug, default_branch,
                    base_url=instance.base_url)
                if default_branch else None
            )
        except Exception as e:
            logger.exception("GitLab metadata lookup failed for %s", slug)
            skipped.append({"slug": slug, "reason": f"GitLab error: {e}"})
            continue

        now = _now_iso()
        item = {
            "PK": f"WS#{workspace_id}",
            "SK": f"GITLAB_REPO#{slug}",
            "workspace_id": workspace_id,
            "provider": "gitlab",
            "group": group,
            "project": project,
            "slug": slug,
            "connection_id": connection_id,
            "instance_id": instance.id,
            "base_url": instance.base_url,
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
            # Enqueue the initial sync job so the repo is mirrored + code-
            # mapped immediately, rather than waiting for the next hourly
            # scheduler tick. Only when include_code_maps is set.
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
    """GET /workspaces/{id}/gitlab/repos"""
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
                "group": r.get("group"),
                "project": r.get("project"),
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


def handle_trigger_sync(event: Dict) -> Dict:
    """POST /workspaces/{id}/gitlab/sync

    Body optional. ``{"slug": "group/.../project"}`` syncs one repo; omitted
    syncs every enabled, code-map-enabled repo in the workspace. Enqueues to the
    GitLab sync queue (the gitlab-sync worker does the actual mirror + fan-out).
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

    queued = 0
    for repo in _list_workspace_repos(workspace_id):
        if not repo.get("enabled", True) or not repo.get("include_code_maps", True):
            continue
        _queue_sync_job(workspace_id, repo["slug"], repo["connection_id"])
        queued += 1
    return _response(200, {"message": "Sync queued", "repos": queued})


def handle_update_repo(event: Dict) -> Dict:
    """PUT /workspaces/{id}/gitlab/repos/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected group/project")

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
        {"PK": f"WS#{workspace_id}", "SK": f"GITLAB_REPO#{slug}"},
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


def handle_delete_repo(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/gitlab/repos/{slug}"""
    user_email = _get_user_from_request(event)
    if not user_email:
        return _error(401, "User email not provided")
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    slug = urllib.parse.unquote(path_params.get("slug", ""))
    if not _slug_to_parts(slug):
        return _error(400, "Invalid slug; expected group/project")

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    if not _is_workspace_admin(table, user_email, workspace_id):
        return _error(403, "Only workspace admins can remove repos")

    repo = _get_workspace_repo(workspace_id, slug)
    if not repo:
        return _error(404, "Repo not found")

    # Purge the sync-written blobs under gitlab/{slug}/ (+ their vectors)
    # via the shared delete worker before dropping the DDB row.
    _queue_delete_job(workspace_id, slug)
    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"GITLAB_REPO#{slug}"},
    )
    _snapshot_active_repos()
    return _response(200, {"message": "Repo removed"})


def handle_disconnect(event: Dict) -> Dict:
    """DELETE /workspaces/{id}/gitlab/connections/{cid}"""
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
        # Purge each repo's S3-mirror prefix before dropping its DDB row.
        _queue_delete_job(workspace_id, repo["slug"])
        dual_write.ddb_delete_item(
            WORKSPACES_TABLE,
            {"PK": f"WS#{workspace_id}", "SK": f"GITLAB_REPO#{repo['slug']}"},
        )

    dual_write.ddb_delete_item(
        WORKSPACES_TABLE,
        {"PK": f"WS#{workspace_id}", "SK": f"GITLAB_CONNECTION#{connection_id}"},
    )
    gitlab_oauth.delete_tokens(WORKSPACES_TABLE, connection_id)
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
    logger.info(f"GitLab API: {http_method} {path}")
    parts = _parse_path(path)

    try:
        # Deploy-level GitLab instance list (unauthenticated -- it is just
        # public config: which GitLab hosts this deploy can connect).
        if parts == ("gitlab", "instances") and http_method == "GET":
            return handle_list_instances(event)

        # OAuth (unauthenticated)
        if parts == ("gitlab", "authorize") and http_method == "GET":
            return handle_authorize(event)
        if parts == ("gitlab", "callback") and http_method in ("GET", "POST"):
            return handle_callback(event)

        # All other routes require auth + workspace membership.
        if len(parts) >= 3 and parts[0] == "workspaces" and parts[2] == "gitlab":
            workspace_id = parts[1]

            # /workspaces/{id}/gitlab/connections
            if len(parts) == 4 and parts[3] == "connections" and http_method == "GET":
                event["pathParameters"] = {"id": workspace_id}
                return handle_list_connections(event)

            # /workspaces/{id}/gitlab/connections/{cid}
            if (
                len(parts) == 5
                and parts[3] == "connections"
                and http_method == "DELETE"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                try:
                    guard_active("gitlab_disconnect", workspace_id=workspace_id)
                except RegionPassiveError:
                    return passive_response("gitlab_disconnect")
                return handle_disconnect(event)

            # /workspaces/{id}/gitlab/connections/{cid}/groups
            if (
                len(parts) == 6
                and parts[3] == "connections"
                and parts[5] == "groups"
                and http_method == "GET"
            ):
                event["pathParameters"] = {"id": workspace_id, "connectionId": parts[4]}
                return handle_list_groups(event)

            # /workspaces/{id}/gitlab/connections/{cid}/groups/{gid}/projects
            # gid is the numeric GitLab group id (no slashes), so this is a
            # fixed 8-segment shape.
            if (
                len(parts) == 8
                and parts[3] == "connections"
                and parts[5] == "groups"
                and parts[7] == "projects"
                and http_method == "GET"
            ):
                event["pathParameters"] = {
                    "id": workspace_id,
                    "connectionId": parts[4],
                    "groupId": parts[6],
                }
                return handle_list_group_projects(event)

            # /workspaces/{id}/gitlab/repos
            if len(parts) == 4 and parts[3] == "repos":
                event["pathParameters"] = {"id": workspace_id}
                if http_method == "GET":
                    return handle_list_repos(event)
                if http_method == "POST":
                    try:
                        guard_active("gitlab_add_repos", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("gitlab_add_repos")
                    return handle_add_repos(event)

            # /workspaces/{id}/gitlab/sync -- manual sync trigger (UI button)
            if len(parts) == 4 and parts[3] == "sync" and http_method == "POST":
                event["pathParameters"] = {"id": workspace_id}
                try:
                    guard_active("gitlab_trigger_sync", workspace_id=workspace_id)
                except RegionPassiveError:
                    return passive_response("gitlab_trigger_sync")
                return handle_trigger_sync(event)

            # /workspaces/{id}/gitlab/repos/{slug...}
            # ``slug`` is group/.../project and contains AT LEAST one slash
            # (>= 2 segments), so the slug spans parts[4:] with len >= 6.
            if len(parts) >= 6 and parts[3] == "repos":
                slug = "/".join(parts[4:])
                event["pathParameters"] = {"id": workspace_id, "slug": slug}
                if http_method == "PUT":
                    try:
                        guard_active("gitlab_update_repo", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("gitlab_update_repo")
                    return handle_update_repo(event)
                if http_method == "DELETE":
                    try:
                        guard_active("gitlab_delete_repo", workspace_id=workspace_id)
                    except RegionPassiveError:
                        return passive_response("gitlab_delete_repo")
                    return handle_delete_repo(event)

        return _error(404, "Not found")
    except ClientError:
        logger.exception("DynamoDB error")
        return _error(500, "Database error")
    except Exception:
        logger.exception("Unhandled error")
        return _error(500, "Internal server error")
