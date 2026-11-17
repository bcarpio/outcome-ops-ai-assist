# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
   skip-unchanged without re-uploading. Hourly scheduler reticks
   stay cheap.
5. If changed (or manual): walk the tree, fetch each interesting
   file's content via the ADO REST API, and write it directly to
   S3 under
   ``workspaces/{workspace_id}/azure_devops/{slug}/{file_path}``
   with ``source: "azure_devops"`` metadata. The KB indexer
   ``process-documents`` is S3-triggered + source-agnostic, so
   no downstream Lambda change is needed for chat KB search to
   see ADO files.
6. Update last_commit_sha + sync_status=success.

On failure: mark sync_status=failed with last_sync_error populated,
then raise so SQS retries per the redrive policy (DLQ after 3 attempts).

Why we write S3 directly instead of enqueuing to file-ingestion or
repo-summaries
------------------------------------------------------------------
file-ingestion + process-repo-summary are GitHub-installation-token
shaped. Enqueuing a ``provider="azure_devops"`` message would
Pydantic-reject inside those consumers and DLQ -- contaminating
the existing GitHub DLQ alarms. Writing to S3 + leaning on
process-documents (which already accepts any ``source``) gets us
end-to-end KB search for ADO without touching GitHub prod paths.

The trade-off is that code-map generation (the symbol-graph
pipeline driven by process-repo-summary) does NOT run for ADO
repos in this MVP. Chat full-text retrieval works; symbol-graph
tools (find_callers, find_definitions, etc.) won't find ADO
symbols. A future iteration could provider-aware process-repo-
summary or split out an ado-repo-summaries worker.
"""

import json
import logging
import os
import sys
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Explicit timeouts so a wedged network call can't hang the Lambda until
# its function timeout (python:S7618).
_BOTO_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3})


# shared/ is bundled into the deploy zip at the package root by
# terraform source_path.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from shared import dual_write  # noqa: E402

from shared.azure_devops_api import (  # noqa: E402
    AzureDevOpsAPIError,
    AzureDevOpsAuthError,
    AzureDevOpsNotFoundError,
    get_default_branch,
    get_file_content,
    get_latest_commit_sha,
    get_tree_recursive,
)
from shared.oauth.azure_devops import refresh_access_token  # noqa: E402


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KB_BUCKET = os.environ.get("KB_BUCKET")
# after the S3 upload completes we enqueue a single
# message to repo-summaries so the same code-map + symbol-graph
# pipeline that runs for github (process-repo-summary +
# process-batch-summary) also runs for ADO. The downstream workers
# read provider="azure_devops" + slug + workspace_id from the
# message and route the file-content fetch to the S3 mirror.
REPO_SUMMARIES_QUEUE = os.environ.get("REPO_SUMMARIES_QUEUE")
AZURE_DEVOPS_TENANT_ID = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
AZURE_DEVOPS_CLIENT_ID = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", ""
)


# File filtering -- mirrors lambda/github-sync/handler.py constants
# so the ADO and GitHub paths produce the same shape of KB index.
# If the github-sync constants change, this needs to follow.
DOC_EXTENSIONS = {
    ".md", ".txt", ".html", ".htm", ".yaml", ".yml",
    ".json", ".rst", ".pdf", ".csv", ".xml",
}
CODE_EXTENSIONS = {
    ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt",
    ".go", ".rs", ".rb", ".php", ".cs", ".cpp", ".c", ".h",
    ".tf",
}
INDEXABLE_EXTENSIONS = DOC_EXTENSIONS | CODE_EXTENSIONS
SKIP_DIRS = {
    "node_modules", ".git", "__pycache__", ".venv", "venv",
    "dist", "build", ".next", ".nuxt", "coverage",
}
MAX_FILE_SIZE = 1024 * 1024


_dynamodb = None
_ssm = None
_sqs = None
_client_secret: Optional[str] = None


def _get_sqs():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", config=_BOTO_CONFIG)
    return _sqs


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
    return _dynamodb


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm", config=_BOTO_CONFIG)
    return _ssm


def _get_client_secret() -> str:
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
    file_count: Optional[int] = None,
) -> None:
    """Write the new sync_status. Terminal states (success / failed /
    skip-unchanged) update last_sync_at. in_progress only bumps
    updated_at so upstream staleness detection can fire."""
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

    if file_count is not None:
        set_parts.append("last_synced_file_count = :fc")
        expr_values[":fc"] = file_count

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
# File filter (mirrors github-sync's should_process_*)
# ---------------------------------------------------------------------------


def _is_indexable(tree_item: Dict[str, Any]) -> bool:
    if tree_item.get("gitObjectType") != "blob":
        return False
    path = tree_item.get("path", "")
    if not path:
        return False
    # Skip files under any of the SKIP_DIRS at any depth.
    parts = path.strip("/").split("/")
    for part in parts[:-1]:
        if part in SKIP_DIRS:
            return False
    ext = os.path.splitext(path)[1].lower()
    if ext not in INDEXABLE_EXTENSIONS:
        return False
    size = tree_item.get("size", 0)
    if isinstance(size, (int, float)) and size > MAX_FILE_SIZE:
        logger.debug("Skipping large file: %s (%s bytes)", path, size)
        return False
    return True


# ---------------------------------------------------------------------------
# S3 upload (mirrors github-sync's upload_file_to_s3)
# ---------------------------------------------------------------------------


_CONTENT_TYPES = {
    ".md": "text/markdown",
    ".json": "application/json",
    ".yaml": "text/yaml",
    ".yml": "text/yaml",
    ".rst": "text/x-rst",
    ".txt": "text/plain",
    ".html": "text/html",
    ".htm": "text/html",
    ".pdf": "application/pdf",
    ".csv": "text/csv",
    ".xml": "application/xml",
}


def _enqueue_repo_summaries_job(
    *,
    workspace_id: str,
    slug: str,
    repo_type: str,
    connection_id: str,
    all_files: List[str],
    last_commit_sha: Optional[str],
    current_commit_sha: str,
) -> None:
    """Fan into the existing code-map + symbol-graph pipeline.

    Mirrors github-sync's queue_repo_for_code_maps message shape, with
    these ADO-specific deltas:
    - ``provider="azure_devops"`` routes process-batch-summary's file
      fetch to the S3 mirror (lambda/shared/source_fetch.py).
    - ``slug`` is the org/project/repo string used to construct the
      S3 key shape.
    - ``connection_id`` replaces ``installation_id`` for consistency
      with the AZDO_CONNECTION# row, though process-repo-summary's
      ADO branch doesn't need it at the upstream layer (the sync
      worker already wrote the source).

    Repo-summaries queue is FIFO; MessageGroupId orders messages
    per repo. We use ``slug`` (the natural per-repo identifier);
    the dedup id includes the commit SHA so re-syncs at the same
    SHA don't fan out twice.
    """
    if not REPO_SUMMARIES_QUEUE:
        logger.warning("REPO_SUMMARIES_QUEUE not configured -- skipping code-map fan-out")
        return
    repo_name = slug.rsplit("/", 1)[-1]
    body: Dict[str, Any] = {
        "repo": repo_name,
        "repo_project": slug,
        "repo_type": repo_type,
        "workspace_id": workspace_id,
        "provider": "azure_devops",
        "slug": slug,
        "connection_id": connection_id,
        "all_files": all_files,
        "discover_code_units": True,
        "current_commit_sha": current_commit_sha,
    }
    if last_commit_sha:
        body["last_commit_sha"] = last_commit_sha
    try:
        _get_sqs().send_message(
            QueueUrl=REPO_SUMMARIES_QUEUE,
            MessageBody=json.dumps(body),
            MessageGroupId=slug,
            MessageDeduplicationId=f"{workspace_id}-{slug}-{current_commit_sha}",
        )
        logger.info(
            "Queued repo-summaries fan-out for %s/%s @ %s (%d files)",
            workspace_id, slug, current_commit_sha, len(all_files),
        )
    except ClientError as e:
        logger.warning(
            "Failed to enqueue repo-summaries for %s/%s: %s",
            workspace_id, slug, e,
        )


def _upload_blob_to_s3(
    workspace_id: str,
    slug: str,
    file_path: str,
    content_bytes: bytes,
    commit_sha: str,
    repo_type: str,
) -> Optional[str]:
    """Write one file to the KB bucket.

    Key shape: ``workspaces/{workspace_id}/azure_devops/{slug}/{path}``
    -- mirrors github-sync's ``workspaces/{ws}/github/{repo}/{path}``
    so process-documents picks it up via the standard ``workspaces/``
    S3 ObjectCreated trigger and the vector key gets a consistent
    ``{workspace_id}#azure_devops#...`` prefix.

    Metadata uses the ``source: "azure_devops"`` convention so
    process-documents' source-agnostic indexing flow surfaces the
    file in chat retrieval.
    """
    if not KB_BUCKET:
        logger.warning("KB_BUCKET not configured -- skipping S3 upload")
        return None

    # ADO paths are leading-slashed; normalize so the S3 key is clean.
    clean_path = file_path.lstrip("/")
    s3_key = f"workspaces/{workspace_id}/azure_devops/{slug}/{clean_path}"

    ext = os.path.splitext(clean_path)[1].lower()
    content_type = _CONTENT_TYPES.get(ext, "text/plain")

    metadata = {
        "source": "azure_devops",
        "workspace_id": workspace_id,
        "azure_devops_slug": slug,
        "azure_devops_file_path": clean_path,
        "azure_devops_commit_sha": commit_sha,
        "azure_devops_repo_type": repo_type,
    }

    try:
        dual_write.s3_put_object(
            bucket=KB_BUCKET,
            key=s3_key,
            body=content_bytes,
            ContentType=content_type,
            Metadata=metadata,
        )
        return s3_key
    except ClientError as e:
        logger.error("Failed to upload %s to S3: %s", clean_path, e)
        return None


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
        logger.warning("ADO sync skip -- repo row missing: %s/%s", workspace_id, slug)
        return {"slug": slug, "status": "skip-missing-row", "sync_id": sync_id}
    if not repo_row.get("enabled", True):
        logger.info("ADO sync skip -- repo disabled: %s/%s", workspace_id, slug)
        return {"slug": slug, "status": "skip-disabled", "sync_id": sync_id}

    repo_type = repo_row.get("repo_type", "application")

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

        # Changed (or manual): walk tree, upload each indexable file.
        tree = get_tree_recursive(access_token, org, project, repo, default_branch)
        indexable = [t for t in tree if _is_indexable(t)]

        uploaded = 0
        upload_errors = 0
        for item in indexable:
            try:
                content = get_file_content(
                    access_token, org, project, repo,
                    item["path"], default_branch,
                )
            except AzureDevOpsAuthError:
                # Token may have expired mid-walk; bail and let SQS retry.
                raise
            except AzureDevOpsNotFoundError:
                # Race: file deleted between tree-walk and content fetch.
                logger.info(
                    "ADO sync: file vanished between tree + fetch: %s",
                    item.get("path"),
                )
                continue
            except AzureDevOpsAPIError as e:
                upload_errors += 1
                logger.warning(
                    "ADO sync: get_file_content failed for %s: %s",
                    item.get("path"), e,
                )
                continue

            s3_key = _upload_blob_to_s3(
                workspace_id=workspace_id,
                slug=slug,
                file_path=item["path"],
                content_bytes=content,
                commit_sha=latest_sha,
                repo_type=repo_type,
            )
            if s3_key:
                uploaded += 1
            else:
                upload_errors += 1

        _update_sync_status(
            workspace_id, slug,
            status="success",
            commit_sha=latest_sha,
            file_count=uploaded,
        )

        # fan into the code-map + symbol-graph pipeline
        # only for application repos (matches github-sync's behavior).
        # Standards repos skip code-map; their content is already in
        # S3 for chat KB search via process-documents.
        if repo_type == "application" and uploaded > 0:
            # ADO returns paths leading-slashed ("/lambda/foo/handler.py").
            # The downstream backends in process-repo-summary's python
            # backend filter on prefix-match ("lambda/"...) which the
            # leading slash silently breaks -- the result is the
            # discover_lambda_handlers / discover_frontend_* etc. paths
            # match 0 files and every code unit collapses into the
            # catch-all "shared" / "tests/other" buckets. The S3 mirror
            # already normalizes the slash off (
            # _upload_blob_to_s3), so the enqueued list needs to use
            # the same normalized form. Burned 2026-06-05 on the first
            # marketplace-events query.
            indexable_paths = [item["path"].lstrip("/") for item in indexable]
            _enqueue_repo_summaries_job(
                workspace_id=workspace_id,
                slug=slug,
                repo_type=repo_type,
                connection_id=connection_id,
                all_files=indexable_paths,
                last_commit_sha=stored_sha,
                current_commit_sha=latest_sha,
            )

        logger.info(
            "ADO sync success: %s/%s -> %d uploaded (%d errors)",
            workspace_id, slug, uploaded, upload_errors,
        )
        return {
            "slug": slug,
            "status": "success",
            "commit_sha": latest_sha,
            "uploaded": uploaded,
            "upload_errors": upload_errors,
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
        _update_sync_status(workspace_id, slug, status="failed", error=str(e))
        raise
    except RuntimeError as e:
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
