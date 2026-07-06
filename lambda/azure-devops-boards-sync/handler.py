"""
Azure DevOps Boards Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Azure DevOps work items for one project per
invocation. Direct analog of ``lambda/jira-sync/handler.py``.

Flow (per SQS message):
1. Parse project sync request from SQS body.
2. Refresh the Boards OAuth access token via ``token_manager``.
3. Mark ``AZDO_BOARDS_PROJECT#`` row ``sync_status="in_progress"``.
4. Read the ``last_changed_watermark`` from the project row. On
   initial sync (null) fall back to N days ago per the plan's
   ``INITIAL_SYNC_LOOKBACK_DAYS``.
5. Build + run WIQL to find work items changed since the watermark
   (open items only per plan open-question 2).
6. Batch-fetch work items in 200-ID chunks. For each work item,
   fetch comments and enqueue to ``FILE_INGESTION_QUEUE`` with
   ``source="azure_devops_boards"`` and a structured ``file`` body.
   ``file-ingestion`` serializes to HTML + uploads to S3.
7. Advance the watermark to the max ``System.ChangedDate`` seen
   across all work items.
8. Persist ``sync_status="success"`` + ``last_sync_at=now``.

This is the orchestrator layer -- actual document serialization + S3
put + KMS + embedding trigger all live in file-ingestion.
"""

import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Explicit timeouts so a wedged network call can't hang the Lambda until
# its function timeout (python:S7618).
_BOTO_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3})


# Make shared/ importable; deploy package bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared import dual_write  # noqa: E402
from shared.audit_writer import log_license_fail_open, log_source_sync  # noqa: E402

# ADR-019: license validator + enforcement come from the layer.
try:
    from outcomeops_license import (  # noqa: E402
        check_repo_limit,
        validate_license,
    )
except ImportError:  # pragma: no cover - deploy bug path
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]

from azure_devops_work_items_api import (  # noqa: E402
    AzureDevOpsBoardsAPIError,
    AzureDevOpsBoardsAuthError,
    build_incremental_wiql,
    get_work_item_comments,
    get_work_items,
    run_wiql,
    MAX_WORK_ITEMS_PER_BATCH,
    STANDARD_WORK_ITEM_FIELDS,
)
from shared.oauth.azure_devops_boards import refresh_access_token  # noqa: E402


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")

AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)

# Cap on initial-sync fan-out. WIQL itself caps at 20k results per
# call; this tighter cap keeps the initial ingest bounded even for
# very large projects. Older work items require a customer-initiated
# backfill (documented in the chunk 09 runbook).
INITIAL_SYNC_MAX_IDS = int(os.environ.get("BOARDS_INITIAL_SYNC_MAX_IDS", "5000"))

# Initial-sync watermark lookback -- treat "no watermark" as "N days
# ago" rather than "beginning of time" so the initial sync is bounded
# even before INITIAL_SYNC_MAX_IDS truncates.
INITIAL_SYNC_LOOKBACK_DAYS = int(
    os.environ.get("BOARDS_INITIAL_SYNC_LOOKBACK_DAYS", "90")
)

# AWS clients
dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
sqs_client = boto3.client("sqs", config=_BOTO_CONFIG)
ssm_client = boto3.client("ssm", config=_BOTO_CONFIG)

_client_secret: Optional[str] = None


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _get_client_secret() -> str:
    global _client_secret
    if _client_secret is not None:
        return _client_secret
    if not AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM:
        raise RuntimeError("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured")
    response = ssm_client.get_parameter(
        Name=AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM,
        WithDecryption=True,
    )
    _client_secret = response["Parameter"]["Value"]
    return _client_secret


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# DDB helpers -- project row is the sync-state anchor
# ---------------------------------------------------------------------------


def _get_project(workspace_id: str, slug: str) -> Optional[Dict]:
    """Load the AZDO_BOARDS_PROJECT# row for this sync."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"AZDO_BOARDS_PROJECT#{slug}",
            }
        )
        return response.get("Item")
    except ClientError as e:
        logger.error(f"Failed to get project row {slug}: {e}")
        return None


def _update_project_status(
    workspace_id: str,
    slug: str,
    status: str,
    watermark: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Update the AZDO_BOARDS_PROJECT# row's sync tracking fields."""
    now = _now_iso()
    update_expr_parts = ["sync_status = :status", "updated_at = :now"]
    expr_values = {":status": status, ":now": now}

    if status in ("success", "failed", "auth_error"):
        update_expr_parts.append("last_sync_at = :sync_at")
        expr_values[":sync_at"] = now

    if watermark is not None:
        update_expr_parts.append("last_changed_watermark = :wm")
        expr_values[":wm"] = watermark

    update_expr = "SET " + ", ".join(update_expr_parts)
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
                "SK": f"AZDO_BOARDS_PROJECT#{slug}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update project status for {slug}: {e}")


# ---------------------------------------------------------------------------
# file-ingestion enqueue
# ---------------------------------------------------------------------------


def _fields_for_ingestion(work_item: Dict) -> Dict[str, Any]:
    """Extract the STANDARD_WORK_ITEM_FIELDS values from an ADO
    work-item response into the flat structure file-ingestion consumes.

    ADO returns fields under ``work_item["fields"]`` keyed by the
    full field ID (e.g. ``"System.Title"``). File-ingestion doesn't
    care about the ADO-specific keying; we translate to friendlier
    snake_case field names it can render into HTML.
    """
    f = work_item.get("fields") or {}
    assigned_to = f.get("System.AssignedTo") or {}
    if isinstance(assigned_to, dict):
        assigned_to_name = assigned_to.get("displayName")
    else:
        assigned_to_name = str(assigned_to) if assigned_to else None

    return {
        "id": work_item.get("id"),
        "url": work_item.get("url"),
        "title": f.get("System.Title", ""),
        "work_item_type": f.get("System.WorkItemType"),
        "state": f.get("System.State"),
        "reason": f.get("System.Reason"),
        "assigned_to": assigned_to_name,
        "created_date": f.get("System.CreatedDate"),
        "changed_date": f.get("System.ChangedDate"),
        "tags": f.get("System.Tags"),
        "description": f.get("System.Description"),
        "repro_steps": f.get("Microsoft.VSTS.TCM.ReproSteps"),
        "acceptance_criteria": f.get("Microsoft.VSTS.Common.AcceptanceCriteria"),
        "priority": f.get("Microsoft.VSTS.Common.Priority"),
        "severity": f.get("Microsoft.VSTS.Common.Severity"),
        "area_path": f.get("System.AreaPath"),
        "iteration_path": f.get("System.IterationPath"),
    }


def _queue_work_item_for_ingestion(
    workspace_id: str,
    connection_id: str,
    org: str,
    project: str,
    work_item: Dict,
    comments: List[Dict],
    sync_id: str,
) -> None:
    """Enqueue one work item to FILE_INGESTION_QUEUE.

    ``document_id`` is deterministic (``azdo-boards#{org}/{project}/{id}``)
    so a re-sync updates rather than duplicating. file-ingestion's
    S3 key is derived from ``org/project/{id}.html``.
    """
    if not FILE_INGESTION_QUEUE:
        logger.warning("FILE_INGESTION_QUEUE not configured; dropping work item")
        return

    wi_id = work_item.get("id")
    fields = _fields_for_ingestion(work_item)

    message = {
        "source": "azure_devops_boards",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "file": {
            **fields,
            "org": org,
            "project": project,
            "comments": comments,
        },
        "sync_id": sync_id,
    }

    try:
        sqs_client.send_message(
            QueueUrl=FILE_INGESTION_QUEUE,
            MessageBody=json.dumps(message),
            MessageGroupId=f"{workspace_id}-{connection_id}",
            MessageDeduplicationId=f"{sync_id}-wi-{wi_id}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue work item {wi_id}: {e}")


# ---------------------------------------------------------------------------
# WIQL + fetch orchestration
# ---------------------------------------------------------------------------


def _initial_watermark() -> str:
    """Watermark for a project with no ``last_changed_watermark`` --
    treat "never synced" as "N days ago" rather than epoch so the
    initial ingest is bounded even before ``INITIAL_SYNC_MAX_IDS``.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(days=INITIAL_SYNC_LOOKBACK_DAYS)
    # WIQL parses UTC ISO timestamps; second-precision is enough.
    return cutoff.strftime("%Y-%m-%dT%H:%M:%SZ")


def _batches(iterable: List, size: int):
    """Yield ``size``-length chunks from ``iterable``. Used to respect
    ADO's 200-IDs-per-batch cap on ``get_work_items``.
    """
    for i in range(0, len(iterable), size):
        yield iterable[i:i + size]


def _sync_project(
    access_token: str,
    workspace_id: str,
    connection_id: str,
    slug: str,
    org: str,
    project: str,
    sync_id: str,
) -> Dict[str, Any]:
    """Sync one project's work items. Returns
    ``{"queued": N, "new_watermark": iso}`` on success. Raises on
    unrecoverable errors so SQS retries.
    """
    project_row = _get_project(workspace_id, slug)
    if not project_row:
        raise RuntimeError(f"Project row missing for {workspace_id}/{slug}")

    watermark = project_row.get("last_changed_watermark") or _initial_watermark()
    is_initial = project_row.get("last_changed_watermark") is None

    wiql = build_incremental_wiql(
        project=project,
        changed_since=watermark,
        open_only=True,
    )
    logger.info(
        f"[boards-sync] WIQL for {org}/{project} "
        f"(initial={is_initial}, watermark={watermark})"
    )
    ids = run_wiql(access_token, org, project, wiql)

    if is_initial and len(ids) > INITIAL_SYNC_MAX_IDS:
        logger.warning(
            f"[boards-sync] initial sync truncated: {len(ids)} > "
            f"{INITIAL_SYNC_MAX_IDS}; older work items require operator backfill"
        )
        ids = ids[:INITIAL_SYNC_MAX_IDS]

    if not ids:
        logger.info(f"[boards-sync] no changes for {org}/{project} since {watermark}")
        return {"queued": 0, "new_watermark": None}

    max_changed = watermark
    queued = 0
    for batch in _batches(ids, MAX_WORK_ITEMS_PER_BATCH):
        items = get_work_items(
            access_token, org, batch,
            fields=STANDARD_WORK_ITEM_FIELDS,
        )
        for item in items:
            wi_id = item.get("id")
            try:
                comments = get_work_item_comments(access_token, org, project, wi_id)
            except AzureDevOpsBoardsAPIError as e:
                # Non-fatal: comments API can transiently fail; the work
                # item still ingests without comments in that case.
                logger.warning(f"[boards-sync] comments fetch failed for {wi_id}: {e}")
                comments = []

            _queue_work_item_for_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                org=org,
                project=project,
                work_item=item,
                comments=comments,
                sync_id=sync_id,
            )
            queued += 1

            changed = (item.get("fields") or {}).get("System.ChangedDate")
            if changed and changed > max_changed:
                max_changed = changed

    return {"queued": queued, "new_watermark": max_changed}


# ---------------------------------------------------------------------------
# Message processing
# ---------------------------------------------------------------------------


def process_sync_request(message: Dict) -> Dict[str, Any]:
    sync_start = time.time()
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    slug = message.get("slug")
    org = message.get("org")
    project = message.get("project")
    triggered_by = message.get("triggered_by", "unknown")
    sync_id = message.get("sync_id") or (
        f"boards-sync-{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}"
        f"-{(connection_id or '')[:8]}"
    )

    if not (workspace_id and connection_id and slug and org and project):
        return {"status": "error", "error": "missing required fields"}

    logger.info(
        f"[boards-sync] processing {workspace_id}/{slug} "
        f"(triggered_by={triggered_by}, sync_id={sync_id})"
    )

    _update_project_status(workspace_id, slug, "in_progress")

    # Token refresh -- surface reconnect_required to the UI so admins
    # can prompt the user without digging through logs.
    try:
        client_secret = _get_client_secret()
    except Exception as e:
        logger.exception("Failed to read ADO client secret from SSM")
        _update_project_status(
            workspace_id, slug, "failed",
            error=f"config_error: {e}",
        )
        return {"status": "error", "error": "config_error"}

    try:
        access_token = refresh_access_token(
            connection_id=connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=client_secret,
        )
    except Exception as e:
        logger.exception(f"[boards-sync] token refresh raised for {connection_id}")
        _update_project_status(
            workspace_id, slug, "auth_error",
            error=f"reconnect_required: {e}",
        )
        return {"status": "error", "error": "auth_error"}

    if not access_token:
        _update_project_status(
            workspace_id, slug, "auth_error",
            error="reconnect_required",
        )
        return {"status": "error", "error": "auth_error"}

    try:
        result = _sync_project(
            access_token=access_token,
            workspace_id=workspace_id,
            connection_id=connection_id,
            slug=slug,
            org=org,
            project=project,
            sync_id=sync_id,
        )
    except AzureDevOpsBoardsAuthError as e:
        _update_project_status(
            workspace_id, slug, "auth_error",
            error=f"reconnect_required: {e}",
        )
        raise
    except AzureDevOpsBoardsAPIError as e:
        _update_project_status(workspace_id, slug, "failed", error=str(e))
        raise
    except Exception as e:  # noqa: BLE001
        logger.exception(f"[boards-sync] unhandled sync error for {slug}")
        _update_project_status(workspace_id, slug, "failed", error=str(e))
        raise

    _update_project_status(
        workspace_id, slug, "success",
        watermark=result.get("new_watermark"),
    )

    duration_ms = int((time.time() - sync_start) * 1000)
    try:
        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="azure_devops_boards",
            workspace_id=workspace_id,
            target=slug,
            trigger=triggered_by,
            files_synced=result.get("queued", 0),
            duration_ms=duration_ms,
            extra_details={
                "sync_id": sync_id,
                "org": org,
                "project": project,
                "new_watermark": result.get("new_watermark"),
            },
        )
    except Exception as e:
        logger.warning(f"[boards-sync] audit log failed: {e}")

    return {
        "status": "success",
        "sync_id": sync_id,
        "queued": result.get("queued", 0),
        "new_watermark": result.get("new_watermark"),
    }


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: Dict, context: Any) -> Dict:
    """SQS-triggered handler. Batch size 1 per event source mapping."""
    logger.info(f"boards-sync invoked with {len(event.get('Records', []))} records")

    if validate_license is None:
        logger.error("[boards-sync] license layer not attached (deploy bug)")
        log_license_fail_open(
            os.environ.get("AUDIT_TABLE", ""), "azure-devops-boards-sync",
        )
        raise ImportError("outcomeops_license layer not attached")
    try:
        claims = validate_license()
        logger.info(f"[boards-sync] license validated: {claims.get('sub')}")
    except Exception as e:
        logger.error(f"[boards-sync] license validation failed: {e}")
        raise

    if "Records" not in event:
        return {"statusCode": 400, "body": "no SQS records"}

    results = []
    for record in event["Records"]:
        try:
            body = json.loads(record["body"])
        except json.JSONDecodeError as e:
            logger.error(f"invalid JSON body: {e}")
            results.append({"status": "error", "error": "invalid_json"})
            continue
        try:
            results.append(process_sync_request(body))
        except Exception as e:  # noqa: BLE001 -- log + re-raise for SQS retry
            logger.error(f"sync request raised: {e}")
            raise

    successful = sum(1 for r in results if r.get("status") == "success")
    logger.info(f"boards-sync completed {successful}/{len(results)}")
    return {"statusCode": 200, "body": json.dumps({"processed": len(results)})}
