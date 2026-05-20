"""
Teams Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Teams channel messages and files:
1. Receives sync request from TEAMS_SYNC_QUEUE
2. Uses MS Graph delta API for incremental sync per channel
3. Queues messages to FILE_INGESTION_QUEUE with source="teams"
4. Queues files to FILE_INGESTION_QUEUE with source="teams_file"
5. Handles SQS continuation for large channels
6. Updates connection sync status and delta links
"""

import hashlib
import html
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_license_fail_open, log_source_sync  # noqa: E402

import boto3
from botocore.exceptions import ClientError

from teams_api import (
    TeamsAPIError,
    TeamsAuthError,
    filter_messages_by_date,
    get_channel_files_folder,
    get_channel_messages,
    get_message_replies,
    get_messages_delta,
    list_folder_files,
)
from token_manager import refresh_access_token

# License layer imports (ADR-019). The layer is attached at deploy time;
# absence is a deploy bug (caught in lambda_handler and surfaces as a
# LICENSE_FAIL_OPEN audit row + raise so SQS retries / DLQs the message).
try:
    from outcomeops_license import (
        check_repo_limit,
        validate_license,
    )
except ImportError:
    # Module symbols left None intentionally - lambda_handler detects this
    # and emits the LICENSE_FAIL_OPEN audit row before raising.
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
TEAMS_SYNC_QUEUE = os.environ.get("TEAMS_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")

# Cached config
_oauth_config = None

# Max messages per invocation before SQS continuation
MAX_MESSAGES_PER_INVOCATION = 500


def _load_oauth_config() -> Dict[str, str]:
    """Load Microsoft OAuth configuration from SSM."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    prefix = f"/{ENV}/{APP_NAME}/oauth/microsoft"
    try:
        client_id = ssm_client.get_parameter(Name=f"{prefix}/client-id")["Parameter"]["Value"]
        client_secret = ssm_client.get_parameter(
            Name=f"{prefix}/client-secret", WithDecryption=True
        )["Parameter"]["Value"]
        _oauth_config = {"client_id": client_id, "client_secret": client_secret}
        return _oauth_config
    except ClientError as e:
        logger.error(f"Failed to load OAuth config: {e}")
        raise


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a connection by ID."""
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"TEAMS_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError as e:
        logger.error(f"Failed to get connection: {e}")
        return None


def update_connection_status(
    workspace_id: str,
    connection_id: str,
    status: str,
    message_count: int = 0,
    file_count: int = 0,
    delta_links: Optional[Dict] = None,
    error: Optional[str] = None,
):
    """Update connection sync status."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    update_expr = "SET last_sync_status = :status, updated_at = :now"
    expr_values = {":status": status, ":now": now}

    if status in ("completed", "error", "auth_error"):
        update_expr += ", last_sync_at = :sync_at"
        expr_values[":sync_at"] = now

    if message_count > 0:
        update_expr += ", message_count = :msg_count"
        expr_values[":msg_count"] = message_count

    if file_count > 0:
        update_expr += ", file_count = :file_count"
        expr_values[":file_count"] = file_count

    if delta_links is not None:
        update_expr += ", delta_links = :delta"
        expr_values[":delta"] = delta_links

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        dual_write.ddb_update_item(
            WORKSPACES_TABLE,
            {
                "PK": f"WS#{workspace_id}",
                "SK": f"TEAMS_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def sanitize_filename(name: str) -> str:
    """Sanitize a string to be safe for use as a filename."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:200]


def build_message_html(message: Dict, replies: List[Dict] = None) -> str:
    """Build HTML content from a Teams message and its replies."""
    sender = message.get("from", {})
    user_info = sender.get("user", {}) if sender else {}
    sender_name = user_info.get("displayName", "Unknown")
    created = message.get("createdDateTime", "")
    body = message.get("body", {})
    body_content = body.get("content", "")
    body_type = body.get("contentType", "html")
    subject = message.get("subject", "")

    if body_type == "text":
        body_html = f"<pre>{html.escape(body_content)}</pre>"
    else:
        body_html = body_content

    parts = [
        '<!DOCTYPE html>',
        '<html>',
        '<head><meta charset="utf-8">',
        f'<title>{html.escape(subject or "Teams Message")}</title>',
        '</head>',
        '<body>',
    ]

    if subject:
        parts.append(f'<h1>{html.escape(subject)}</h1>')

    parts.append('<div class="message">')
    parts.append(f'<p><strong>{html.escape(sender_name)}</strong> - {html.escape(created)}</p>')
    parts.append(f'<div class="message-body">{body_html}</div>')
    parts.append('</div>')

    if replies:
        parts.append('<div class="replies">')
        parts.append('<h2>Replies</h2>')
        for reply in replies:
            reply_sender = reply.get("from", {})
            reply_user = reply_sender.get("user", {}) if reply_sender else {}
            reply_name = reply_user.get("displayName", "Unknown")
            reply_created = reply.get("createdDateTime", "")
            reply_body = reply.get("body", {})
            reply_content = reply_body.get("content", "")
            reply_type = reply_body.get("contentType", "html")

            if reply_type == "text":
                reply_html = f"<pre>{html.escape(reply_content)}</pre>"
            else:
                reply_html = reply_content

            parts.append('<div class="reply">')
            parts.append(f'<p><strong>{html.escape(reply_name)}</strong> - {html.escape(reply_created)}</p>')
            parts.append(f'<div class="reply-body">{reply_html}</div>')
            parts.append('</div>')
        parts.append('</div>')

    parts.append('</body>')
    parts.append('</html>')

    return '\n'.join(parts)


def queue_message_ingestion(
    workspace_id: str,
    connection_id: str,
    message: Dict,
    channel_name: str,
    message_html: str,
    sync_id: str,
    file_index: int,
):
    """Queue a Teams message for file ingestion."""
    message_id = message.get("id", "")

    msg = {
        "source": "teams",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "message_id": message_id,
            "channel_name": channel_name,
            "subject": message.get("subject", ""),
            "body_content": message_html,
            "created_at": message.get("createdDateTime", ""),
        },
        "sync_id": sync_id,
        "file_index": file_index,
        "total_files": 0,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-msg-{hashlib.sha256(message_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue message {message_id}: {e}")


def queue_file_ingestion(
    workspace_id: str,
    connection_id: str,
    file_item: Dict,
    channel_name: str,
    sync_id: str,
):
    """Queue a Teams channel file for file ingestion."""
    item_id = file_item.get("id", "")
    filename = file_item.get("name", "")
    download_url = file_item.get("@microsoft.graph.downloadUrl", "")

    if not download_url:
        logger.warning(f"No download URL for file {filename}")
        return

    msg = {
        "source": "teams_file",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "item_id": item_id,
            "channel_name": channel_name,
            "filename": filename,
            "download_url": download_url,
            "mime_type": file_item.get("file", {}).get("mimeType", "application/octet-stream"),
            "size": file_item.get("size", 0),
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-file-{hashlib.sha256(item_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue file {filename}: {e}")


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """Process a sync request for a Teams connection."""
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = message.get("sync_id") or f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing Teams sync for {workspace_id}/{connection_id} [{sync_id}]")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    config = connection.get("config", {})
    include_channels = config.get("include_channels", [])
    days_back = int(config.get("days_back", 30))
    sync_files = config.get("sync_files", True)
    delta_links = connection.get("delta_links", {}) or {}

    if not include_channels:
        logger.warning(f"No channels configured for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_channels_configured")
        return {"status": "skipped", "reason": "No channels configured"}

    update_connection_status(workspace_id, connection_id, "syncing")

    oauth_config = _load_oauth_config()
    access_token = refresh_access_token(
        connection_id=connection_id,
        client_id=oauth_config["client_id"],
        client_secret=oauth_config["client_secret"],
    )

    if not access_token:
        logger.error(f"Failed to get access token for connection {connection_id}")
        update_connection_status(
            workspace_id, connection_id, "auth_error",
            error="Failed to refresh OAuth token"
        )
        return {"status": "error", "error": "Authentication failed"}

    total_messages = 0
    total_files = 0

    try:
        for channel_info in include_channels:
            team_id = channel_info.get("team_id")
            channel_id = channel_info.get("channel_id")
            channel_name = channel_info.get("channel_name", "Unknown")

            if not team_id or not channel_id:
                continue

            # Per-source repo-limit enforcement (ADR-019). Use the stable
            # team/channel GUIDs (not the display name, which can change).
            # check_repo_limit returns allowed=True for already-tracked
            # channels, so this is a no-op for previously-synced sources.
            repo_project = f"teams:{team_id}/{channel_id}"
            limit_result = check_repo_limit(repo=repo_project)
            if not limit_result.allowed:
                logger.warning(
                    f"[teams-sync] Repo limit reached for {repo_project}: "
                    f"{limit_result.reason} "
                    f"(current={limit_result.current}/{limit_result.limit})"
                )
                continue

            delta_key = f"{team_id}_{channel_id}"
            existing_delta = delta_links.get(delta_key)

            # Sync messages
            if existing_delta:
                # Delta sync
                msg_count, new_delta = _sync_channel_delta(
                    access_token=access_token,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    delta_link=existing_delta,
                    sync_id=sync_id,
                    messages_so_far=total_messages,
                )
                if new_delta:
                    delta_links[delta_key] = new_delta
            else:
                # Full sync with date filter
                msg_count, new_delta = _sync_channel_full(
                    access_token=access_token,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    days_back=days_back,
                    sync_id=sync_id,
                    messages_so_far=total_messages,
                )
                if new_delta:
                    delta_links[delta_key] = new_delta

            total_messages += msg_count

            # Sync files if enabled
            if sync_files:
                file_count = _sync_channel_files(
                    access_token=access_token,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    team_id=team_id,
                    channel_id=channel_id,
                    channel_name=channel_name,
                    sync_id=sync_id,
                )
                total_files += file_count

            # ADR-019: liveness tracking now lives in connect/disconnect
            # handlers via report_repos_snapshot (license layer v2.0.0).

        update_connection_status(
            workspace_id, connection_id, "completed",
            message_count=total_messages,
            file_count=total_files,
            delta_links=delta_links,
        )

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="teams",
            workspace_id=workspace_id,
            target=connection_id,
            trigger=trigger,
            files_synced=total_messages + total_files,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={
                "sync_id": sync_id,
                "messages_queued": total_messages,
                "files_queued": total_files,
            },
        )
        return {
            "status": "success",
            "sync_id": sync_id,
            "messages_queued": total_messages,
            "files_queued": total_files,
        }

    except TeamsAuthError:
        logger.error("Authentication error during sync")
        update_connection_status(
            workspace_id, connection_id, "auth_error",
            error="Authentication failed during sync"
        )
        return {"status": "error", "error": "Authentication failed"}
    except Exception as e:
        logger.error(f"Error during sync: {e}")
        update_connection_status(
            workspace_id, connection_id, "error",
            error=str(e)
        )
        return {"status": "error", "error": str(e)}


def _sync_channel_delta(
    access_token: str,
    workspace_id: str,
    connection_id: str,
    team_id: str,
    channel_id: str,
    channel_name: str,
    delta_link: str,
    sync_id: str,
    messages_so_far: int,
) -> tuple:
    """Sync a channel using delta query. Returns (message_count, new_delta_link)."""
    queued = 0
    new_delta = None
    next_link = delta_link

    while next_link:
        result = get_messages_delta(
            access_token=access_token,
            team_id=team_id,
            channel_id=channel_id,
            delta_link=next_link,
        )

        messages = result.get("messages", [])
        logger.info(f"Delta: got {len(messages)} messages from channel {channel_name}")

        for msg in messages:
            body = msg.get("body", {})
            if not body.get("content"):
                continue

            try:
                replies = get_message_replies(access_token, team_id, channel_id, msg["id"])
            except TeamsAPIError:
                replies = []

            message_html = build_message_html(msg, replies)

            queue_message_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                message=msg,
                channel_name=channel_name,
                message_html=message_html,
                sync_id=sync_id,
                file_index=messages_so_far + queued,
            )
            queued += 1

        next_link = result.get("next_link")
        if result.get("delta_link"):
            new_delta = result["delta_link"]

        if messages_so_far + queued >= MAX_MESSAGES_PER_INVOCATION and next_link:
            logger.info(f"Reached max messages, would need continuation")
            break

    return queued, new_delta


def _sync_channel_full(
    access_token: str,
    workspace_id: str,
    connection_id: str,
    team_id: str,
    channel_id: str,
    channel_name: str,
    days_back: int,
    sync_id: str,
    messages_so_far: int,
) -> tuple:
    """Full sync of a channel with date filter. Returns (message_count, delta_link)."""
    queued = 0
    next_token = None

    # First, establish delta link for future incremental syncs
    delta_result = get_messages_delta(
        access_token=access_token,
        team_id=team_id,
        channel_id=channel_id,
    )
    delta_link = delta_result.get("delta_link")

    # Now fetch messages with pagination
    while True:
        messages, next_token = get_channel_messages(
            access_token=access_token,
            team_id=team_id,
            channel_id=channel_id,
            skip_token=next_token,
        )

        if not messages:
            break

        # Filter by date
        filtered = filter_messages_by_date(messages, days_back)
        logger.info(f"Full sync: got {len(filtered)}/{len(messages)} messages from {channel_name}")

        for msg in filtered:
            body = msg.get("body", {})
            if not body.get("content"):
                continue

            try:
                replies = get_message_replies(access_token, team_id, channel_id, msg["id"])
            except TeamsAPIError:
                replies = []

            message_html = build_message_html(msg, replies)

            queue_message_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                message=msg,
                channel_name=channel_name,
                message_html=message_html,
                sync_id=sync_id,
                file_index=messages_so_far + queued,
            )
            queued += 1

        # If all messages are older than our filter, stop
        if len(filtered) < len(messages):
            break

        if not next_token:
            break

        if messages_so_far + queued >= MAX_MESSAGES_PER_INVOCATION:
            logger.info(f"Reached max messages for full sync")
            break

    return queued, delta_link


def _sync_channel_files(
    access_token: str,
    workspace_id: str,
    connection_id: str,
    team_id: str,
    channel_id: str,
    channel_name: str,
    sync_id: str,
) -> int:
    """Sync files from a channel's SharePoint folder. Returns file count."""
    files_folder = get_channel_files_folder(access_token, team_id, channel_id)
    if not files_folder:
        return 0

    drive_id = files_folder.get("parentReference", {}).get("driveId")
    folder_id = files_folder.get("id")

    if not drive_id or not folder_id:
        return 0

    try:
        files = list_folder_files(access_token, drive_id, folder_id)
    except TeamsAPIError as e:
        logger.error(f"Failed to list files for channel {channel_name}: {e}")
        return 0

    queued = 0
    for file_item in files:
        # Skip folders
        if "folder" in file_item:
            continue

        queue_file_ingestion(
            workspace_id=workspace_id,
            connection_id=connection_id,
            file_item=file_item,
            channel_name=channel_name,
            sync_id=sync_id,
        )
        queued += 1

    logger.info(f"Queued {queued} files from channel {channel_name}")
    return queued


def lambda_handler(event: Dict, context: Any) -> Dict:
    """SQS-triggered handler for Teams sync orchestration."""
    logger.info(f"Teams sync handler invoked with {len(event.get('Records', []))} records")

    # License validation (ADR-019). SQS-driven entry: raise on failure so
    # the message is retried / DLQ'd rather than silently dropped. OAuth
    # token failures elsewhere log "Authentication error during sync"; the
    # "[teams-sync] License validation failed: ..." prefix below keeps the
    # two failure modes distinguishable in CloudWatch.
    if validate_license is None:
        logger.error(
            "[teams-sync] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(os.environ.get("AUDIT_TABLE", ""), "teams-sync")
        raise ImportError("outcomeops_license layer not attached")
    try:
        claims = validate_license()
        logger.info(f"[teams-sync] License validated: {claims.get('sub')}")
    except Exception as e:
        logger.error(f"[teams-sync] License validation failed: {e}")
        raise

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    results = []

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            result = process_sync_request(message)
            results.append(result)

        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON in SQS message: {e}")
            results.append({"status": "error", "error": "Invalid JSON"})
        except Exception as e:
            logger.error(f"Error processing sync request: {e}")
            results.append({"status": "error", "error": str(e)})

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"Teams sync completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Teams sync completed",
            "results": results,
        }),
    }
