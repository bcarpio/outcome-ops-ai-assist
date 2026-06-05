"""
Azure DevOps sync worker.

SQS-triggered. Each message describes one ADO repo to sync:

    {
      "workspace_id": "...",
      "connection_id": "...",
      "slug": "org/project/repo",
      "org": "...",
      "project": "...",
      "repo": "...",
      "triggered_by": "manual" | "scheduler" | "connect",
      "sync_id": "uuid"
    }

For each message the worker:

1. Resolves a fresh access token (token_manager.refresh_access_token).
2. Marks the AZDO_REPO# row sync_status=in_progress.
3. Asks ADO for the default branch + the branch's HEAD commit SHA.
4. **Skip-unchanged short-circuit.** If the SHA matches the stored
   last_commit_sha AND triggered_by != "manual", mark sync_status=
   success without enqueuing downstream work. ADR-004 idempotency:
   the scheduler retriggering the same repo every hour stays cheap.
5. If changed (or manual): get the repo tree, then enqueue downstream
   work to file-ingestion / repo-summaries based on repo_type.
6. Update last_commit_sha + sync_status=success.

On failure: mark sync_status=failed with last_sync_error populated,
then raise so SQS retries per the redrive policy (DLQ after 3 attempts).

A note on downstream provider-aware routing
-------------------------------------------
The chunk-05 plan's recommended option (b) is that downstream consumers
(file-ingestion, process-repo-summary) become provider-aware: they
read ``provider`` + ``connection_id`` from the message and fetch
content via the right API. That refactor touches existing GitHub
prod paths and is tracked separately as chunk 05a.

This MVP enqueues *the right shape* of downstream message
(carrying provider="azure_devops", org, project, repo,
connection_id, path) but the consumers don't yet act on it.
Sync_status=success here means "ADO side did its job"; the
downstream KB ingest is still gated on chunk 05a landing.
ADR-036's "live verification" section calls this out explicitly so a
buyer reading the design doc understands the wiring.
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# shared/ is bundled into the deploy zip at the package root by
# terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # noqa: E402

from azure_devops_api import (  # noqa: E402
    AzureDevOpsAPIError,
    AzureDevOpsAuthError,
    AzureDevOpsNotFoundError,
    get_default_branch,
    get_latest_commit_sha,
    get_tree_recursive,
)
from token_manager import refresh_access_token  # noqa: E402


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
FILE_INGESTION_QUEUE = os.environ.get("FILE_INGESTION_QUEUE")
REPO_SUMMARIES_QUEUE = os.environ.get("REPO_SUMMARIES_QUEUE")

AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)


_dynamodb = None
_sqs = None
_ssm = None
_client_secret: Optional[str] = None


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


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _get_client_secret() -> str:
    """Cache-once read of the Entra ID app client secret from SSM.
    Same shape as the integration Lambda's helper -- the operator owns
    the parameter VALUE; terraform only knows the parameter NAME."""
    global _client_secret
    if _client_secret is not None:
        return _client_secret
    if not AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM:
        raise RuntimeError("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured")
    response = _get_ssm().get_parameter(
        Name=AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM, WithDecryption=True,
    )
    _client_secret = response["Parameter"]["Value"]
    return _client_secret


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Repo row helpers
# ---------------------------------------------------------------------------


def _get_repo_row(workspace_id: str, slug: str) -> Optional[Dict[str, Any]]:
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    response = table.get_item(
        Key={"PK": f"WS#{workspace_id}", "SK": f"AZDO_REPO#{slug}"},
    )
    return response.get("Item")


def _update_sync_status(
    workspace_id: str,
    slug: str,
    *,
    status: str,
    error: Optional[str] = None,
    commit_sha: Optional[str] = None,
) -> None:
    """Write the new sync_status. ``success`` and ``failed`` are
    terminal -- both update last_sync_at. ``in_progress`` updates
    updated_at only so the staleness check upstream can detect a
    stuck sync."""
    now = _now_iso()
    set_parts = ["last_sync_status = :status", "updated_at = :now"]
    remove_parts: List[str] = []
    expr_values: Dict[str, Any] = {":status": status, ":now": now}

    if status in ("success", "failed", "skip-unchanged"):
        set_parts.append("last_sync_at = :sync_at")
        expr_values[":sync_at"] = now

    if error is not None:
        set_parts.append("last_sync_error = :error")
        expr_values[":error"] = error
    elif status in ("success", "skip-unchanged"):
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
            {"PK": f"WS#{workspace_id}", "SK": f"AZDO_REPO#{slug}"},
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
            ConditionExpression="attribute_exists(SK)",
        )
    except ClientError as e:
        # Row deleted between scheduler enqueue and sync run -- benign;
        # don't crash the worker on it.
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.warning(
                "Skipping sync status update for deleted repo %s/%s",
                workspace_id, slug,
            )
            return
        raise


# ---------------------------------------------------------------------------
# Downstream enqueue (extension point for chunk 05a)
# ---------------------------------------------------------------------------


def _queue_downstream_for_application_repo(
    workspace_id: str, slug: str, org: str, project: str, repo: str,
    connection_id: str, branch: str, tree: List[Dict[str, Any]],
) -> int:
    """Enqueue one repo-summaries job carrying the tree + provider
    metadata. The chunk-05a consumer in process-repo-summary becomes
    provider-aware to read ``provider`` + ``connection_id`` and fetch
    file content via azure_devops_api at process time.

    Returns the number of messages enqueued.
    """
    if not REPO_SUMMARIES_QUEUE:
        logger.warning("REPO_SUMMARIES_QUEUE not configured -- skipping enqueue")
        return 0
    body = {
        "provider": "azure_devops",
        "workspace_id": workspace_id,
        "slug": slug,
        "org": org,
        "project": project,
        "repo": repo,
        "connection_id": connection_id,
        "branch": branch,
        "file_count": sum(1 for item in tree if item.get("gitObjectType") == "blob"),
    }
    _get_sqs().send_message(
        QueueUrl=REPO_SUMMARIES_QUEUE, MessageBody=json.dumps(body),
    )
    return 1


def _queue_downstream_for_standards_repo(
    workspace_id: str, slug: str, org: str, project: str, repo: str,
    connection_id: str, branch: str, tree: List[Dict[str, Any]],
) -> int:
    """Enqueue one file-ingestion job per blob. Same downstream-aware
    contract as the application path: the message carries
    ``provider="azure_devops"`` so process time can route to the
    right fetcher.
    """
    if not FILE_INGESTION_QUEUE:
        logger.warning("FILE_INGESTION_QUEUE not configured -- skipping enqueue")
        return 0
    queued = 0
    for item in tree:
        if item.get("gitObjectType") != "blob":
            continue
        path = item.get("path")
        if not path:
            continue
        body = {
            "provider": "azure_devops",
            "workspace_id": workspace_id,
            "slug": slug,
            "org": org,
            "project": project,
            "repo": repo,
            "connection_id": connection_id,
            "branch": branch,
            "path": path,
        }
        _get_sqs().send_message(
            QueueUrl=FILE_INGESTION_QUEUE, MessageBody=json.dumps(body),
        )
        queued += 1
    return queued


# ---------------------------------------------------------------------------
# Per-message sync
# ---------------------------------------------------------------------------


def process_sync_message(message: Dict[str, Any]) -> Dict[str, Any]:
    """Sync one repo. Returns a result dict for observability + tests."""
    workspace_id = message["workspace_id"]
    connection_id = message["connection_id"]
    slug = message["slug"]
    org = message["org"]
    project = message["project"]
    repo = message["repo"]
    triggered_by = message.get("triggered_by", "scheduler")
    sync_id = message.get("sync_id") or str(uuid.uuid4())

    log_extra = {
        "workspace_id": workspace_id, "slug": slug, "sync_id": sync_id,
    }
    logger.info("ADO sync start", extra=log_extra)

    repo_row = _get_repo_row(workspace_id, slug)
    if repo_row is None:
        # Row was deleted between scheduler enqueue and our pickup.
        logger.warning("ADO sync skip -- repo row missing: %s/%s", workspace_id, slug)
        return {"slug": slug, "status": "skip-missing-row", "sync_id": sync_id}
    if not repo_row.get("enabled", True):
        logger.info("ADO sync skip -- repo disabled: %s/%s", workspace_id, slug)
        return {"slug": slug, "status": "skip-disabled", "sync_id": sync_id}

    _update_sync_status(workspace_id, slug, status="in_progress")

    try:
        access_token = refresh_access_token(
            connection_id=connection_id,
            tenant_id=AZURE_DEVOPS_TENANT_ID,
            client_id=AZURE_DEVOPS_CLIENT_ID,
            client_secret=_get_client_secret(),
        )
        if not access_token:
            raise RuntimeError("reconnect_required")

        default_branch = get_default_branch(access_token, org, project, repo)
        if not default_branch:
            raise RuntimeError("default_branch_missing")

        latest_sha = get_latest_commit_sha(
            access_token, org, project, repo, default_branch,
        )
        if not latest_sha:
            raise RuntimeError("no_commits_on_default_branch")

        stored_sha = repo_row.get("last_commit_sha")
        if stored_sha == latest_sha and triggered_by != "manual":
            # Skip-unchanged. The hourly scheduler retriggering doesn't
            # need to re-do downstream work.
            _update_sync_status(
                workspace_id, slug,
                status="skip-unchanged",
                commit_sha=latest_sha,
            )
            logger.info(
                "ADO sync skip-unchanged: %s/%s @ %s",
                workspace_id, slug, latest_sha,
            )
            return {
                "slug": slug,
                "status": "skip-unchanged",
                "commit_sha": latest_sha,
                "sync_id": sync_id,
            }

        # Changed (or manual): fetch tree + enqueue downstream.
        tree = get_tree_recursive(access_token, org, project, repo, default_branch)
        repo_type = repo_row.get("repo_type", "application")
        if repo_type == "standards":
            enqueued = _queue_downstream_for_standards_repo(
                workspace_id, slug, org, project, repo,
                connection_id, default_branch, tree,
            )
        else:
            enqueued = _queue_downstream_for_application_repo(
                workspace_id, slug, org, project, repo,
                connection_id, default_branch, tree,
            )

        _update_sync_status(
            workspace_id, slug,
            status="success",
            commit_sha=latest_sha,
        )
        logger.info(
            "ADO sync success: %s/%s -> %d downstream messages",
            workspace_id, slug, enqueued,
        )
        return {
            "slug": slug,
            "status": "success",
            "commit_sha": latest_sha,
            "downstream_enqueued": enqueued,
            "sync_id": sync_id,
        }

    except AzureDevOpsAuthError as e:
        msg = "reconnect_required"
        _update_sync_status(workspace_id, slug, status="failed", error=msg)
        logger.error("ADO sync auth-failed: %s/%s: %s", workspace_id, slug, e)
        raise
    except AzureDevOpsNotFoundError:
        msg = "repo_or_branch_not_found"
        _update_sync_status(workspace_id, slug, status="failed", error=msg)
        raise
    except AzureDevOpsAPIError as e:
        msg = f"ado_api_error: {e}"
        _update_sync_status(workspace_id, slug, status="failed", error=str(e))
        raise
    except RuntimeError as e:
        # Internal classified failure (token refresh failed,
        # default_branch missing, etc.).
        _update_sync_status(workspace_id, slug, status="failed", error=str(e))
        raise
    except Exception as e:
        _update_sync_status(workspace_id, slug, status="failed", error=str(e))
        raise


# ---------------------------------------------------------------------------
# Lambda entry point
# ---------------------------------------------------------------------------


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """SQS partial-batch-failure handler. One repo per message."""
    records = event.get("Records", [])
    logger.info("ADO sync invoked with %d record(s)", len(records))

    batch_item_failures: List[Dict[str, str]] = []
    for record in records:
        try:
            body = json.loads(record.get("body", "{}"))
            process_sync_message(body)
        except Exception as e:
            logger.exception("ADO sync record failed: %s", e)
            batch_item_failures.append({"itemIdentifier": record["messageId"]})

    if batch_item_failures:
        return {"batchItemFailures": batch_item_failures}
    return {"batchItemFailures": []}
