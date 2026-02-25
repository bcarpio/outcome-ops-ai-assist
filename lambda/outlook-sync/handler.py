"""
Outlook Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Outlook emails:
1. Receives sync request from OUTLOOK_SYNC_QUEUE
2. Uses MS Graph delta API for incremental sync per folder
3. Queues individual emails to FILE_INGESTION_QUEUE
4. Queues attachments separately with source="outlook_attachment"
5. Handles SQS continuation for large mailboxes
6. Updates connection sync status and delta links
"""

import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

from outlook_api import (
    OutlookAPIError,
    OutlookAuthError,
    get_messages_delta,
    list_attachments,
)
from token_manager import refresh_access_token

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
OUTLOOK_SYNC_QUEUE = os.environ.get("OUTLOOK_SYNC_QUEUE")
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
                "SK": f"OUTLOOK_CONNECTION#{connection_id}",
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
    email_count: int = 0,
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

    if email_count > 0:
        update_expr += ", email_count = :count"
        expr_values[":count"] = email_count

    if delta_links is not None:
        update_expr += ", delta_links = :delta"
        expr_values[":delta"] = delta_links

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"OUTLOOK_CONNECTION#{connection_id}",
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


def queue_email_ingestion(
    workspace_id: str,
    connection_id: str,
    message: Dict,
    folder_name: str,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue an email for file ingestion."""
    subject = message.get("subject", "No Subject")
    message_id = message.get("id", "")

    msg = {
        "source": "outlook",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "message_id": message_id,
            "subject": subject,
            "folder_name": folder_name,
            "from": message.get("from", {}).get("emailAddress", {}).get("address", ""),
            "received_at": message.get("receivedDateTime", ""),
            "body_content": message.get("body", {}).get("content", ""),
            "body_content_type": message.get("body", {}).get("contentType", "html"),
            "has_attachments": message.get("hasAttachments", False),
        },
        "sync_id": sync_id,
        "file_index": file_index,
        "total_files": total_files,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-email-{hashlib.sha256(message_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue email {message_id}: {e}")


def queue_attachment_ingestion(
    workspace_id: str,
    connection_id: str,
    attachment: Dict,
    message_id: str,
    folder_name: str,
    sync_id: str,
):
    """Queue an email attachment for file ingestion."""
    msg = {
        "source": "outlook_attachment",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "attachment_id": attachment.get("id"),
            "message_id": message_id,
            "folder_name": folder_name,
            "filename": attachment.get("name", "attachment"),
            "content_type": attachment.get("contentType", "application/octet-stream"),
            "size": attachment.get("size", 0),
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-attachment-{hashlib.sha256((attachment.get('id') or '').encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue attachment {attachment.get('id')}: {e}")


def queue_continuation(
    workspace_id: str,
    connection_id: str,
    folder_id: str,
    folder_name: str,
    next_link: str,
    sync_id: str,
    messages_so_far: int,
):
    """Queue a continuation message for large mailboxes."""
    if not OUTLOOK_SYNC_QUEUE:
        logger.warning("OUTLOOK_SYNC_QUEUE not configured for continuation")
        return

    msg = {
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "continuation": True,
        "folder_id": folder_id,
        "folder_name": folder_name,
        "next_link": next_link,
        "sync_id": sync_id,
        "messages_so_far": messages_so_far,
    }

    try:
        sqs_client.send_message(
            QueueUrl=OUTLOOK_SYNC_QUEUE,
            MessageBody=json.dumps(msg),
        )
        logger.info(f"Queued continuation for folder {folder_name} after {messages_so_far} messages")
    except ClientError as e:
        logger.error(f"Failed to queue continuation: {e}")


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """
    Process a sync request for an Outlook connection.

    Uses MS Graph delta API for incremental sync.

    Args:
        message: Sync request with workspace_id, connection_id

    Returns:
        Result dict
    """
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    is_continuation = message.get("continuation", False)
    sync_id = message.get("sync_id") or f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing {'continuation' if is_continuation else 'sync'} request for {workspace_id}/{connection_id} [{sync_id}]")

    # Get connection details
    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    selected_folders = connection.get("selected_folders", [])
    delta_links = connection.get("delta_links", {}) or {}

    if not selected_folders and not is_continuation:
        logger.warning(f"No folders selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_folders_selected")
        return {"status": "skipped", "reason": "No folders selected"}

    # Update status to syncing
    if not is_continuation:
        update_connection_status(workspace_id, connection_id, "syncing")

    # Get OAuth token
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

    total_queued = message.get("messages_so_far", 0)
    attachment_count = 0

    try:
        if is_continuation:
            # Process single folder continuation
            folder_id = message["folder_id"]
            folder_name = message["folder_name"]
            next_link = message["next_link"]

            queued, att_count = _process_folder_page(
                access_token=access_token,
                workspace_id=workspace_id,
                connection_id=connection_id,
                folder_id=folder_id,
                folder_name=folder_name,
                delta_link_or_next=next_link,
                delta_links=delta_links,
                sync_id=sync_id,
                messages_so_far=total_queued,
            )
            total_queued += queued
            attachment_count += att_count
        else:
            # Process all selected folders
            for folder in selected_folders:
                folder_id = folder.get("id")
                folder_name = folder.get("displayName", "Unknown")

                # Use existing delta link for incremental sync
                existing_delta = delta_links.get(folder_id)

                queued, att_count = _process_folder_page(
                    access_token=access_token,
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    folder_id=folder_id,
                    folder_name=folder_name,
                    delta_link_or_next=existing_delta,
                    delta_links=delta_links,
                    sync_id=sync_id,
                    messages_so_far=total_queued,
                )
                total_queued += queued
                attachment_count += att_count

        # Update connection with new delta links and status
        update_connection_status(
            workspace_id, connection_id, "completed",
            email_count=total_queued,
            delta_links=delta_links,
        )

        result = {
            "status": "success",
            "sync_id": sync_id,
            "emails_queued": total_queued,
        }
        if attachment_count:
            result["attachments_queued"] = attachment_count
        return result

    except OutlookAuthError:
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


def _process_folder_page(
    access_token: str,
    workspace_id: str,
    connection_id: str,
    folder_id: str,
    folder_name: str,
    delta_link_or_next: Optional[str],
    delta_links: Dict,
    sync_id: str,
    messages_so_far: int,
) -> tuple:
    """
    Process messages from a folder using delta query.

    Returns:
        Tuple of (messages_queued, attachments_queued)
    """
    queued = 0
    attachment_count = 0

    result = get_messages_delta(
        access_token=access_token,
        folder_id=folder_id,
        delta_link=delta_link_or_next,
    )

    messages = result.get("messages", [])
    logger.info(f"Got {len(messages)} messages from folder {folder_name}")

    for msg in messages:
        # Skip deleted messages (they have @removed)
        if "@removed" in msg:
            continue

        queue_email_ingestion(
            workspace_id=workspace_id,
            connection_id=connection_id,
            message=msg,
            folder_name=folder_name,
            sync_id=sync_id,
            file_index=messages_so_far + queued,
            total_files=0,  # Unknown total with delta
        )
        queued += 1

        # Queue attachments
        if msg.get("hasAttachments"):
            try:
                attachments = list_attachments(access_token, msg["id"])
                for att in attachments:
                    # Skip inline attachments that are just embedded images
                    if att.get("isInline") and att.get("contentType", "").startswith("image/"):
                        continue
                    queue_attachment_ingestion(
                        workspace_id=workspace_id,
                        connection_id=connection_id,
                        attachment=att,
                        message_id=msg["id"],
                        folder_name=folder_name,
                        sync_id=sync_id,
                    )
                    attachment_count += 1
            except OutlookAPIError as e:
                logger.error(f"Error listing attachments for message {msg['id']}: {e}")

    # Handle pagination: if there's a next_link, queue continuation
    if result.get("next_link"):
        if messages_so_far + queued >= MAX_MESSAGES_PER_INVOCATION:
            queue_continuation(
                workspace_id=workspace_id,
                connection_id=connection_id,
                folder_id=folder_id,
                folder_name=folder_name,
                next_link=result["next_link"],
                sync_id=sync_id,
                messages_so_far=messages_so_far + queued,
            )
        else:
            # Continue processing in this invocation
            more_queued, more_att = _process_folder_page(
                access_token=access_token,
                workspace_id=workspace_id,
                connection_id=connection_id,
                folder_id=folder_id,
                folder_name=folder_name,
                delta_link_or_next=result["next_link"],
                delta_links=delta_links,
                sync_id=sync_id,
                messages_so_far=messages_so_far + queued,
            )
            queued += more_queued
            attachment_count += more_att

    # Save delta link for next sync
    if result.get("delta_link"):
        delta_links[folder_id] = result["delta_link"]

    return queued, attachment_count


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for Outlook sync orchestration.

    Receives sync requests and queues individual emails.
    """
    logger.info(f"Outlook sync handler invoked with {len(event.get('Records', []))} records")

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
    logger.info(f"Sync orchestration completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Outlook sync orchestration completed",
            "results": results,
        }),
    }
