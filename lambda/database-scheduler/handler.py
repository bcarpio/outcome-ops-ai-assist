# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Database Sync Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to fan out sync jobs for all active
database connections. Gates on `scope_config.introspection.include`
being non-empty — connections without any selected schemas/tables
are idle and skipped.

Mirrors onenote-scheduler exactly. Differences: SK prefix
(`DATABASE_CONNECTION#`), queue env var (`DATABASE_SYNC_QUEUE`),
gating field (nested under scope_config).
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
DATABASE_SYNC_QUEUE = os.environ.get("DATABASE_SYNC_QUEUE")

dynamodb = boto3.resource("dynamodb")
sqs = boto3.client("sqs")


def get_all_database_connections() -> List[Dict]:
    """Scan DynamoDB for database connections with at least one schema
    selected for introspection."""
    table = dynamodb.Table(WORKSPACES_TABLE)
    connections: List[Dict] = []

    try:
        paginator = table.meta.client.get_paginator("scan")

        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression="begins_with(SK, :sk_prefix)",
            ExpressionAttributeValues={":sk_prefix": "DATABASE_CONNECTION#"},
        ):
            for item in page.get("Items", []):
                pk = item.get("PK", "")
                if not pk.startswith("WS#"):
                    continue

                scope = item.get("scope_config") or {}
                include = (scope.get("introspection") or {}).get("include") or []
                if not include:
                    continue

                item["workspace_id"] = pk[3:]
                connections.append(item)

    except ClientError as e:
        logger.error(f"Error scanning for database connections: {e}")

    return connections


def queue_sync_job(connection: Dict) -> bool:
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
            QueueUrl=DATABASE_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info(f"Queued database sync for {workspace_id}/{connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to queue sync for {connection_id}: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    logger.info("Database scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[database-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not DATABASE_SYNC_QUEUE:
        logger.error("DATABASE_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    connections = get_all_database_connections()
    logger.info(f"Found {len(connections)} active database connections")

    if not connections:
        return {
            "statusCode": 200,
            "body": json.dumps({
                "message": "No active database connections to sync",
                "connections_found": 0,
            }),
        }

    queued = 0
    for conn in connections:
        if queue_sync_job(conn):
            queued += 1

    logger.info(f"Queued {queued}/{len(connections)} database sync jobs")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "message": "Database sync scheduling completed",
            "connections_found": len(connections),
            "jobs_queued": queued,
        }),
    }
