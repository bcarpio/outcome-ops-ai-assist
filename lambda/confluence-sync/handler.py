# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Confluence Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Confluence pages:
1. Receives high-level sync request from CONFLUENCE_SYNC_QUEUE
2. Lists all pages in selected spaces
3. Queues individual page messages to FILE_INGESTION_QUEUE
4. Handles orphan detection by comparing current pages to stored pages
5. Updates connection sync status

This is the orchestrator layer - actual page fetching is done by file-ingestion.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/
# at zip root via terraform source_path (see lambda-confluence.tf).
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_source_sync  # noqa: E402

from confluence_api import (
    ConfluenceAPIError,
    ConfluenceAuthError,
    list_page_attachments,
    list_pages_in_space,
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
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")

# Cached config
_oauth_config = None


def _load_oauth_config() -> Dict[str, str]:
    """Load OAuth configuration from SSM."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    prefix = f"/{ENV}/{APP_NAME}/oauth/atlassian"
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
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
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
    page_count: int = 0,
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

    if page_count > 0:
        update_expr += ", page_count = :count"
        expr_values[":count"] = page_count

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
                "SK": f"CONFLUENCE_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def queue_file_ingestion(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    page: Dict,
    space_key: str,
    space_name: str,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue a page for file ingestion."""
    message = {
        "source": "confluence",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "cloud_id": cloud_id,
        "file": {
            "page_id": page.get("id"),
            "title": page.get("title", ""),
            "space_key": space_key,
            "space_name": space_name,
        },
        "sync_id": sync_id,
        "file_index": file_index,
        "total_files": total_files,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(message),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-page-{page.get('id')}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue page {page.get('id')}: {e}")


def queue_attachment_ingestion(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    attachment: Dict,
    page_id: str,
    space_name: str,
    sync_id: str,
):
    """Queue a Confluence page attachment for file ingestion."""
    message = {
        "source": "confluence_attachment",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "cloud_id": cloud_id,
        "file": {
            "attachment_id": attachment.get("id"),
            "page_id": page_id,
            "space_name": space_name,
            "filename": attachment.get("title", "attachment"),
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(message),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-attachment-{attachment.get('id')}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue attachment {attachment.get('id')}: {e}")


def queue_orphan_detection(
    workspace_id: str,
    source: str,
    prefix: str,
    current_ids: List[str],
):
    """Queue orphan detection job to delete worker."""
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured, skipping orphan detection")
        return

    message = {
        "action": "delete_orphans",
        "workspace_id": workspace_id,
        "source": source,
        "prefix": prefix,
        "current_ids": current_ids,
    }

    try:
        sqs_client.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued orphan detection for {workspace_id}/{source}/{prefix}")
    except ClientError as e:
        logger.error(f"Failed to queue orphan detection: {e}")


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """
    Process a sync request for a Confluence connection.

    Fetches all pages from selected spaces and queues them for ingestion.

    Args:
        message: Sync request with workspace_id, connection_id

    Returns:
        Result dict
    """
    sync_start = time.time()
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    trigger = message.get("trigger", "scheduled")
    sync_id = f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing sync request for {workspace_id}/{connection_id} [{sync_id}]")

    # Get connection details
    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    cloud_id = connection.get("cloud_id")
    selected_spaces = connection.get("selected_spaces", [])
    include_attachments = connection.get("include_attachments", False)

    if not selected_spaces:
        logger.warning(f"No spaces selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_spaces_selected")
        return {"status": "skipped", "reason": "No spaces selected"}

    # Determine sync type based on last_sync_at (delta vs full)
    last_sync_at = connection.get("last_sync_at")
    is_delta_sync = last_sync_at is not None
    sync_type = "delta" if is_delta_sync else "full"
    logger.info(f"Starting {sync_type} sync for connection {connection_id}")

    # Update status to syncing
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

    # Collect all pages from selected spaces
    all_pages = []
    all_page_ids = []
    spaces_processed = []
    spaces_blocked: List[str] = []

    try:
        for space in selected_spaces:
            space_id = space.get("id")
            space_key = space.get("key", "")
            space_name = space.get("name", space_key)

            logger.info(f"[{sync_type}] Listing pages in space: {space_name} ({space_key})")

            try:
                pages = list_pages_in_space(access_token, cloud_id, space_id)

                if is_delta_sync:
                    # Filter to pages modified since last sync.
                    # version.createdAt = when current version was created (last modified).
                    all_count = len(pages)
                    pages = [
                        p for p in pages
                        if p.get("version", {}).get("createdAt", "") >= last_sync_at
                    ]
                    logger.info(
                        f"[delta] {len(pages)}/{all_count} pages modified since "
                        f"{last_sync_at} in space {space_key}"
                    )
                else:
                    logger.info(f"[full] Found {len(pages)} pages in space {space_key}")

                for page in pages:
                    all_pages.append({
                        "page": page,
                        "space_key": space_key,
                        "space_name": space_name,
                    })
                    all_page_ids.append(page.get("id"))

                spaces_processed.append(space_name)

            except ConfluenceAPIError as e:
                logger.error(f"Error listing pages in space {space_key}: {e}")
                continue

        if not all_pages:
            logger.warning(f"No pages found in any selected space")
            update_connection_status(workspace_id, connection_id, "completed", page_count=0)
            # Return "success" NOT "completed" -- the SQS envelope at
            # handler.py:544 only matches "success" and would otherwise
            # mark a legitimate empty sync as a batchItemFailure, causing
            # 3 SQS retries + a DLQ landing every scheduler tick.
            return {"status": "success", "pages_queued": 0}

        # Queue all pages for ingestion
        total_files = len(all_pages)
        logger.info(f"Queuing {total_files} pages for ingestion")

        for i, page_info in enumerate(all_pages):
            queue_file_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                cloud_id=cloud_id,
                page=page_info["page"],
                space_key=page_info["space_key"],
                space_name=page_info["space_name"],
                sync_id=sync_id,
                file_index=i,
                total_files=total_files,
            )

        # Queue attachments if enabled
        attachment_count = 0
        if include_attachments:
            for page_info in all_pages:
                page_id = page_info["page"].get("id")
                try:
                    attachments = list_page_attachments(access_token, cloud_id, page_id)
                    for att in attachments:
                        queue_attachment_ingestion(
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            cloud_id=cloud_id,
                            attachment=att,
                            page_id=page_id,
                            space_name=page_info["space_name"],
                            sync_id=sync_id,
                        )
                        attachment_count += 1
                except ConfluenceAPIError as e:
                    logger.error(f"Error listing attachments for page {page_id}: {e}")
            if attachment_count:
                logger.info(f"Queued {attachment_count} attachments for ingestion")

        # Queue orphan detection only on full syncs (delta syncs only see modified pages)
        if not is_delta_sync:
            for space in selected_spaces:
                space_name = space.get("name", space.get("key", ""))
                # Get page IDs for this space
                space_page_ids = [
                    p["page"]["id"]
                    for p in all_pages
                    if p["space_name"] == space_name
                ]
                if space_page_ids:
                    queue_orphan_detection(
                        workspace_id=workspace_id,
                        source="confluence",
                        prefix=space_name + "/",
                        current_ids=space_page_ids,
                    )

        # Update connection status - mark as completed since pages are queued
        # The actual ingestion happens asynchronously via file-ingestion pipeline
        update_connection_status(
            workspace_id, connection_id, "completed",
            page_count=total_files
        )

        result = {
            "status": "success",
            "sync_id": sync_id,
            "sync_type": sync_type,
            "pages_queued": total_files,
            "spaces_processed": spaces_processed,
        }
        if attachment_count:
            result["attachments_queued"] = attachment_count

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="confluence",
            workspace_id=workspace_id,
            target=",".join(spaces_processed),
            trigger=trigger,
            files_synced=total_files,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={
                "sync_id": sync_id,
                "sync_type": sync_type,
                "attachments_queued": attachment_count,
            },
        )
        if spaces_blocked:
            result["spaces_blocked"] = spaces_blocked
        return result

    except ConfluenceAuthError:
        logger.error(f"Authentication error during sync")
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


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for Confluence sync orchestration.

    Receives high-level sync requests and queues individual pages.
    """
    logger.info(f"Confluence sync handler invoked with {len(event.get('Records', []))} records")

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    # partial-batch-failure semantics -- see github-sync
    # for the same conversion + rationale (silent KB data loss under
    # transient upstream failures).
    batch_item_failures: List[Dict[str, str]] = []
    successful = 0
    failed = 0

    for record in event["Records"]:
        try:
            message = json.loads(record["body"])
            result = process_sync_request(message)
            if result.get("status") == "success":
                successful += 1
            else:
                logger.error(
                    f"[confluence-sync] non-success result for {record.get('messageId')}: {result}"
                )
                batch_item_failures.append({"itemIdentifier": record["messageId"]})
                failed += 1

        except Exception as e:
            logger.exception(f"Error processing sync request {record.get('messageId')}: {e}")
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
            failed += 1

    logger.info(
        f"Sync orchestration completed: {successful} successful, {failed} failed, "
        f"{len(batch_item_failures)} re-delivered"
    )

    return {"batchItemFailures": batch_item_failures}
