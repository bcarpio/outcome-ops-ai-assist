"""
Confluence Sync Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to fan out sync jobs for all active
Confluence connections. Each connection gets a message in the sync queue.

This enables incremental syncs - only pages modified since last sync
are processed.
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
CONFLUENCE_SYNC_QUEUE = os.environ.get("CONFLUENCE_SYNC_QUEUE")

# AWS clients
dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


def get_all_confluence_connections() -> List[Dict]:
    """
    Query DynamoDB for all active Confluence connections across all workspaces.

    Uses GSI to find all CONFLUENCE_CONNECTION items.

    Returns:
        List of connection records
    """
    table = dynamodb.Table(WORKSPACES_TABLE)
    connections = []

    try:
        # Scan for all Confluence connections
        # SK starts with CONFLUENCE_CONNECTION#
        paginator = table.meta.client.get_paginator("scan")

        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression="begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={":sk_prefix": "CONFLUENCE_CONNECTION#"},
        ):
            for item in page.get("Items", []):
                # Extract workspace_id from PK (WS#{workspace_id})
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    workspace_id = pk[3:]
                    item["workspace_id"] = workspace_id

                    # Only include connections with selected spaces
                    if item.get("selected_spaces"):
                        connections.append(item)

    except ClientError as e:
        logger.error(f"Error scanning for connections: {e}")

    return connections


def queue_sync_job(connection: Dict) -> bool:
    """
    Send a sync job to the Confluence sync queue.

    Args:
        connection: Connection record from DynamoDB

    Returns:
        True if message sent successfully
    """
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
            QueueUrl=CONFLUENCE_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued sync for {workspace_id}/{connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to queue sync for {connection_id}: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    EventBridge-triggered handler for hourly Confluence sync scheduling.

    Finds all active Confluence connections and queues sync jobs.
    """
    logger.info("Confluence scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[confluence-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not CONFLUENCE_SYNC_QUEUE:
        logger.error("CONFLUENCE_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    # Get all active connections
    connections = get_all_confluence_connections()
    logger.info(f"Found {len(connections)} active Confluence connections")

    if not connections:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No active connections to sync",
                "connections_found": 0,
            }),
        }

    # Queue sync jobs
    queued = 0
    for conn in connections:
        if queue_sync_job(conn):
            queued += 1

    logger.info(f"Queued {queued}/{len(connections)} sync jobs")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Sync scheduling completed",
            "connections_found": len(connections),
            "jobs_queued": queued,
        }),
    }
