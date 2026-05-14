"""
OneDrive Sync Lambda for OutcomeOps AI Assist.

Orchestrates incremental OneDrive sync:
1. Receives sync request from ONEDRIVE_SYNC_QUEUE (manual or scheduled)
2. Refreshes the OAuth access token
3. For each selected folder (or the drive root): runs Microsoft Graph
   delta query, stores the new delta_link for next time
4. Queues each changed file to FILE_INGESTION_QUEUE with source="onedrive"
5. Updates connection sync status, file count, and per-folder delta_tokens

Mirrors sharepoint-sync exactly. Differences:
- OneDrive has ONE drive per user (vs SharePoint's multiple sites).
- No "pages" - OneDrive has files only.
- Per-folder scope (selected_folders) vs SharePoint's per-site scope.
"""

import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import log_license_fail_open, log_source_sync  # noqa: E402
from botocore.exceptions import ClientError

from onedrive_api import (
    OneDriveAPIError,
    OneDriveAuthError,
    get_drive_delta,
    get_folder_delta,
    get_item_download_url,
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
ONEDRIVE_SYNC_QUEUE = os.environ.get("ONEDRIVE_SYNC_QUEUE")
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
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
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
    delta_tokens: Optional[Dict] = None,
    known_item_ids: Optional[List[str]] = None,
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

    if delta_tokens is not None:
        update_expr += ", delta_tokens = :delta"
        expr_values[":delta"] = delta_tokens

    if known_item_ids is not None:
        update_expr += ", known_item_ids = :known"
        expr_values[":known"] = known_item_ids

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"ONEDRIVE_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def queue_orphan_cleanup(
    workspace_id: str,
    connection_id: str,
    current_item_ids: List[str],
) -> None:
    """Queue a delete_orphans job to delete-worker.

    delete-worker enumerates S3 keys under
    ``workspaces/{ws}/onedrive/{connection_id}/`` and reads the
    ``onedrive_item_id`` metadata on each object. Anything whose item_id
    is NOT in current_item_ids gets deleted.

    Best-effort: skipped if DELETE_QUEUE isn't configured or
    current_item_ids is empty (defensive - empty set would wipe
    everything for the connection).
    """
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured; skipping OneDrive orphan cleanup")
        return

    if not current_item_ids:
        logger.warning(
            f"[onedrive-sync] Empty current_item_ids for {workspace_id}/{connection_id}; "
            "skipping orphan cleanup (would wipe everything)"
        )
        return

    payload = {
        "action": "delete_orphans",
        "workspace_id": workspace_id,
        "source": "onedrive",
        "prefix": f"{connection_id}/",
        "current_ids": current_item_ids,
    }
    try:
        sqs_client.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(payload),
        )
        logger.info(
            f"[onedrive-sync] Queued orphan cleanup for "
            f"{workspace_id}/onedrive/{connection_id} "
            f"(current={len(current_item_ids)} item_ids)"
        )
    except ClientError as e:
        logger.error(f"Failed to queue OneDrive orphan cleanup: {e}")


def queue_file_ingestion(
    workspace_id: str,
    connection_id: str,
    item: Dict,
    drive_id: str,
    drive_name: str,
    folder_path: str,
    download_url: str,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue a OneDrive file for file ingestion."""
    item_id = item.get("id", "")
    filename = item.get("name", "")
    parent_path = item.get("parentReference", {}).get("path", "")

    msg = {
        "source": "onedrive",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "item_id": item_id,
            "drive_id": drive_id,
            "drive_name": drive_name,
            "folder_path": folder_path,
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


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """Process a sync request for a OneDrive connection."""
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = message.get("sync_id") or f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing OneDrive sync for {workspace_id}/{connection_id} [{sync_id}]")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    selected_folders = connection.get("selected_folders", [])
    delta_tokens = connection.get("delta_tokens", {}) or {}
    drive_id = connection.get("drive_id", "")
    drive_name = connection.get("drive_name", "OneDrive")
    # known_item_ids maintains the full set of item_ids the sync believes
    # are currently live. We update it incrementally each delta run (add
    # on new files, remove on `deleted` items in the delta response), then
    # ship the full set to delete-worker as expected_keys for orphan
    # cleanup. First-run: empty list, orphan cleanup is a no-op.
    known_item_ids = set(connection.get("known_item_ids", []) or [])

    if not selected_folders:
        logger.warning(f"No folders selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_folders_selected")
        return {"status": "skipped", "reason": "No folders selected"}

    if not drive_id:
        logger.error(f"Connection {connection_id} missing drive_id")
        update_connection_status(
            workspace_id, connection_id, "error",
            error="Connection record missing drive_id"
        )
        return {"status": "error", "error": "Missing drive_id"}

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

    # Per-source repo-limit enforcement (ADR-019). Use drive_id as the
    # stable identifier; the same user's OneDrive always has the same
    # drive_id. check_repo_limit returns allowed=True for already-tracked
    # drives, so this is a no-op for previously-synced sources.
    repo_project = f"onedrive:{drive_id}"
    limit_result = check_repo_limit(repo=repo_project)
    if not limit_result.allowed:
        logger.warning(
            f"[onedrive-sync] Repo limit reached for {repo_project}: "
            f"{limit_result.reason} "
            f"(current={limit_result.current}/{limit_result.limit})"
        )
        update_connection_status(
            workspace_id, connection_id, "blocked_license_limit",
            error=limit_result.reason or "Repo limit reached",
        )
        return {"status": "skipped", "reason": "license_limit"}

    total_files = 0

    try:
        for folder in selected_folders:
            folder_id = folder.get("folder_id")
            folder_name = folder.get("folder_name", "")
            folder_path = folder.get("folder_path", f"/{folder_name}")

            if not folder_id:
                continue

            # Use saved delta token (if any) for this folder. Special:
            # "root" means the whole drive - we use get_drive_delta which
            # walks the entire drive vs get_folder_delta scoped to one folder.
            existing_delta = delta_tokens.get(folder_id)

            try:
                if folder_id == "root":
                    items, new_delta = get_drive_delta(
                        access_token=access_token,
                        drive_id=drive_id,
                        delta_token=existing_delta,
                    )
                else:
                    items, new_delta = get_folder_delta(
                        access_token=access_token,
                        drive_id=drive_id,
                        folder_id=folder_id,
                        delta_token=existing_delta,
                    )
            except OneDriveAuthError:
                # Surface as auth error for the whole sync run
                raise
            except OneDriveAPIError as e:
                logger.warning(f"Failed delta sync for folder {folder_name} ({folder_id}): {e}")
                continue

            if new_delta:
                delta_tokens[folder_id] = new_delta

            # Split the delta response into deletions, active files, and
            # folders/other (ignored). Deletions update known_item_ids so the
            # end-of-run delete_orphans cleanup picks them up. Files get
            # queued for ingestion AND added to known_item_ids.
            active_files = []
            for item in items:
                item_id = item.get("id")
                if item.get("deleted") or "@removed" in item:
                    if item_id:
                        known_item_ids.discard(item_id)
                    continue
                if "file" not in item:
                    continue
                if item_id:
                    known_item_ids.add(item_id)
                active_files.append(item)

            logger.info(
                f"Got {len(active_files)} files from folder {folder_name} "
                f"(folder_id={folder_id}, delta={'incremental' if existing_delta else 'full'})"
            )

            for idx, item in enumerate(active_files):
                # Prefer the inline downloadUrl from the delta response;
                # fall back to a separate API call if not present.
                download_url = item.get("@microsoft.graph.downloadUrl")
                if not download_url:
                    download_url = get_item_download_url(access_token, drive_id, item["id"])
                if not download_url:
                    continue

                queue_file_ingestion(
                    workspace_id=workspace_id,
                    connection_id=connection_id,
                    item=item,
                    drive_id=drive_id,
                    drive_name=drive_name,
                    folder_path=folder_path,
                    download_url=download_url,
                    sync_id=sync_id,
                    file_index=total_files + idx,
                    total_files=0,
                )
                total_files += 1

        # ADR-019: liveness tracking now lives in connect/disconnect handlers
        # via report_repos_snapshot (license layer v2.0.0 desired-state model).

        current_item_ids = sorted(known_item_ids)
        update_connection_status(
            workspace_id, connection_id, "completed",
            file_count=total_files,
            delta_tokens=delta_tokens,
            known_item_ids=current_item_ids,
        )

        # Queue orphan cleanup. delete-worker enumerates S3 keys under
        # this connection's prefix, reads onedrive_item_id metadata, and
        # deletes anything not in current_item_ids. Catches files removed
        # from OneDrive (delta reported as deleted -> we discarded their
        # id from known_item_ids above) and any drift from previous syncs.
        queue_orphan_cleanup(workspace_id, connection_id, current_item_ids)

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="onedrive",
            workspace_id=workspace_id,
            target=connection_id,
            trigger=trigger,
            files_synced=total_files,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={
                "sync_id": sync_id,
                "drive_id": drive_id,
                "folder_count": len(selected_folders),
                "known_item_count": len(current_item_ids),
            },
        )

        return {
            "status": "success",
            "sync_id": sync_id,
            "files_queued": total_files,
        }

    except OneDriveAuthError:
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
    """SQS-triggered handler for OneDrive sync orchestration."""
    logger.info(f"OneDrive sync handler invoked with {len(event.get('Records', []))} records")

    # License validation (ADR-019). SQS-driven entry: raise on failure so
    # the message is retried / DLQ'd rather than silently dropped. OAuth
    # token failures elsewhere log "Authentication error during sync"; the
    # "[onedrive-sync] License validation failed: ..." prefix below keeps
    # the two failure modes distinguishable in CloudWatch.
    if validate_license is None:
        logger.error(
            "[onedrive-sync] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(os.environ.get("AUDIT_TABLE", ""), "onedrive-sync")
        raise ImportError("outcomeops_license layer not attached")
    try:
        claims = validate_license()
        logger.info(f"[onedrive-sync] License validated: {claims.get('sub')}")
    except Exception as e:
        logger.error(f"[onedrive-sync] License validation failed: {e}")
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
    logger.info(f"OneDrive sync completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "OneDrive sync completed",
            "results": results,
        }),
    }
