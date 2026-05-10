"""
Slack Integration Lambda for RetrieveIt.AI

Workspace-scoped integration following ADR-001. Connections, tokens, and
synced data are scoped to individual workspaces.

Routes:
    GET    /slack/authorize                                              - Generate OAuth URL
    GET    /slack/callback                                               - OAuth callback (creates connection + seeds user cache)
    GET    /workspace/{id}/slack/connections                             - List connections
    GET    /workspace/{id}/slack/connections/{connId}                    - Get connection details
    GET    /workspace/{id}/slack/connections/{connId}/channels           - List channels the bot is in
    PUT    /workspace/{id}/slack/connections/{connId}/config             - Save selected channels
    DELETE /workspace/{id}/slack/connections/{connId}                    - Disconnect
    POST   /workspace/{id}/slack/connections/{connId}/sync               - Manual sync

All routes return 404 when SLACK_ENABLED env var is not exactly "true".

ADR-002 reminder: every "source" value emitted to FILE_INGESTION_QUEUE here
must have a corresponding `process_<source>_file` and `elif source ==`
branch in lambda/file-ingestion/handler.py, or files silently disappear.
"""

import json
import logging
import os
import re
import secrets
import time
from decimal import Decimal
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import boto3
import jwt
from botocore.exceptions import ClientError

from token_manager import (
    save_tokens,
    delete_tokens,
    refresh_access_token,
    _get_client_id,
    _get_client_secret,
)
from slack_api import (
    oauth_v2_access,
    auth_test,
    iterate_users,
    users_info,
    iterate_member_channels,
    iterate_channel_messages,
    iterate_thread_replies,
    SlackRateLimitedError,
)


class DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return int(obj) if obj % 1 == 0 else float(obj)
        return super().default(obj)


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

HANDLER_NAME = "slack-integration"

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "app.retrieveit.ai")
TENANTS_TABLE = os.environ.get("TENANTS_TABLE")
DOCUMENTS_BUCKET = os.environ.get("DOCUMENTS_BUCKET")
SLACK_SYNC_QUEUE = os.environ.get("SLACK_SYNC_QUEUE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
DOCUMENT_DELETE_QUEUE = os.environ.get("DOCUMENT_DELETE_QUEUE")

# Source values dispatched in lambda/file-ingestion/handler.py. Per ADR-002
# every value here MUST have a corresponding `elif source == "slack_X":`
# branch + process_slack_X function in file-ingestion or messages drop silently.
SOURCE_MESSAGE = "slack_message"
SOURCE_FILE = "slack_file"

# Bot scopes the OAuth grant requests. Must match what the Slack app config
# has enabled in the developer console — Slack rejects unknown scopes at
# consent time. See plan/EXECUTE.md for the rationale on each.
SLACK_BOT_SCOPES = ",".join([
    "channels:history",
    "channels:read",
    "groups:history",
    "groups:read",
    "files:read",
    "users:read",
    "team:read",
])

BASE_URLS = {
    "dev": "https://app.dev.retrieveit.ai",
    "prd": "https://app.retrieveit.ai",
}

API_BASE_URLS = {
    "dev": "https://api.app.dev.retrieveit.ai",
    "prd": "https://api.app.retrieveit.ai",
}

OAUTH_STATE_TTL_SECONDS = 600
AUDIT_LOG_TTL_SECONDS = 90 * 24 * 3600
USER_CACHE_TTL_SECONDS = 24 * 3600

_dynamodb = None
_ssm_client = None
_sqs_client = None
_jwt_secret = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _get_jwt_secret() -> str:
    global _jwt_secret
    if _jwt_secret is None:
        client = _get_ssm_client()
        parameter_name = f"/{ENV}/{APP_NAME}/secrets/jwt_secret"
        response = client.get_parameter(Name=parameter_name, WithDecryption=True)
        _jwt_secret = response["Parameter"]["Value"]
    return _jwt_secret


def _is_enabled() -> bool:
    return os.environ.get("SLACK_ENABLED", "false").lower() == "true"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def generate_connection_id() -> str:
    return f"slack_{secrets.token_urlsafe(16)}"


# ============================================================================
# Response Helpers (5 mandatory security headers)
# ============================================================================


def _security_headers() -> Dict[str, str]:
    return {
        "Strict-Transport-Security": "max-age=31536000; includeSubDomains; preload",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
        "X-XSS-Protection": "1; mode=block",
        "Cache-Control": "no-store, no-cache, must-revalidate, private",
    }


def _response(status_code: int, body: Any) -> Dict:
    return {
        "statusCode": status_code,
        "headers": {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Headers": "Content-Type,Authorization",
            "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
            **_security_headers(),
        },
        "body": json.dumps(body, cls=DecimalEncoder),
    }


def _error(status_code: int, message: str) -> Dict:
    return _response(status_code, {"error": message})


def _redirect(url: str) -> Dict:
    return {
        "statusCode": 302,
        "headers": {
            "Location": url,
            **_security_headers(),
        },
        "body": "",
    }


# ============================================================================
# Auth
# ============================================================================


def get_auth_context(event: Dict) -> Optional[Dict]:
    headers = event.get("headers", {})
    auth_header = headers.get("authorization") or headers.get("Authorization")

    if not auth_header or not auth_header.startswith("Bearer "):
        return None

    token = auth_header[7:]

    try:
        return jwt.decode(token, _get_jwt_secret(), algorithms=["HS256"])
    except jwt.InvalidTokenError as e:
        logger.warning(f"[{HANDLER_NAME}] Invalid JWT: {e}")
        return None


def require_auth(event: Dict) -> tuple[Optional[Dict], Optional[Dict]]:
    auth_context = get_auth_context(event)
    if not auth_context:
        return None, _error(401, "Unauthorized")

    user_id = auth_context.get("sub")
    org_id = auth_context.get("org_id")

    if not user_id or not org_id:
        return None, _error(401, "Invalid token claims")

    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"ORG_MEMBER#{org_id}"}
    )
    membership = response.get("Item")

    if not membership:
        logger.warning(
            f"[{HANDLER_NAME}] SECURITY: Rejected request from removed user "
            f"user_id={user_id} org_id={org_id}"
        )
        return None, _error(401, "User is no longer a member of this organization")

    auth_context["org_role"] = membership.get("role", "member")
    return auth_context, None


def check_workspace_access(
    auth_context: Dict,
    workspace_id: str,
    required_role: str = "member",
) -> Optional[Dict]:
    user_id = auth_context.get("sub")

    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.get_item(
        Key={"PK": f"USER#{user_id}", "SK": f"WS_MEMBER#{workspace_id}"}
    )
    membership = response.get("Item")

    if not membership:
        return _error(403, "Not a member of this workspace")

    role = membership.get("role", "member")
    role_hierarchy = {"member": 0, "workspace_admin": 1, "org_admin": 2}
    if role_hierarchy.get(role, 0) < role_hierarchy.get(required_role, 0):
        return _error(403, f"Requires {required_role} role")

    return None


# ============================================================================
# Audit Logs
# ============================================================================


def create_audit_log(
    org_id: str,
    actor_id: str,
    actor_email: str,
    action: str,
    resource_type: str,
    resource_id: str,
    details: Optional[Dict] = None,
    workspace_id: Optional[str] = None,
) -> None:
    timestamp = now_iso()
    audit_id = f"audit_{secrets.token_urlsafe(12)}"
    ttl = int(time.time()) + AUDIT_LOG_TTL_SECONDS

    item = {
        "PK": f"ORG#{org_id}",
        "SK": f"AUDIT#{timestamp}#{audit_id}",
        "GSI1PK": f"USER#{actor_id}",
        "GSI1SK": f"AUDIT#{timestamp}#{audit_id}",
        "entity_type": "audit_log",
        "audit_id": audit_id,
        "org_id": org_id,
        "timestamp": timestamp,
        "actor_id": actor_id,
        "actor_email": actor_email,
        "action": action,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "ttl": ttl,
    }
    if details:
        item["details"] = details
    if workspace_id:
        item["workspace_id"] = workspace_id

    try:
        _get_dynamodb().Table(TENANTS_TABLE).put_item(Item=item)
    except Exception:
        logger.exception(f"[{HANDLER_NAME}] Failed to write audit log {action}")


# ============================================================================
# OAuth State (single-use, replay-safe)
# ============================================================================


def _claim_oauth_state(state: str, payload: Dict) -> bool:
    expires_at = int(time.time()) + OAUTH_STATE_TTL_SECONDS
    item = {
        "PK": f"SLACK_STATE#{state}",
        "SK": f"SLACK_STATE#{state}",
        "entity_type": "slack_oauth_state",
        "state_payload": payload,
        "ttl": expires_at + 60,
    }
    try:
        _get_dynamodb().Table(TENANTS_TABLE).put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(PK)",
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _consume_oauth_state(state: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(TENANTS_TABLE)
    try:
        response = table.delete_item(
            Key={"PK": f"SLACK_STATE#{state}", "SK": f"SLACK_STATE#{state}"},
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_OLD",
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return None
        raise

    attrs = response.get("Attributes") or {}
    if not attrs:
        return None

    ttl = attrs.get("ttl")
    if ttl is not None and int(ttl) < int(time.time()):
        return None

    payload = attrs.get("state_payload") or {}
    return payload if isinstance(payload, dict) else None


# ============================================================================
# User cache (cross-sync DynamoDB cache for user_id -> display_name)
# ============================================================================
# Seeded by _populate_user_cache at OAuth callback time using one bulk
# users.list pagination. _resolve_user reads from cache; on miss falls
# back to single users.info call and writes to cache. 24h TTL per row.
# See plan/open-questions.md #6 for rationale.


def _user_cache_key(team_id: str, user_id: str) -> Dict[str, str]:
    return {
        "PK": f"SLACK_USER_CACHE#{team_id}",
        "SK": f"USER#{user_id}",
    }


def _user_cache_write(team_id: str, user: Dict) -> None:
    """Write one user to the cache. `user` is the Slack users.info member shape."""
    user_id = user.get("id")
    if not user_id:
        return
    profile = user.get("profile") or {}
    display = (
        profile.get("display_name")
        or profile.get("real_name")
        or user.get("real_name")
        or user.get("name")
        or user_id
    )
    real = profile.get("real_name") or user.get("real_name") or display
    item = {
        **_user_cache_key(team_id, user_id),
        "entity_type": "slack_user_cache",
        "team_id": team_id,
        "user_id": user_id,
        "display_name": display,
        "real_name": real,
        "deleted": bool(user.get("deleted", False)),
        "ttl": int(time.time()) + USER_CACHE_TTL_SECONDS,
    }
    try:
        _get_dynamodb().Table(TENANTS_TABLE).put_item(Item=item)
    except Exception:
        logger.exception(f"[{HANDLER_NAME}] Failed to cache user {user_id}")


def _populate_user_cache(team_id: str, access_token: str) -> int:
    """Bulk-seed the user cache from users.list. Returns count written.

    Called at OAuth callback time so the first sync doesn't burn rate
    limit doing per-message users.info lookups. Failures are non-fatal —
    the connection still works, just slower until cache backfills via
    cache-miss writes during sync.
    """
    count = 0
    try:
        for user in iterate_users(access_token):
            _user_cache_write(team_id, user)
            count += 1
    except Exception:
        logger.exception(
            f"[{HANDLER_NAME}] Failed during users.list seed for team {team_id}; "
            f"cache populated with {count} users so far"
        )
    logger.info(f"[{HANDLER_NAME}] Seeded user cache: {count} users for team {team_id}")
    return count


def _resolve_user(team_id: str, user_id: str, access_token: str) -> str:
    """Return display name for a Slack user_id. Cache-then-API."""
    if not user_id:
        return ""
    table = _get_dynamodb().Table(TENANTS_TABLE)
    try:
        response = table.get_item(Key=_user_cache_key(team_id, user_id))
        item = response.get("Item")
        if item:
            ttl = item.get("ttl")
            if ttl is None or int(ttl) > int(time.time()):
                return item.get("display_name") or user_id
    except Exception:
        logger.exception(f"[{HANDLER_NAME}] User cache read failed for {user_id}")

    # Cache miss or stale — fetch and write.
    try:
        body = users_info(access_token, user_id)
        user = body.get("user") or {}
        _user_cache_write(team_id, user)
        profile = user.get("profile") or {}
        return (
            profile.get("display_name")
            or profile.get("real_name")
            or user.get("real_name")
            or user.get("name")
            or user_id
        )
    except Exception:
        logger.warning(
            f"[{HANDLER_NAME}] users.info fallback failed for {user_id}; "
            f"using raw id as display"
        )
        return user_id


_USER_MENTION_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")


def _substitute_mentions(body: str, team_id: str, access_token: str) -> str:
    """Replace `<@USER_ID>` mentions with `@<display_name>`."""
    if not body:
        return ""

    def _sub(match):
        user_id = match.group(1)
        return f"@{_resolve_user(team_id, user_id, access_token)}"

    return _USER_MENTION_RE.sub(_sub, body)


# ============================================================================
# Connection storage
# ============================================================================


def save_connection(
    connection_id: str,
    workspace_id: str,
    org_id: str,
    team_id: str,
    team_name: str,
    bot_user_id: str,
    created_by: str,
) -> None:
    now = now_iso()
    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.put_item(
        Item={
            "PK": f"WS#{workspace_id}",
            "SK": f"SLACK_CONNECTION#{connection_id}",
            "GSI1PK": f"ORG#{org_id}",
            "GSI1SK": f"SLACK_CONNECTION#{connection_id}",
            "entity_type": "slack_connection",
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "org_id": org_id,
            "team_id": team_id,
            "team_name": team_name,
            "bot_user_id": bot_user_id,
            "config": None,
            "last_sync_at": None,
            "last_sync_status": "needs_configuration",
            "last_sync_error": None,
            "file_count": 0,
            "created_by": created_by,
            "created_at": now,
            "updated_at": now,
        }
    )
    logger.info(f"[{HANDLER_NAME}] Saved connection {connection_id} for workspace {workspace_id}")


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"}
    )
    return response.get("Item")


def list_connections(workspace_id: str) -> List[Dict]:
    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.query(
        KeyConditionExpression="PK = :pk AND begins_with(SK, :sk)",
        FilterExpression="entity_type = :et",
        ExpressionAttributeValues={
            ":pk": f"WS#{workspace_id}",
            ":sk": "SLACK_CONNECTION#",
            ":et": "slack_connection",
        },
    )
    return response.get("Items", [])


def delete_connection(workspace_id: str, connection_id: str) -> None:
    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.delete_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"}
    )
    logger.info(f"[{HANDLER_NAME}] Deleted connection {connection_id}")


def update_connection_config(workspace_id: str, connection_id: str, config: Dict) -> None:
    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.update_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"},
        UpdateExpression="SET config = :config, updated_at = :now",
        ExpressionAttributeValues={":config": config, ":now": now_iso()},
    )


def _increment_file_count(workspace_id: str, connection_id: str, delta: int) -> None:
    """Atomically add `delta` to the connection row's file_count.

    Same pattern Clio + Dropbox use. Slack delta = newly-arrived messages
    (no deletion signal in conversations.history; messages just stop
    showing up if removed). Edits inflate the count; documented caveat.
    """
    if not delta:
        return
    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.update_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"},
        UpdateExpression="ADD file_count :delta",
        ExpressionAttributeValues={":delta": delta},
    )


def update_sync_status(
    workspace_id: str,
    connection_id: str,
    status: str,
    error: Optional[str] = None,
    file_count: Optional[int] = None,
    cursors: Optional[Dict[str, str]] = None,
) -> None:
    table = _get_dynamodb().Table(TENANTS_TABLE)

    set_parts = ["last_sync_status = :status", "updated_at = :now"]
    remove_parts = []
    expr_values = {":status": status, ":now": now_iso()}

    if status in ("success", "partial_success", "failed", "auth_error", "reauth_required"):
        set_parts.append("last_sync_at = :sync_at")
        expr_values[":sync_at"] = now_iso()

    if error is not None:
        set_parts.append("last_sync_error = :error")
        expr_values[":error"] = error
    else:
        remove_parts.append("last_sync_error")

    if file_count is not None:
        set_parts.append("file_count = :files")
        expr_values[":files"] = file_count

    if cursors is not None:
        set_parts.append("config.cursors = :cursors")
        expr_values[":cursors"] = cursors

    update_expr = "SET " + ", ".join(set_parts)
    if remove_parts:
        update_expr += " REMOVE " + ", ".join(remove_parts)

    table.update_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"},
        UpdateExpression=update_expr,
        ExpressionAttributeValues=expr_values,
    )


# ============================================================================
# SQS
# ============================================================================


def queue_sync_job(job: Dict) -> None:
    sqs = _get_sqs_client()
    sqs.send_message(QueueUrl=SLACK_SYNC_QUEUE, MessageBody=json.dumps(job))
    logger.info(f"[{HANDLER_NAME}] Queued sync job for connection {job.get('connection_id')}")


def queue_message_for_ingestion(
    connection_id: str,
    workspace_id: str,
    org_id: str,
    message: Dict,
    sync_id: str,
    file_index: int,
) -> None:
    """Send one Slack message + thread (already enriched) to FILE_INGESTION_QUEUE."""
    sqs = _get_sqs_client()
    sqs.send_message(
        QueueUrl=FILE_INGESTION_QUEUE,
        MessageBody=json.dumps({
            "source": SOURCE_MESSAGE,
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "org_id": org_id,
            "file": message,
            "sync_id": sync_id,
            "file_index": file_index,
            "total_files": 0,
        }, cls=DecimalEncoder),
    )


def queue_file_for_ingestion(
    connection_id: str,
    workspace_id: str,
    org_id: str,
    file_data: Dict,
    sync_id: str,
    file_index: int,
) -> None:
    """Send one Slack file attachment to FILE_INGESTION_QUEUE."""
    sqs = _get_sqs_client()
    sqs.send_message(
        QueueUrl=FILE_INGESTION_QUEUE,
        MessageBody=json.dumps({
            "source": SOURCE_FILE,
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "org_id": org_id,
            "file": file_data,
            "sync_id": sync_id,
            "file_index": file_index,
            "total_files": 0,
        }, cls=DecimalEncoder),
    )


def queue_delete_job(org_id: str, workspace_id: str, connection_id: str) -> None:
    sqs = _get_sqs_client()
    sqs.send_message(
        QueueUrl=DOCUMENT_DELETE_QUEUE,
        MessageBody=json.dumps({
            "action": "delete_documents",
            "org_id": org_id,
            "workspace_id": workspace_id,
            "source": "slack",
            "connection_id": connection_id,
        }),
    )
    logger.info(f"[{HANDLER_NAME}] Queued delete job for connection {connection_id}")


# ============================================================================
# Route handlers
# ============================================================================


def handle_authorize(event: Dict, auth_context: Dict) -> Dict:
    query_params = event.get("queryStringParameters") or {}
    workspace_id = query_params.get("workspace_id")

    if not workspace_id:
        return _error(400, "workspace_id is required")

    error = check_workspace_access(auth_context, workspace_id, "workspace_admin")
    if error:
        return error

    org_id = auth_context.get("org_id")
    user_id = auth_context.get("sub")
    actor_email = auth_context.get("email", "")

    state = secrets.token_urlsafe(32)
    payload = {"org_id": org_id, "user_id": user_id, "workspace_id": workspace_id}

    if not _claim_oauth_state(state, payload):
        return _error(500, "Failed to allocate OAuth state")

    api_base = API_BASE_URLS.get(ENV, API_BASE_URLS["dev"])
    redirect_uri = f"{api_base}/slack/callback"

    params = {
        "client_id": _get_client_id(),
        "scope": SLACK_BOT_SCOPES,
        "user_scope": "",
        "redirect_uri": redirect_uri,
        "state": state,
    }
    auth_url = f"https://slack.com/oauth/v2/authorize?{urlencode(params)}"

    create_audit_log(
        org_id=org_id,
        actor_id=user_id,
        actor_email=actor_email,
        action="slack.connect.start",
        resource_type="slack_connection",
        resource_id=workspace_id,
        workspace_id=workspace_id,
    )

    return _response(200, {"auth_url": auth_url})


def handle_callback(event: Dict) -> Dict:
    query_params = event.get("queryStringParameters") or {}
    code = query_params.get("code")
    state = query_params.get("state")
    error = query_params.get("error")

    base_url = BASE_URLS.get(ENV, BASE_URLS["dev"])

    if error:
        logger.warning(f"[{HANDLER_NAME}] OAuth error from provider: {error}")
        return _redirect(f"{base_url}/dashboard?slack_error={error}")

    if not code or not state:
        return _redirect(f"{base_url}/dashboard?slack_error=missing_params")

    state_payload = _consume_oauth_state(state)
    if not state_payload:
        logger.warning(f"[{HANDLER_NAME}] OAuth state invalid or replayed")
        return _redirect(f"{base_url}/dashboard?slack_error=invalid_state")

    org_id = state_payload.get("org_id")
    user_id = state_payload.get("user_id")
    workspace_id = state_payload.get("workspace_id")

    if not org_id or not user_id or not workspace_id:
        return _redirect(f"{base_url}/dashboard?slack_error=invalid_state")

    try:
        api_base = API_BASE_URLS.get(ENV, API_BASE_URLS["dev"])
        redirect_uri = f"{api_base}/slack/callback"

        oauth_response = oauth_v2_access(
            client_id=_get_client_id(),
            client_secret=_get_client_secret(),
            code=code,
            redirect_uri=redirect_uri,
        )
        bot_token = oauth_response.get("access_token")
        if not bot_token:
            logger.error(f"[{HANDLER_NAME}] No bot token returned by Slack")
            create_audit_log(
                org_id=org_id, actor_id=user_id, actor_email="",
                action="slack.connect.failed",
                resource_type="slack_connection", resource_id=workspace_id,
                workspace_id=workspace_id,
                details={"reason": "no_bot_token"},
            )
            return _redirect(f"{base_url}/dashboard?slack_error=no_bot_token")

        team = oauth_response.get("team") or {}
        team_id = team.get("id", "")
        team_name = team.get("name", "")
        bot_user_id = oauth_response.get("bot_user_id", "")

        existing_conns = list_connections(workspace_id)
        if existing_conns:
            existing = existing_conns[0]
            existing_team = existing.get("team_id") or ""
            if existing_team and team_id and team_id != existing_team:
                # Per open-questions.md #4: refuse to silently swap teams.
                logger.warning(
                    f"[{HANDLER_NAME}] Workspace {workspace_id} already has "
                    f"a different Slack team connected ({existing_team}); "
                    f"refusing to swap to {team_id}."
                )
                create_audit_log(
                    org_id=org_id, actor_id=user_id, actor_email="",
                    action="slack.connect.failed",
                    resource_type="slack_connection", resource_id=workspace_id,
                    workspace_id=workspace_id,
                    details={"reason": "different_team_already_connected"},
                )
                return _redirect(
                    f"{base_url}/dashboard?slack_error=different_team_already_connected"
                )

            connection_id = existing["connection_id"]
            save_tokens(connection_id, bot_token)
            table = _get_dynamodb().Table(TENANTS_TABLE)
            table.update_item(
                Key={"PK": f"WS#{workspace_id}", "SK": f"SLACK_CONNECTION#{connection_id}"},
                UpdateExpression=(
                    "SET team_id = :tid, team_name = :tn, "
                    "bot_user_id = :buid, updated_at = :now"
                ),
                ExpressionAttributeValues={
                    ":tid": team_id,
                    ":tn": team_name,
                    ":buid": bot_user_id,
                    ":now": now_iso(),
                },
            )
        else:
            connection_id = generate_connection_id()
            save_connection(
                connection_id=connection_id,
                workspace_id=workspace_id,
                org_id=org_id,
                team_id=team_id,
                team_name=team_name,
                bot_user_id=bot_user_id,
                created_by=user_id,
            )
            save_tokens(connection_id, bot_token)

        # Seed the user cache so initial sync doesn't burn the users.info
        # rate limit. Non-fatal if this fails — sync still works, just
        # backfills the cache via per-message users.info on cache miss.
        if team_id:
            try:
                _populate_user_cache(team_id, bot_token)
            except Exception:
                logger.exception(f"[{HANDLER_NAME}] User-cache seed failed (non-fatal)")

        create_audit_log(
            org_id=org_id, actor_id=user_id, actor_email="",
            action="slack.connect.complete",
            resource_type="slack_connection", resource_id=connection_id,
            workspace_id=workspace_id,
            details={"team_id": team_id, "team_name": team_name},
        )

        return _redirect(
            f"{base_url}/slack/configure?workspace_id={workspace_id}&connection_id={connection_id}"
        )

    except Exception:
        logger.exception(f"[{HANDLER_NAME}] OAuth callback failed")
        create_audit_log(
            org_id=org_id, actor_id=user_id, actor_email="",
            action="slack.connect.failed",
            resource_type="slack_connection", resource_id=workspace_id,
            workspace_id=workspace_id,
            details={"reason": "callback_exception"},
        )
        return _redirect(f"{base_url}/dashboard?slack_error=callback_failed")


def handle_list_connections(event: Dict, auth_context: Dict) -> Dict:
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")

    error = check_workspace_access(auth_context, workspace_id)
    if error:
        return error

    connections = list_connections(workspace_id)
    return _response(200, {
        "connections": [
            {
                "connection_id": conn["connection_id"],
                "team_id": conn.get("team_id", ""),
                "team_name": conn.get("team_name", ""),
                "bot_user_id": conn.get("bot_user_id", ""),
                "config": conn.get("config"),
                "last_sync_at": conn.get("last_sync_at"),
                "last_sync_status": conn.get("last_sync_status"),
                "last_sync_error": conn.get("last_sync_error"),
                "file_count": conn.get("file_count", 0),
                "created_at": conn.get("created_at"),
            }
            for conn in connections
        ]
    })


def handle_get_connection(event: Dict, auth_context: Dict) -> Dict:
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    error = check_workspace_access(auth_context, workspace_id)
    if error:
        return error

    conn = get_connection(workspace_id, connection_id)
    if not conn:
        return _error(404, "Connection not found")

    return _response(200, {
        "connection_id": conn["connection_id"],
        "team_id": conn.get("team_id", ""),
        "team_name": conn.get("team_name", ""),
        "bot_user_id": conn.get("bot_user_id", ""),
        "config": conn.get("config"),
        "last_sync_at": conn.get("last_sync_at"),
        "last_sync_status": conn.get("last_sync_status"),
        "last_sync_error": conn.get("last_sync_error"),
        "file_count": conn.get("file_count", 0),
        "created_at": conn.get("created_at"),
    })


def handle_list_channels(event: Dict, auth_context: Dict) -> Dict:
    """List channels the bot is a member of so the picker can show actually-accessible options."""
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    error = check_workspace_access(auth_context, workspace_id, "workspace_admin")
    if error:
        return error

    conn = get_connection(workspace_id, connection_id)
    if not conn:
        return _error(404, "Connection not found")

    try:
        bot_token = refresh_access_token(connection_id)
        channels = []
        for channel in iterate_member_channels(bot_token):
            channels.append({
                "channel_id": channel.get("id"),
                "name": channel.get("name") or channel.get("name_normalized") or "",
                "is_private": bool(channel.get("is_private", False)),
                "num_members": channel.get("num_members", 0),
            })
        config = conn.get("config") or {}
        selected = set(config.get("include_channels", []))
        return _response(200, {
            "channels": channels,
            "selected_channels": [c["channel_id"] for c in channels if c["channel_id"] in selected],
        })

    except SlackRateLimitedError as e:
        return _response(429, {"error": str(e), "code": "rate_limited"})
    except ValueError as e:
        msg = str(e)
        if "expired" in msg.lower() or "invalid_auth" in msg.lower() or "token_revoked" in msg.lower():
            return _response(401, {"error": msg, "code": "reauth_required"})
        return _error(401, msg)


def handle_save_config(event: Dict, auth_context: Dict) -> Dict:
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    error = check_workspace_access(auth_context, workspace_id, "workspace_admin")
    if error:
        return error

    try:
        body = json.loads(event.get("body", "{}"))
    except json.JSONDecodeError:
        return _error(400, "Invalid JSON body")

    requested = body.get("include_channels", []) or []
    include_channels = [c for c in requested if isinstance(c, str) and c]
    if not include_channels:
        return _error(400, "At least one channel must be selected")

    conn = get_connection(workspace_id, connection_id)
    if not conn:
        return _error(404, "Connection not found")

    org_id = conn.get("org_id")
    team_id = conn.get("team_id", "")

    config = {"include_channels": include_channels, "cursors": {}}
    update_connection_config(workspace_id, connection_id, config)

    update_sync_status(workspace_id, connection_id, "queued")
    queue_sync_job({
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "org_id": org_id,
        "team_id": team_id,
        "include_channels": include_channels,
        "cursors": {},
    })

    create_audit_log(
        org_id=org_id,
        actor_id=auth_context.get("sub"),
        actor_email=auth_context.get("email", ""),
        action="slack.config.update",
        resource_type="slack_connection",
        resource_id=connection_id,
        workspace_id=workspace_id,
        details={"channel_count": len(include_channels)},
    )

    return _response(200, {
        "message": "Configuration saved, sync queued",
        "channel_count": len(include_channels),
    })


def handle_disconnect(event: Dict, auth_context: Dict) -> Dict:
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    error = check_workspace_access(auth_context, workspace_id, "workspace_admin")
    if error:
        return error

    conn = get_connection(workspace_id, connection_id)
    if not conn:
        return _error(404, "Connection not found")

    org_id = conn.get("org_id")
    queue_delete_job(org_id, workspace_id, connection_id)
    delete_tokens(connection_id)
    delete_connection(workspace_id, connection_id)

    create_audit_log(
        org_id=org_id,
        actor_id=auth_context.get("sub"),
        actor_email=auth_context.get("email", ""),
        action="slack.disconnect",
        resource_type="slack_connection",
        resource_id=connection_id,
        workspace_id=workspace_id,
    )

    return _response(200, {
        "message": "Disconnected successfully. Synced messages and files are being deleted in the background.",
    })


def handle_manual_sync(event: Dict, auth_context: Dict) -> Dict:
    path_params = event.get("pathParameters", {})
    workspace_id = path_params.get("id")
    connection_id = path_params.get("connectionId")

    error = check_workspace_access(auth_context, workspace_id, "workspace_admin")
    if error:
        return error

    conn = get_connection(workspace_id, connection_id)
    if not conn:
        return _error(404, "Connection not found")

    config = conn.get("config") or {}
    include_channels = config.get("include_channels", [])
    if not include_channels:
        return _error(400, "No channels selected. Configure first.")

    status = conn.get("last_sync_status")
    if status in ("queued", "in_progress"):
        return _response(200, {"message": "Sync already in progress", "status": status})

    update_sync_status(workspace_id, connection_id, "queued")
    queue_sync_job({
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "org_id": conn.get("org_id"),
        "team_id": conn.get("team_id", ""),
        "include_channels": include_channels,
        "cursors": config.get("cursors", {}),
    })

    create_audit_log(
        org_id=conn.get("org_id"),
        actor_id=auth_context.get("sub"),
        actor_email=auth_context.get("email", ""),
        action="slack.sync.manual",
        resource_type="slack_connection",
        resource_id=connection_id,
        workspace_id=workspace_id,
    )

    return _response(200, {"message": "Sync queued", "status": "queued"})


# ============================================================================
# SQS sync engine
# ============================================================================


def handle_sqs_event(event: Dict) -> Dict:
    for record in event.get("Records", []):
        try:
            body = json.loads(record["body"])
            sync_connection(
                connection_id=body["connection_id"],
                workspace_id=body["workspace_id"],
                org_id=body["org_id"],
                team_id=body.get("team_id", ""),
                include_channels=body["include_channels"],
                cursors=body.get("cursors", {}),
            )
        except Exception:
            logger.exception(f"[{HANDLER_NAME}] Error processing SQS message")
            raise

    return {"statusCode": 200, "body": json.dumps({"message": "Processed"})}


def _enrich_message(
    raw: Dict,
    channel_id: str,
    channel_name: str,
    team_id: str,
    access_token: str,
) -> Dict:
    """Build the enriched payload file-ingestion will render to markdown.

    Resolution happens here (in the integration lambda) so file-ingestion
    never needs to call Slack for user names. file-ingestion only reaches
    out for slack_file binary downloads.
    """
    sender_id = raw.get("user") or raw.get("bot_id") or ""
    sender_name = _resolve_user(team_id, sender_id, access_token) if sender_id else "(unknown)"
    body = _substitute_mentions(raw.get("text") or "", team_id, access_token)
    return {
        "channel_id": channel_id,
        "channel_name": channel_name,
        "ts": raw.get("ts"),
        "thread_ts": raw.get("thread_ts"),
        "sender_id": sender_id,
        "sender_name": sender_name,
        "body": body,
        "permalink": raw.get("permalink"),  # may not be present; renderer tolerates
    }


def sync_connection(
    connection_id: str,
    workspace_id: str,
    org_id: str,
    team_id: str,
    include_channels: List[str],
    cursors: Dict[str, str],
) -> None:
    sync_id = f"sync_{secrets.token_urlsafe(8)}"
    is_full_sync = not cursors

    try:
        update_sync_status(workspace_id, connection_id, "in_progress")
        bot_token = refresh_access_token(connection_id)

        # Resolve channel names for the selected channels (cheap, one
        # conversations.list pagination per sync).
        channel_names: Dict[str, str] = {}
        try:
            for ch in iterate_member_channels(bot_token):
                channel_names[ch.get("id")] = ch.get("name") or ch.get("name_normalized") or ""
        except Exception:
            logger.exception(f"[{HANDLER_NAME}] [{sync_id}] channel name lookup failed; proceeding with channel_ids")

        total_messages = 0
        new_cursors: Dict[str, str] = dict(cursors)
        failed_channels: List[str] = []

        for channel_id in include_channels:
            channel_name = channel_names.get(channel_id, channel_id)
            old_cursor = cursors.get(channel_id)

            try:
                file_index = 0
                latest_ts_seen = old_cursor or ""
                for msg in iterate_channel_messages(bot_token, channel_id, oldest=old_cursor):
                    enriched = _enrich_message(msg, channel_id, channel_name, team_id, bot_token)

                    # Pull thread replies if this message has them.
                    if msg.get("thread_ts") and msg.get("thread_ts") == msg.get("ts") and msg.get("reply_count", 0) > 0:
                        replies = []
                        try:
                            for reply in iterate_thread_replies(bot_token, channel_id, msg["ts"]):
                                replies.append(_enrich_message(reply, channel_id, channel_name, team_id, bot_token))
                        except Exception:
                            logger.warning(
                                f"[{HANDLER_NAME}] [{sync_id}] thread fetch failed for {channel_id}/{msg.get('ts')}",
                                exc_info=True,
                            )
                        enriched["replies"] = replies

                    queue_message_for_ingestion(
                        connection_id=connection_id,
                        workspace_id=workspace_id,
                        org_id=org_id,
                        message=enriched,
                        sync_id=sync_id,
                        file_index=file_index,
                    )
                    file_index += 1
                    total_messages += 1

                    # Files attached to this message: queue separately.
                    for f in (msg.get("files") or []):
                        if not f.get("url_private_download"):
                            continue
                        queue_file_for_ingestion(
                            connection_id=connection_id,
                            workspace_id=workspace_id,
                            org_id=org_id,
                            file_data={
                                "channel_id": channel_id,
                                "file_id": f.get("id"),
                                "filename": f.get("name") or f.get("title") or f.get("id"),
                                "mimetype": f.get("mimetype", ""),
                                "url_private_download": f.get("url_private_download"),
                                "size": f.get("size", 0),
                            },
                            sync_id=sync_id,
                            file_index=file_index,
                        )
                        file_index += 1

                    msg_ts = msg.get("ts") or ""
                    if msg_ts and (not latest_ts_seen or float(msg_ts) > float(latest_ts_seen)):
                        latest_ts_seen = msg_ts

                if latest_ts_seen:
                    new_cursors[channel_id] = latest_ts_seen
                logger.info(
                    f"[{HANDLER_NAME}] [{sync_id}] {channel_name} ({channel_id}): "
                    f"queued {file_index} items"
                )
            except Exception as e:
                failed_channels.append(channel_id)
                logger.warning(
                    f"[{HANDLER_NAME}] [{sync_id}] Failed to sync channel {channel_id}: {e}",
                    exc_info=True,
                )

        if not failed_channels:
            status = "success"
            error = None
        elif len(failed_channels) == len(include_channels):
            status = "failed"
            error = f"All {len(failed_channels)} channels failed: {', '.join(failed_channels)}"
        else:
            status = "partial_success"
            error = f"{len(failed_channels)} of {len(include_channels)} channels failed: {', '.join(failed_channels)}"

        # file_count: SET on full sync, ADD on delta. Mirror Clio + Dropbox.
        if is_full_sync:
            update_sync_status(
                workspace_id, connection_id, status,
                file_count=total_messages,
                cursors=new_cursors,
                error=error,
            )
        else:
            _increment_file_count(workspace_id, connection_id, total_messages)
            update_sync_status(
                workspace_id, connection_id, status,
                cursors=new_cursors,
                error=error,
            )

        logger.info(
            f"[{HANDLER_NAME}] [{sync_id}] Sync {status}: {total_messages} messages queued, "
            f"{len(failed_channels)} channel(s) failed"
        )

    except ValueError as e:
        msg = str(e)
        if "invalid_auth" in msg.lower() or "token_revoked" in msg.lower():
            update_sync_status(workspace_id, connection_id, "reauth_required", error=msg)
        else:
            update_sync_status(workspace_id, connection_id, "auth_error", error=msg)
        logger.error(f"[{HANDLER_NAME}] Auth error during sync: {e}")
        raise

    except Exception as e:
        logger.exception(f"[{HANDLER_NAME}] Sync failed")
        update_sync_status(workspace_id, connection_id, "failed", error=str(e))
        raise


# ============================================================================
# Main handler
# ============================================================================


def handler(event: Dict[str, Any], context: Any) -> Dict:
    """Routes HTTP API and SQS events. Returns 404 for all routes when SLACK_ENABLED != "true"."""

    if "Records" in event and event["Records"][0].get("eventSource") == "aws:sqs":
        if not _is_enabled():
            logger.info(f"[{HANDLER_NAME}] SLACK_ENABLED is false; dropping SQS message")
            return {"statusCode": 200, "body": "{}"}
        return handle_sqs_event(event)

    if not _is_enabled():
        return _error(404, "Not found")

    http_method = event.get("requestContext", {}).get("http", {}).get("method")
    path = event.get("rawPath", "")

    logger.info(f"[{HANDLER_NAME}] {http_method} {path}")

    try:
        if path == "/slack/callback" and http_method == "GET":
            return handle_callback(event)

        if path == "/slack/authorize" and http_method == "GET":
            auth_context, error = require_auth(event)
            if error:
                return error
            return handle_authorize(event, auth_context)

        if path.startswith("/workspace/"):
            auth_context, error = require_auth(event)
            if error:
                return error

            if "/slack/connections" in path and "/slack/connections/" not in path:
                if http_method == "GET":
                    return handle_list_connections(event, auth_context)

            if "/channels" in path and http_method == "GET":
                return handle_list_channels(event, auth_context)

            if "/config" in path and http_method == "PUT":
                return handle_save_config(event, auth_context)

            if "/sync" in path and http_method == "POST":
                return handle_manual_sync(event, auth_context)

            if "/slack/connections/" in path and http_method == "GET" \
                    and "/channels" not in path and "/config" not in path:
                return handle_get_connection(event, auth_context)

            if "/slack/connections/" in path and http_method == "DELETE":
                return handle_disconnect(event, auth_context)

        return _error(404, "Not found")

    except Exception:
        logger.exception(f"[{HANDLER_NAME}] Unhandled error")
        return _error(500, "Internal server error")
