"""
Jira Sync Lambda for OutcomeOps AI Assist.

Orchestrates sync of Jira issues:
1. Receives sync request from JIRA_SYNC_QUEUE
2. Lists issues in selected projects
3. Queues individual issues to FILE_INGESTION_QUEUE
4. Handles delta sync (only sync issues modified since last_sync_at)
5. Updates connection sync status

This is the orchestrator layer - actual issue fetching is done by file-ingestion.
"""

import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import boto3
from botocore.exceptions import ClientError

# Make shared/ importable; deploy package now bundles lambda/shared/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.audit_writer import log_license_fail_open, log_source_sync  # noqa: E402

# ADR-019: license validator + enforcement + usage tracking come from the
# outcomeops_license layer (mounted at /opt/python in Lambda). Wrap the
# top-level import in try/except so the module still loads when the layer is
# unattached -- lambda_handler then catches the missing-symbol case and emits
# a LICENSE_FAIL_OPEN regression-detector audit row before re-raising.
try:
    from outcomeops_license import (  # noqa: E402
        check_repo_limit,
        validate_license,
    )
except ImportError:  # pragma: no cover - deploy bug path, exercised in tests
    validate_license = None  # type: ignore[assignment]
    check_repo_limit = None  # type: ignore[assignment]

from jira_api import (
    JiraAPIError,
    JiraAuthError,
    get_issue_comments,
    list_all_issues_in_project,
    search_issues_modified_since,
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


def get_connection(workspace_id: str, connection_id: str) -> Optional[Dict]:
    """Get a Jira connection by ID."""
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        response = table.get_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"JIRA_CONNECTION#{connection_id}",
            }
        )
        return response.get("Item")
    except ClientError as e:
        logger.error(f"Failed to get connection: {e}")
        return None


def update_sync_status(
    workspace_id: str,
    connection_id: str,
    status: str,
    issue_count: Optional[int] = None,
    error: Optional[str] = None,
):
    """Update connection sync status."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    now = datetime.now(timezone.utc).isoformat()

    update_expr = "SET last_sync_status = :status, updated_at = :now"
    expr_values = {":status": status, ":now": now}

    if status in ("success", "failed", "auth_error"):
        update_expr += ", last_sync_at = :sync_at"
        expr_values[":sync_at"] = now

    if issue_count is not None:
        update_expr += ", issue_count = :count"
        expr_values[":count"] = issue_count

    if error:
        update_expr += ", last_sync_error = :error"
        expr_values[":error"] = error
    else:
        update_expr += " REMOVE last_sync_error"

    try:
        table.update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"JIRA_CONNECTION#{connection_id}",
            },
            UpdateExpression=update_expr,
            ExpressionAttributeValues=expr_values,
        )
    except ClientError as e:
        logger.error(f"Failed to update connection status: {e}")


def queue_issue_for_ingestion(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    issue: Dict,
    project_key: str,
    project_name: str,
    comments: List[Dict],
    include_attachments: bool,
    sync_id: str,
    file_index: int,
    total_files: int,
):
    """Queue a Jira issue for file ingestion."""
    fields = issue.get("fields", {})

    message = {
        "source": "jira",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "cloud_id": cloud_id,
        "file": {
            "issue_id": issue.get("id"),
            "issue_key": issue.get("key"),
            "project_key": project_key,
            "project_name": project_name,
            "summary": fields.get("summary", ""),
            "description": fields.get("description"),
            "status": fields.get("status", {}).get("name", "Unknown"),
            "priority": fields.get("priority", {}).get("name") if fields.get("priority") else None,
            "issue_type": fields.get("issuetype", {}).get("name", "Unknown"),
            "assignee": fields.get("assignee", {}).get("displayName") if fields.get("assignee") else None,
            "reporter": fields.get("reporter", {}).get("displayName") if fields.get("reporter") else None,
            "labels": fields.get("labels", []),
            "created": fields.get("created"),
            "updated": fields.get("updated"),
            "comments": comments,
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
            MessageDeduplicationId=f"{sync_id}-issue-{issue.get('id')}",
        )
    except ClientError as e:
        logger.error(f"Failed to queue issue {issue.get('key')}: {e}")

    # Queue attachments if enabled
    if include_attachments:
        attachments = fields.get("attachment", [])
        for attachment in attachments:
            queue_attachment_for_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                cloud_id=cloud_id,
                attachment=attachment,
                issue_key=issue.get("key"),
                project_key=project_key,
                project_name=project_name,
                sync_id=sync_id,
            )


def queue_attachment_for_ingestion(
    workspace_id: str,
    connection_id: str,
    cloud_id: str,
    attachment: Dict,
    issue_key: str,
    project_key: str,
    project_name: str,
    sync_id: str,
):
    """Queue a Jira attachment for file ingestion."""
    message = {
        "source": "jira_attachment",
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "cloud_id": cloud_id,
        "file": {
            "attachment_id": attachment.get("id"),
            "issue_key": issue_key,
            "project_key": project_key,
            "project_name": project_name,
            "filename": attachment.get("filename", "attachment"),
            "mime_type": attachment.get("mimeType", ""),
            "size": attachment.get("size", 0),
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
    Process a sync request for a Jira connection.

    Delta sync strategy:
    - If last_sync_at exists, only sync issues modified since that timestamp
    - If no last_sync_at (first sync), do full sync of all issues

    Args:
        message: Sync request with workspace_id, connection_id

    Returns:
        Result dict
    """
    sync_start = time.time()
    trigger = message.get("trigger", "scheduled")
    workspace_id = message.get("workspace_id")
    connection_id = message.get("connection_id")
    sync_id = f"jira_sync_{secrets.token_urlsafe(8)}"

    logger.info(f"Processing Jira sync request for {workspace_id}/{connection_id} [{sync_id}]")

    # Get connection details
    connection = get_connection(workspace_id, connection_id)
    if not connection:
        logger.error(f"Connection not found: {connection_id}")
        return {"status": "error", "error": "Connection not found"}

    cloud_id = connection.get("cloud_id")
    selected_projects = connection.get("selected_projects", [])
    include_attachments = connection.get("include_attachments", True)
    include_comments = connection.get("include_comments", True)
    last_sync_at = connection.get("last_sync_at")

    if not selected_projects:
        logger.warning(f"No projects selected for connection {connection_id}")
        update_sync_status(workspace_id, connection_id, "no_projects_selected")
        return {"status": "skipped", "reason": "No projects selected"}

    # Update status to syncing
    update_sync_status(workspace_id, connection_id, "in_progress")

    # Get OAuth token
    access_token = refresh_access_token(connection_id)

    if not access_token:
        logger.error(f"Failed to get access token for connection {connection_id}")
        update_sync_status(
            workspace_id, connection_id, "auth_error",
            error="Failed to refresh OAuth token"
        )
        return {"status": "error", "error": "Authentication failed"}

    # Determine sync type
    is_delta_sync = last_sync_at is not None
    sync_type = "delta" if is_delta_sync else "full"
    logger.info(f"Starting {sync_type} sync for connection {connection_id}")

    # Collect all issues from selected projects
    all_issues = []
    all_issue_keys = []
    projects_processed = []
    projects_blocked: List[str] = []

    try:
        for project in selected_projects:
            project_key = project.get("key")
            project_name = project.get("name", project_key)

            # ADR-019: per-project repo limit. Use a stable, namespaced
            # identifier (`jira:{project_key}`) so the license-server's
            # string-equality dedupe never collides with GitHub repo names.
            # Already-tracked projects always return allowed=True.
            source_id = f"jira:{project_key}"
            try:
                limit_result = check_repo_limit(repo=source_id)
                if not limit_result.allowed:
                    logger.warning(
                        f"[jira-sync] Repo limit reached for {source_id}: "
                        f"{limit_result.reason} "
                        f"(current={limit_result.current}/{limit_result.limit})"
                    )
                    projects_blocked.append(project_name)
                    continue
            except Exception as e:
                # Layer's network-error fallback handles this; if we got here
                # the call itself blew up unexpectedly. Log and continue.
                logger.warning(
                    f"[jira-sync] check_repo_limit failed for "
                    f"{source_id}: {e}"
                )

            logger.info(f"[{sync_type}] Listing issues in project: {project_name} ({project_key})")

            try:
                if is_delta_sync:
                    # Delta sync: only issues modified since last_sync_at
                    issues = search_issues_modified_since(
                        access_token, cloud_id, project_key, last_sync_at
                    )
                    logger.info(f"Found {len(issues)} modified issues in project {project_key}")
                else:
                    # Full sync: all issues in project
                    issues = list_all_issues_in_project(access_token, cloud_id, project_key)
                    logger.info(f"Found {len(issues)} issues in project {project_key}")

                for issue in issues:
                    # Get comments if enabled
                    comments = []
                    if include_comments:
                        try:
                            comments = get_issue_comments(access_token, cloud_id, issue.get("key"))
                        except JiraAPIError as e:
                            logger.warning(f"Failed to get comments for {issue.get('key')}: {e}")

                    all_issues.append({
                        "issue": issue,
                        "project_key": project_key,
                        "project_name": project_name,
                        "comments": comments,
                    })
                    all_issue_keys.append(issue.get("key"))

                projects_processed.append(project_name)

            except JiraAPIError as e:
                logger.error(f"Error listing issues in project {project_key}: {e}")
                continue

        if not all_issues:
            logger.info(f"No issues found in any selected project")
            update_sync_status(workspace_id, connection_id, "success", issue_count=0)
            return {"status": "completed", "issues_queued": 0}

        # Queue all issues for ingestion
        total_files = len(all_issues)
        logger.info(f"Queuing {total_files} issues for ingestion")

        for i, issue_info in enumerate(all_issues):
            queue_issue_for_ingestion(
                workspace_id=workspace_id,
                connection_id=connection_id,
                cloud_id=cloud_id,
                issue=issue_info["issue"],
                project_key=issue_info["project_key"],
                project_name=issue_info["project_name"],
                comments=issue_info["comments"],
                include_attachments=include_attachments,
                sync_id=sync_id,
                file_index=i,
                total_files=total_files,
            )

        # Queue orphan detection for each processed project (full sync only)
        if not is_delta_sync:
            for project in selected_projects:
                project_name = project.get("name", project.get("key", ""))
                # Get issue keys for this project
                project_issue_keys = [
                    info["issue"]["key"]
                    for info in all_issues
                    if info["project_name"] == project_name
                ]
                if project_issue_keys:
                    queue_orphan_detection(
                        workspace_id=workspace_id,
                        source="jira",
                        prefix=project_name + "/",
                        current_ids=project_issue_keys,
                    )

        # Update connection status
        update_sync_status(
            workspace_id, connection_id, "success",
            issue_count=total_files
        )

        log_source_sync(
            audit_table_name=os.environ.get("AUDIT_TABLE", ""),
            source="jira",
            workspace_id=workspace_id,
            target=",".join(projects_processed),
            trigger=trigger,
            files_synced=total_files,
            duration_ms=int((time.time() - sync_start) * 1000),
            extra_details={"sync_id": sync_id, "sync_type": sync_type},
        )
        # ADR-019: liveness tracking now lives in connect/disconnect handlers
        # via report_repos_snapshot (license layer v2.0.0 desired-state model).
        result = {
            "status": "success",
            "sync_id": sync_id,
            "sync_type": sync_type,
            "issues_queued": total_files,
            "projects_processed": projects_processed,
        }
        if projects_blocked:
            result["projects_blocked"] = projects_blocked
        return result

    except JiraAuthError:
        logger.error("Authentication error during sync")
        update_sync_status(
            workspace_id, connection_id, "auth_error",
            error="Authentication failed during sync"
        )
        return {"status": "error", "error": "Authentication failed"}
    except Exception as e:
        logger.error(f"Error during sync: {e}")
        update_sync_status(
            workspace_id, connection_id, "failed",
            error=str(e)
        )
        return {"status": "error", "error": str(e)}


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    SQS-triggered handler for Jira sync orchestration.

    Receives sync requests and queues individual issues for file ingestion.
    """
    # ADR-019: validate license before doing any work. SQS-driven, so we
    # raise on validation failure rather than returning a status code.
    try:
        if validate_license is None:
            raise ImportError("outcomeops_license layer not attached")
        claims = validate_license()
        logger.info(
            f"[jira-sync] License validated: {claims.get('sub')}"
        )
    except ImportError:
        logger.error(
            "[jira-sync] License layer not attached - deploy bug (ADR-019)"
        )
        log_license_fail_open(
            os.environ.get("AUDIT_TABLE", ""), "jira-sync"
        )
        raise
    except Exception as e:
        logger.error(f"[jira-sync] License validation failed: {e}")
        raise

    logger.info(f"Jira sync handler invoked with {len(event.get('Records', []))} records")

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
    logger.info(f"Jira sync orchestration completed: {successful}/{len(results)} successful")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Jira sync orchestration completed",
            "results": results,
        }),
    }
