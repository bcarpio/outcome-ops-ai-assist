"""
Salesforce sync scheduler.

EventBridge-triggered hourly at :15 UTC (staggered off ServiceNow's :45
and the top-of-hour crowd). Scans the workspaces table for every
Salesforce connection row and enqueues one sync job per healthy
connection to SALESFORCE_SYNC_QUEUE.

Skips connections in states where a scheduler-triggered sync would be
noise:
- ``reauth_required``: user must reconnect first
- ``in_progress``: previous message still processing
- ``needs_oauth``: POST /connect ran but callback hasn't happened yet

Gated on ``is_active_region()`` so passive-region deploys short-circuit
before scanning.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from typing import Any, Dict, List

import boto3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from shared.active_region import is_active_region  # noqa: E402


logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))


ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
SALESFORCE_SYNC_QUEUE = os.environ.get("SALESFORCE_SYNC_QUEUE")


_SKIP_STATUSES = {"reauth_required", "in_progress", "needs_oauth"}
_SF_CONN_SK = "SALESFORCE_CONNECTION#"


_dynamodb = None
_sqs = None


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


def _iter_salesforce_connections() -> List[Dict[str, Any]]:
    """Paginated scan for every SALESFORCE_CONNECTION# row."""
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    connections: List[Dict[str, Any]] = []
    scan_kwargs = {
        "FilterExpression": "begins_with(SK, :prefix)",
        "ExpressionAttributeValues": {":prefix": _SF_CONN_SK},
    }
    while True:
        resp = table.scan(**scan_kwargs)
        for item in resp.get("Items", []) or []:
            pk = item.get("PK", "")
            if pk.startswith("WS#"):
                item["workspace_id"] = pk[3:]
                connections.append(item)
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
    return connections


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    if not is_active_region():
        logger.info("Passive region -- skipping scheduler tick")
        return {"enqueued": 0, "skipped": 0, "reason": "passive_region"}

    connections = _iter_salesforce_connections()
    logger.info(f"Found {len(connections)} Salesforce connections total")

    sqs = _get_sqs()
    enqueued = 0
    skipped = 0
    for conn in connections:
        status = conn.get("last_sync_status") or ""
        if status in _SKIP_STATUSES:
            skipped += 1
            logger.info(
                f"Skipping connection {conn.get('connection_id')} "
                f"(status={status})"
            )
            continue

        workspace_id = conn.get("workspace_id")
        connection_id = conn.get("connection_id")
        if not workspace_id or not connection_id:
            skipped += 1
            logger.warning(f"Malformed connection row: {conn}")
            continue

        cursors = (conn.get("config") or {}).get("cursors") or {}
        sqs.send_message(
            QueueUrl=SALESFORCE_SYNC_QUEUE,
            MessageBody=json.dumps(
                {
                    "workspace_id": workspace_id,
                    "connection_id": connection_id,
                    "cursors": cursors,
                    "triggered_by": "scheduler",
                }
            ),
        )
        enqueued += 1

    logger.info(f"[salesforce-scheduler] enqueued {enqueued} connections (skipped {skipped})")
    return {"enqueued": enqueued, "skipped": skipped}
