# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
SharePoint Sync Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to fan out sync jobs for all active
SharePoint connections. Each connection gets a message in the sync queue.
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
SHAREPOINT_SYNC_QUEUE = os.environ.get("SHAREPOINT_SYNC_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


def get_all_sharepoint_connections() -> List[Dict]:
    """Query DynamoDB for all active SharePoint connections across all workspaces."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    connections = []

    try:
        paginator = table.meta.client.get_paginator("scan")

        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression="begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={":sk_prefix": "SHAREPOINT_CONNECTION#"},
        ):
            for item in page.get("Items", []):
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    workspace_id = pk[3:]
                    item["workspace_id"] = workspace_id

                    # Only include connections with selected sites
                    if item.get("selected_sites"):
                        connections.append(item)

    except ClientError as e:
        logger.error(f"Error scanning for SharePoint connections: {e}")

    return connections


def queue_sync_job(connection: Dict) -> bool:
    """Send a sync job to the SharePoint sync queue."""
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
            QueueUrl=SHAREPOINT_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued SharePoint sync for {workspace_id}/{connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to queue sync for {connection_id}: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    """EventBridge-triggered handler for hourly SharePoint sync scheduling."""
    logger.info("SharePoint scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[sharepoint-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not SHAREPOINT_SYNC_QUEUE:
        logger.error("SHAREPOINT_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    connections = get_all_sharepoint_connections()
    logger.info(f"Found {len(connections)} active SharePoint connections")

    if not connections:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No active SharePoint connections to sync",
                "connections_found": 0,
            }),
        }

    queued = 0
    for conn in connections:
        if queue_sync_job(conn):
            queued += 1

    logger.info(f"Queued {queued}/{len(connections)} SharePoint sync jobs")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "SharePoint sync scheduling completed",
            "connections_found": len(connections),
            "jobs_queued": queued,
        }),
    }
