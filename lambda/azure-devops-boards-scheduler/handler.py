# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Azure DevOps Boards Sync Scheduler Lambda for OutcomeOps AI Assist.

EventBridge-triggered hourly. Enumerates every enabled Boards project
across every workspace and enqueues a sync message per project to the
Boards sync queue. The sync worker is the actual consumer.

Direct analog of ``lambda/jira-scheduler/handler.py`` adapted to
Boards' per-project row shape (Jira stores selected projects on the
connection row; Boards has one AZDO_BOARDS_PROJECT# row per project
per DDB schema).

Active-region guard mirrors the Jira scheduler: the passive region's
scheduler tick logs + returns without side effects so no double
enqueueing happens during dual-write windows.
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

# Explicit timeouts so a wedged network call can't hang the Lambda until
# its function timeout (python:S7618).
_BOTO_CONFIG = Config(connect_timeout=10, read_timeout=60, retries={"max_attempts": 3})


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
AZURE_DEVOPS_BOARDS_SYNC_QUEUE = os.environ.get("AZURE_DEVOPS_BOARDS_SYNC_QUEUE")

dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
sqs = boto3.client("sqs", config=_BOTO_CONFIG)


def get_all_enabled_projects() -> List[Dict]:
    """Paginated scan for every enabled AZDO_BOARDS_PROJECT# row across
    every workspace.

    Per ``feedback_enterprise_scale_pagination`` we walk the whole
    ``LastEvaluatedKey`` chain instead of relying on a single scan
    page -- a customer with hundreds of projects would silently
    truncate otherwise.
    """
    if not WORKSPACES_TABLE:
        return []

    table = dynamodb.Table(WORKSPACES_TABLE)
    projects: List[Dict] = []

    try:
        paginator = table.meta.client.get_paginator("scan")
        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression="begins_with(SK, :sk_prefix) AND enabled = :true",
            ExpressionAttributeValues={
                ":sk_prefix": "AZDO_BOARDS_PROJECT#",
                ":true": True,
            },
        ):
            for item in page.get("Items", []):
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    item["workspace_id"] = pk[3:]
                    projects.append(item)
    except ClientError as e:
        logger.error(f"Error scanning for enabled Boards projects: {e}")

    return projects


def queue_sync_job(project: Dict) -> bool:
    """Enqueue one project sync message to the Boards sync queue.

    Body shape matches what the sync worker's ``process_sync_request``
    reads. Missing fields on the row → skip with a warning.
    """
    workspace_id = project.get("workspace_id")
    connection_id = project.get("connection_id")
    slug = project.get("slug")
    org = project.get("org")
    project_name = project.get("project")

    if not (workspace_id and connection_id and slug and org and project_name):
        logger.warning(f"[boards-scheduler] skipping malformed row: {slug}")
        return False

    message = {
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "slug": slug,
        "org": org,
        "project": project_name,
        "triggered_by": "scheduler",
    }

    try:
        sqs.send_message(
            QueueUrl=AZURE_DEVOPS_BOARDS_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"[boards-scheduler] queued sync for {workspace_id}/{slug}")
        return True
    except ClientError as e:
        logger.error(f"[boards-scheduler] failed to queue sync for {slug}: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    """EventBridge-triggered handler."""
    logger.info("boards-scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[boards-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not AZURE_DEVOPS_BOARDS_SYNC_QUEUE:
        logger.error("AZURE_DEVOPS_BOARDS_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    projects = get_all_enabled_projects()
    logger.info(f"[boards-scheduler] found {len(projects)} enabled Boards projects")

    if not projects:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No enabled Boards projects to sync",
                "projects_found": 0,
            }),
        }

    queued = 0
    for project in projects:
        if queue_sync_job(project):
            queued += 1

    logger.info(f"[boards-scheduler] queued {queued}/{len(projects)} sync jobs")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Boards sync scheduling completed",
            "projects_found": len(projects),
            "jobs_queued": queued,
        }),
    }
