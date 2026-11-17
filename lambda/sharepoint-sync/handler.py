# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
SharePoint Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of SharePoint files and pages:
1. Receives sync request from SHAREPOINT_SYNC_QUEUE
2. Uses drive delta API for incremental file sync
3. Queues files to FILE_INGESTION_QUEUE with source="sharepoint"
4. Queues pages to FILE_INGESTION_QUEUE with source="sharepoint_page"
5. Updates connection sync status and delta tokens
"""

import hashlib
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_source_sync  # noqa: E402
from botocore.exceptions import ClientError

from sharepoint_api import (
    SharePointAPIError,
    SharePointAuthError,
    extract_page_text,
    get_drive_delta,
    get_item_download_url,
    list_site_pages,
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
SHAREPOINT_SYNC_QUEUE = os.environ.get("SHAREPOINT_SYNC_QUEUE")
DELETE_QUEUE = os.environ.get("DELETE_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs_client = boto3.client("sqs")
ssm_client = boto3.client("ssm")

# Cached config
_oauth_config = None


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
                "SK": f"SHAREPOINT_CONNECTION#{connection_id}",
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
    file_count: int = 0,
    page_count: int = 0,
    delta_tokens: Optional[Dict] = None,
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

    if file_count > 0:
        update_expr += ", file_count = :file_count"
        expr_values[":file_count"] = file_count

    if page_count > 0:
        update_expr += ", page_count = :page_count"
        expr_values[":page_count"] = page_count

    if delta_tokens is not None:
        update_expr += ", delta_tokens = :delta"
        expr_values[":delta"] = delta_tokens

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
                "SK": f"SHAREPOINT_CONNECTION#{connection_id}",
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


def queue_file_ingestion(
    workspace_id: str,
    connection_id: str,
    item: Dict,
    site_name: str,
    drive_name: str,
    download_url: str,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue a SharePoint file for file ingestion."""
    item_id = item.get("id", "")
    filename = item.get("name", "")
    parent_path = item.get("parentReference", {}).get("path", "")

    msg = {
        "source": "sharepoint",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "item_id": item_id,
            "site_name": site_name,
            "drive_name": drive_name,
            "filename": filename,
            "path": parent_path,
            "download_url": download_url,
            "mime_type": item.get("file", {}).get("mimeType", "application/octet-stream"),
            "size": item.get("size", 0),
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
            MessageDeduplicationId=f"{sync_id}-file-{hashlib.sha256(item_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue file {filename}: {e}")


def queue_page_ingestion(
    workspace_id: str,
    connection_id: str,
    page: Dict,
    site_name: str,
    page_text: str,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue a SharePoint page for file ingestion."""
    page_id = page.get("id", "")
    title = page.get("title", "Untitled")

    msg = {
        "source": "sharepoint_page",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "page_id": page_id,
            "site_name": site_name,
            "title": title,
            "web_url": page.get("webUrl", ""),
            "content": page_text,
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
            MessageDeduplicationId=f"{sync_id}-page-{hashlib.sha256(page_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue page {title}: {e}")


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """Process a sync request for a SharePoint connection."""
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = message.get("sync_id") or f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing SharePoint sync for {workspace_id}/{connection_id} [{sync_id}]")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    selected_sites = connection.get("selected_sites", [])
    include_pages = connection.get("include_pages", True)
    delta_tokens = connection.get("delta_tokens", {}) or {}

    if not selected_sites:
        logger.warning(f"No sites selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_sites_selected")
        return {"status": "skipped", "reason": "No sites selected"}

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

    total_files = 0
    total_pages = 0
    last_sync_at = connection.get("last_sync_at")

    try:
        for site_info in selected_sites:
            site_id = site_info.get("site_id")
            site_name = site_info.get("site_name", "Unknown")
            drives = site_info.get("drives", [])

            if not site_id:
                continue

            # Sync files from each drive
            for drive_info in drives:
                drive_id = drive_info.get("id")
                drive_name = drive_info.get("name", "Documents")

                if not drive_id:
                    continue

                existing_delta = delta_tokens.get(drive_id)

                items, new_delta = get_drive_delta(
                    access_token=access_token,
                    drive_id=drive_id,
                    delta_token=existing_delta,
                )

                if new_delta:
                    delta_tokens[drive_id] = new_delta

                # Separate deleted from active items
                active_items = []
                for item in items:
                    if item.get("deleted"):
                        continue
                    if "@removed" in item:
                        continue
                    # Only process files (not folders)
                    if "file" not in item:
                        continue
                    active_items.append(item)

                logger.info(f"Got {len(active_items)} files from drive {drive_name} in site {site_name}")

                for idx, item in enumerate(active_items):
                    # Get download URL
                    download_url = item.get("@microsoft.graph.downloadUrl")
                    if not download_url:
                        download_url = get_item_download_url(access_token, drive_id, item["id"])
                    if not download_url:
                        continue

                    queue_file_ingestion(
                        workspace_id=workspace_id,
                        connection_id=connection_id,
                        item=item,
                        site_name=site_name,
                        drive_name=drive_name,
                        download_url=download_url,
                        sync_id=sync_id,
                        file_index=total_files + idx,
                        total_files=0,
                    )
                    total_files += 1

            # Sync pages if enabled
            if include_pages:
                try:
                    pages = list_site_pages(access_token, site_id)

                    # Filter by last_sync_at for incremental page sync
                    if last_sync_at:
                        filtered_pages = []
                        for page in pages:
                            modified = page.get("lastModifiedDateTime", "")
                            if modified:
                                try:
                                    mod_date = datetime.fromisoformat(modified.replace("Z", "+00:00"))
                                    sync_date = datetime.fromisoformat(last_sync_at.replace("Z", "+00:00"))
                                    if mod_date > sync_date:
                                        filtered_pages.append(page)
                                except ValueError:
                                    filtered_pages.append(page)
                            else:
                                filtered_pages.append(page)
                        pages = filtered_pages

                    logger.info(f"Got {len(pages)} pages from site {site_name}")

                    for idx, page in enumerate(pages):
                        page_text = extract_page_text(page)
                        if not page_text or len(page_text.strip()) < 10:
                            continue

                        queue_page_ingestion(
                            workspace_id=workspace_id,
                            connection_id=connection_id,
                            page=page,
                            site_name=site_name,
                            page_text=page_text,
                            sync_id=sync_id,
                            file_index=total_pages + idx,
                            total_files=0,
                        )
                        total_pages += 1

                except SharePointAPIError as e:
                    logger.warning(f"Failed to sync pages for site {site_name}: {e}")

        update_connection_status(
            workspace_id, connection_id, "completed",
            file_count=total_files,
            page_count=total_pages,
            delta_tokens=delta_tokens,
        )

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="sharepoint",
            workspace_id=workspace_id,
            target=connection_id,
            trigger=trigger,
            files_synced=total_files + total_pages,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={
                "sync_id": sync_id,
                "files_queued": total_files,
                "pages_queued": total_pages,
            },
        )
        return {
            "status": "success",
            "sync_id": sync_id,
            "files_queued": total_files,
            "pages_queued": total_pages,
        }

    except SharePointAuthError:
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


def lambda_handler(event: Dict, context: Any) -> Dict:
    """SQS-triggered handler for SharePoint sync orchestration."""
    logger.info(f"SharePoint sync handler invoked with {len(event.get('Records', []))} records")

    if "Records" not in event:
        logger.error("No SQS records in event")
        return {"statusCode": 400, "body": "Invalid event"}

    # Chunk 02: partial-batch-failure semantics -- see github-sync
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
                    f"[sharepoint-sync] non-success result for {record.get('messageId')}: {result}"
                )
                batch_item_failures.append({"itemIdentifier": record["messageId"]})
                failed += 1

        except Exception as e:
            logger.exception(f"Error processing sync request {record.get('messageId')}: {e}")
            batch_item_failures.append({"itemIdentifier": record["messageId"]})
            failed += 1

    logger.info(
        f"SharePoint sync completed: {successful} successful, {failed} failed, "
        f"{len(batch_item_failures)} re-delivered"
    )

    return {"batchItemFailures": batch_item_failures}
