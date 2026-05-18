"""
OneDrive Sync Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to fan out sync jobs for all active
OneDrive connections. Each connection with at least one selected folder
gets a message in the sync queue.

Mirrors sharepoint-scheduler exactly. Differences: the SK prefix
(`ONEDRIVE_CONNECTION#`), the queue env var (`ONEDRIVE_SYNC_QUEUE`), and
the gating field (`selected_folders` instead of `selected_sites`).
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
ONEDRIVE_SYNC_QUEUE = os.environ.get("ONEDRIVE_SYNC_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


def get_all_onedrive_connections() -> List[Dict]:
    """Scan DynamoDB for all OneDrive connections that have at least one
    folder selected."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    connections = []

    try:
        paginator = table.meta.client.get_paginator("scan")

        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression="begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={":sk_prefix": "ONEDRIVE_CONNECTION#"},
        ):
            for item in page.get("Items", []):
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    workspace_id = pk[3:]
                    item["workspace_id"] = workspace_id

                    # Only include connections with selected folders
                    if item.get("selected_folders"):
                        connections.append(item)

    except ClientError as e:
        logger.error(f"Error scanning for OneDrive connections: {e}")

    return connections


def queue_sync_job(connection: Dict) -> bool:
    """Send a sync job to the OneDrive sync queue."""
    workspace_id = connection.get("workspace_id")
    connection_id = connection.get("connection_id")

    if not workspace_id or not connection_id:
        logger.warning(f"Invalid connection record: {connection}")
        return False

    message = {
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "triggered_by": "scheduler",
    }

    try:
        sqs.send_message(
            QueueUrl=ONEDRIVE_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued OneDrive sync for {workspace_id}/{connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to queue sync for {connection_id}: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    """EventBridge-triggered handler for hourly OneDrive sync scheduling."""
    logger.info("OneDrive scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[onedrive-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not ONEDRIVE_SYNC_QUEUE:
        logger.error("ONEDRIVE_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    connections = get_all_onedrive_connections()
    logger.info(f"Found {len(connections)} active OneDrive connections")

    if not connections:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No active OneDrive connections to sync",
                "connections_found": 0,
            }),
        }

    queued = 0
    for conn in connections:
        if queue_sync_job(conn):
            queued += 1

    logger.info(f"Queued {queued}/{len(connections)} OneDrive sync jobs")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "OneDrive sync scheduling completed",
            "connections_found": len(connections),
            "jobs_queued": queued,
        }),
    }
