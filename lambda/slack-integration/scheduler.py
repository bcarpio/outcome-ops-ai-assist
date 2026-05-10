"""
Slack Sync Scheduler

EventBridge fires this hourly. The scheduler scans for connections with
configured channels and enqueues sync jobs onto the slack-sync SQS
queue for the integration lambda to process.

Returns 200 with zero queued and zero skipped when SLACK_ENABLED != "true".
"""

import json
import logging
import os
from typing import Any, Dict

import boto3

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

HANDLER_NAME = "slack-scheduler"

TENANTS_TABLE = os.environ.get("TENANTS_TABLE")
SLACK_SYNC_QUEUE = os.environ.get("SLACK_SYNC_QUEUE")

_dynamodb = None
_sqs_client = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_sqs_client():
    global _sqs_client
    if _sqs_client is None:
        _sqs_client = boto3.client("sqs")
    return _sqs_client


def _is_enabled() -> bool:
    return os.environ.get("SLACK_ENABLED", "false").lower() == "true"


def list_configured_connections():
    """Return all slack_connection rows that have at least one channel configured."""
    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.scan(
        FilterExpression="entity_type = :et AND attribute_exists(config)",
        ExpressionAttributeValues={":et": "slack_connection"},
    )

    connections = []
    for conn in response.get("Items", []):
        config = conn.get("config") or {}
        if config.get("include_channels"):
            connections.append(conn)

    return connections


def queue_sync_job(connection: Dict) -> None:
    sqs = _get_sqs_client()
    config = connection.get("config") or {}
    sqs.send_message(
        QueueUrl=SLACK_SYNC_QUEUE,
        MessageBody=json.dumps({
            "connection_id": connection["connection_id"],
            "workspace_id": connection["workspace_id"],
            "org_id": connection["org_id"],
            "team_id": connection.get("team_id", ""),
            "include_channels": config.get("include_channels", []),
            "cursors": config.get("cursors", {}),
        }),
    )


def handler(event: Dict[str, Any], context: Any) -> Dict:
    """EventBridge tick handler — fans out sync jobs to SQS for due connections."""
    logger.info(f"[{HANDLER_NAME}] Scheduler triggered")

    if not _is_enabled():
        logger.info(f"[{HANDLER_NAME}] SLACK_ENABLED is false; no-op")
        return {"statusCode": 200, "body": json.dumps({"queued": 0, "skipped": 0, "total": 0})}

    connections = list_configured_connections()
    logger.info(f"[{HANDLER_NAME}] Found {len(connections)} configured connections")

    queued = 0
    skipped = 0

    for conn in connections:
        connection_id = conn.get("connection_id")
        status = conn.get("last_sync_status")

        if status in ("queued", "in_progress"):
            logger.info(f"[{HANDLER_NAME}] Skipping {connection_id} - already {status}")
            skipped += 1
            continue

        if status in ("auth_error", "reauth_required"):
            logger.info(f"[{HANDLER_NAME}] Skipping {connection_id} - {status}")
            skipped += 1
            continue

        try:
            queue_sync_job(conn)
            queued += 1
            logger.info(f"[{HANDLER_NAME}] Queued sync for {connection_id}")
        except Exception:
            logger.exception(f"[{HANDLER_NAME}] Failed to queue {connection_id}")

    logger.info(f"[{HANDLER_NAME}] Scheduler complete: queued={queued}, skipped={skipped}")

    return {
        "statusCode": 200,
        "body": json.dumps({
            "queued": queued,
            "skipped": skipped,
            "total": len(connections),
        }),
    }
