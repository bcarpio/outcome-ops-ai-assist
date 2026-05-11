"""
OneNote Sync Lambda for OutcomeOps AI Assist.

Orchestrates OneNote page sync:
1. Receives sync request from ONENOTE_SYNC_QUEUE (manual or scheduled)
2. Refreshes the OAuth access token
3. For each selected notebook: lists sections > lists pages
4. For each page:
   - Compares lastModifiedDateTime against stored timestamp
   - If new or changed: fetches HTML content + queues to FILE_INGESTION_QUEUE
   - Either way: adds page_id to known_page_ids
5. Updates connection sync status, page count, page_timestamps, known_page_ids
6. Queues delete_orphans to delete-worker so removed pages get cleaned up

Mirrors onedrive-sync. Key differences:
- OneNote has NO delta API. Every sync enumerates everything; change
  detection uses lastModifiedDateTime stored per-page in DDB.
- Content shape is HTML (not files). The page_id is embedded in the S3
  key filename so orphan detection can identify pages without HEADing
  every object.
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
from shared.audit_writer import log_license_fail_open, log_source_sync  # noqa: E402
from botocore.exceptions import ClientError

from onenote_api import (
    OneNoteAPIError,
    OneNoteAuthError,
    get_page_content,
    list_pages,
    list_sections,
)
from token_manager import refresh_access_token

# License layer imports (ADR-019)
try:
    from outcomeops_license import (
        check_repo_limit,
        track_repo_active,
        validate_license,
    )
except ImportError:
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]
    track_repo_active = None  # type: ignore[assignment]

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
ONENOTE_SYNC_QUEUE = os.environ.get("ONENOTE_SYNC_QUEUE")
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


def sanitize_filename(name: str) -> str:
    """Mirror file-ingestion.sanitize_filename exactly. Used to produce
    the page_id form that ends up as the S3 key filename stem, so
    delete-worker's orphan-membership check works with a direct match."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', '_', name)
    name = name.strip('._')
    return name[:200]


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a connection by ID."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"ONENOTE_CONNECTION#{connection_id}",
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
    page_timestamps: Optional[Dict] = None,
    known_page_ids: Optional[List[str]] = None,
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
        update_expr += ", page_count = :page_count"
        expr_values[":page_count"] = page_count

    if page_timestamps is not None:
        update_expr += ", page_timestamps = :ts"
        expr_values[":ts"] = page_timestamps

    if known_page_ids is not None:
        update_expr += ", known_page_ids = :known"
        expr_values[":known"] = known_page_ids

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"ONENOTE_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def queue_page_ingestion(
    workspace_id: str,
    connection_id: str,
    notebook_id: str,
    notebook_name: str,
    section_id: str,
    section_name: str,
    page: Dict,
    html_content: str,
    sync_id: str,
):
    """Queue a OneNote page for file-ingestion processing.

    The page content is included inline in the message (HTML body) so
    file-ingestion can upload it directly to S3 without needing to call
    back to Graph. Graph rate-limits OneNote endpoints aggressively;
    fetching content here and shipping it downstream keeps the rate
    pressure on a single Lambda.
    """
    page_id = page.get("id", "")
    title = page.get("title", "Untitled")
    last_modified = page.get("lastModifiedDateTime", "")

    msg = {
        "source": "onenote",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            "page_id": page_id,
            "title": title,
            "notebook_id": notebook_id,
            "notebook_name": notebook_name,
            "section_id": section_id,
            "section_name": section_name,
            "last_modified": last_modified,
            "content_html": html_content,
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(msg),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-page-{hashlib.sha256(page_id.encode()).hexdigest()[:32]}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue page {title} ({page_id}): {e}")


def queue_orphan_cleanup(
    workspace_id: str,
    connection_id: str,
    current_page_ids: List[str],
) -> None:
    """Queue a delete_orphans job to delete-worker.

    delete-worker enumerates S3 keys under
    ``workspaces/{ws}/onenote/{connection_id}/`` and extracts the
    page_id from each key (embedded as the filename). Anything whose
    page_id is NOT in current_page_ids gets deleted.

    Best-effort: skipped if DELETE_QUEUE isn't configured or
    current_page_ids is empty (defensive - empty set would wipe
    everything for the connection).
    """
    if not DELETE_QUEUE:
        logger.warning("DELETE_QUEUE not configured; skipping OneNote orphan cleanup")
        return

    if not current_page_ids:
        logger.warning(
            f"[onenote-sync] Empty current_page_ids for {workspace_id}/{connection_id}; "
            "skipping orphan cleanup (would wipe everything)"
        )
        return

    payload = {
        "action": "delete_orphans",
        "workspace_id": workspace_id,
        "source": "onenote",
        "prefix": f"{connection_id}/",
        "current_ids": current_page_ids,
    }
    try:
        sqs_client.send_message(
            QueueUrl=DELETE_QUEUE,
            MessageBody=json.dumps(payload),
        )
        logger.info(
            f"[onenote-sync] Queued orphan cleanup for "
            f"{workspace_id}/onenote/{connection_id} "
            f"(current={len(current_page_ids)} page_ids)"
        )
    except ClientError as e:
        logger.error(f"Failed to queue OneNote orphan cleanup: {e}")


def process_sync_request(message: Dict) -> Dict[str, Any]:
    """Process a sync request for a OneNote connection."""
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = message.get("sync_id") or f"sync_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{connection_id[:8]}"

    logger.info(f"Processing OneNote sync for {workspace_id}/{connection_id} [{sync_id}]")

    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    selected_notebooks = connection.get("selected_notebooks", [])
    page_timestamps = dict(connection.get("page_timestamps", {}) or {})
    user_id = connection.get("user_id", "")

    if not selected_notebooks:
        logger.warning(f"No notebooks selected for connection {connection_id}")
        update_connection_status(workspace_id, connection_id, "no_notebooks_selected")
        return {"status": "skipped", "reason": "No notebooks selected"}

    if not user_id:
        logger.error(f"Connection {connection_id} missing user_id")
        update_connection_status(
            workspace_id, connection_id, "error",
            error="Connection record missing user_id"
        )
        return {"status": "error", "error": "Missing user_id"}

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

    # Per-source repo-limit enforcement (ADR-019). Use the Graph user_id
    # as the stable identifier; OneNote scope is at /me, so one user =
    # one licensable "repo" regardless of which notebooks they pick.
    repo_project = f"onenote:{user_id}"
    limit_result = check_repo_limit(repo=repo_project)
    if not limit_result.allowed:
        logger.warning(
            f"[onenote-sync] Repo limit reached for {repo_project}: "
            f"{limit_result.reason} "
            f"(current={limit_result.current}/{limit_result.limit})"
        )
        update_connection_status(
            workspace_id, connection_id, "blocked_license_limit",
            error=limit_result.reason or "Repo limit reached",
        )
        return {"status": "skipped", "reason": "license_limit"}

    pages_queued = 0
    pages_unchanged = 0
    current_page_ids: List[str] = []
    new_page_timestamps: Dict[str, str] = {}

    try:
        for nb in selected_notebooks:
            notebook_id = nb.get("notebook_id")
            notebook_name = nb.get("notebook_name", "")
            if not notebook_id:
                continue

            try:
                sections = list_sections(access_token, notebook_id)
            except OneNoteAuthError:
                raise
            except OneNoteAPIError as e:
                logger.warning(f"Failed to list sections for notebook {notebook_name}: {e}")
                continue

            for section in sections:
                section_id = section.get("id")
                section_name = section.get("displayName", "")
                if not section_id:
                    continue

                try:
                    pages = list_pages(
                        access_token,
                        section_id,
                        select="id,title,lastModifiedDateTime",
                    )
                except OneNoteAPIError as e:
                    logger.warning(f"Failed to list pages in section {section_name}: {e}")
                    continue

                logger.info(
                    f"Section {notebook_name}/{section_name}: {len(pages)} pages"
                )

                for page in pages:
                    page_id = page.get("id")
                    if not page_id:
                        continue
                    last_modified = page.get("lastModifiedDateTime", "")

                    # Sanitize the page_id to the same form file-ingestion
                    # uses when building the S3 key filename, so the orphan
                    # check is a direct string match.
                    safe_page_id = sanitize_filename(page_id)
                    current_page_ids.append(safe_page_id)
                    new_page_timestamps[page_id] = last_modified

                    # Change-detection: only fetch content + queue if
                    # the page is new or its lastModifiedDateTime has
                    # advanced beyond what we processed last sync.
                    old_ts = page_timestamps.get(page_id)
                    if old_ts == last_modified:
                        pages_unchanged += 1
                        continue

                    try:
                        html_content = get_page_content(access_token, page_id)
                    except OneNoteAuthError:
                        raise
                    except OneNoteAPIError as e:
                        logger.warning(
                            f"Failed to fetch content for page {page.get('title')} "
                            f"({page_id}): {e}"
                        )
                        continue

                    if not html_content or len(html_content.strip()) < 10:
                        logger.debug(f"Skipping empty page {page_id}")
                        continue

                    queue_page_ingestion(
                        workspace_id=workspace_id,
                        connection_id=connection_id,
                        notebook_id=notebook_id,
                        notebook_name=notebook_name,
                        section_id=section_id,
                        section_name=section_name,
                        page=page,
                        html_content=html_content,
                        sync_id=sync_id,
                    )
                    pages_queued += 1

        # Per-source usage tracking (ADR-019). Non-blocking.
        track_repo_active(repo=repo_project)

        update_connection_status(
            workspace_id, connection_id, "completed",
            page_count=len(current_page_ids),
            page_timestamps=new_page_timestamps,
            known_page_ids=current_page_ids,
        )

        # Queue orphan cleanup. delete-worker reads page_id from each
        # S3 key's filename and deletes anything not in current_page_ids.
        queue_orphan_cleanup(workspace_id, connection_id, current_page_ids)

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="onenote",
            workspace_id=workspace_id,
            target=connection_id,
            trigger=trigger,
            files_synced=pages_queued,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={
                "sync_id": sync_id,
                "user_id": user_id,
                "notebook_count": len(selected_notebooks),
                "pages_unchanged": pages_unchanged,
                "total_pages": len(current_page_ids),
            },
        )

        return {
            "status": "success",
            "sync_id": sync_id,
            "pages_queued": pages_queued,
            "pages_unchanged": pages_unchanged,
            "total_pages": len(current_page_ids),
        }

    except OneNoteAuthError:
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
    """SQS-triggered handler for OneNote sync orchestration."""
    logger.info(f"OneNote sync handler invoked with {len(event.get('Records', []))} records")

    # License validation (ADR-019). SQS-driven: raise on failure -> retry / DLQ.
    if validate_license is None:
        logger.error(
            "[onenote-sync] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(os.environ.get("AUDIT_TABLE", ""), "onenote-sync")
        raise ImportError("outcomeops_license layer not attached")
    try:
        claims = validate_license()
        logger.info(f"[onenote-sync] License validated: {claims.get('sub')}")
    except Exception as e:
        logger.error(f"[onenote-sync] License validation failed: {e}")
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
    logger.info(f"OneNote sync completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "OneNote sync completed",
            "results": results,
        }),
    }
