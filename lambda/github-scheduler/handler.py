# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
GitHub Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to scan all workspace GitHub repos
and queue sync jobs for repos that have code-maps enabled.

Flow:
1. Scan DynamoDB for all GITHUB_REPO# items with include_code_maps=true
2. Queue a sync job to github-sync SQS for each repo
3. Return summary of queued repos
"""

import json
import logging
import os
from typing import Any, Dict, List

import boto3
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# Configure logging
logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# Environment variables
ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
GITHUB_SYNC_QUEUE = os.environ.get("GITHUB_SYNC_QUEUE")

# Cached clients
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


def scan_github_repos_for_sync() -> List[Dict]:
    """
    Scan all GitHub repos that need syncing.

    Returns repos where:
    - include_code_maps is true
    - enabled is true (not explicitly disabled)
    """
    if not WORKSPACES_TABLE:
        logger.error("WORKSPACES_TABLE not configured")
        return []

    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    repos = []

    try:
        # Scan for all GITHUB_REPO# items
        # Note: For large scale, consider using a GSI on SK prefix
        paginator = table.meta.client.get_paginator("scan")

        for page in paginator.paginate(
            TableName=WORKSPACES_TABLE,
            FilterExpression=Attr("SK").begins_with("GITHUB_REPO#")
            & Attr("include_code_maps").eq(True)
            & (Attr("enabled").not_exists() | Attr("enabled").eq(True)),
        ):
            for item in page.get("Items", []):
                # Extract workspace_id from PK (WS#{workspace_id})
                pk = item.get("PK", "")
                if pk.startswith("WS#"):
                    workspace_id = pk[3:]

                    repos.append({
                        "workspace_id": workspace_id,
                        "repo_name": item.get("repo_name"),
                        "installation_id": item.get("installation_id"),
                        "last_sync_at": item.get("last_sync_at"),
                        "last_sync_status": item.get("last_sync_status"),
                    })

        logger.info(f"Found {len(repos)} repos to sync")
        return repos

    except ClientError as e:
        logger.error(f"Failed to scan repos: {e}")
        return []


def queue_sync_job(workspace_id: str, repo_name: str, installation_id: str) -> bool:
    """Queue a sync job to the github-sync queue."""
    if not GITHUB_SYNC_QUEUE:
        logger.warning("GITHUB_SYNC_QUEUE not configured")
        return False

    try:
        sqs = _get_sqs()

        # Create message body
        message = {
            "workspace_id": workspace_id,
            "repo_name": repo_name,
            "installation_id": installation_id,
            "trigger": "scheduled",
        }

        sqs.send_message(
            QueueUrl=GITHUB_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )

        logger.debug(f"Queued sync for {workspace_id}/{repo_name}")
        return True

    except ClientError as e:
        logger.error(f"Failed to queue sync job: {e}")
        return False


def lambda_handler(event: Dict, context: Any) -> Dict:
    """
    Main Lambda handler for GitHub scheduler.

    Triggered by EventBridge hourly rule.
    """
    logger.info("GitHub scheduler invoked")

    # HA gating: passive region skips its tick. Single-region deploys
    # (no AppConfig env vars set) return True from is_active_region().
    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[github-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    # Log event details for debugging
    event_source = event.get("source", "unknown")
    event_detail_type = event.get("detail-type", "unknown")
    logger.info(f"Event source: {event_source}, type: {event_detail_type}")

    # Scan for repos to sync
    repos = scan_github_repos_for_sync()

    if not repos:
        logger.info("No repos found for sync")
        return {
            "statusCode": 200,
            "body": json.dumps({
                "repos_scanned": 0,
                "repos_queued": 0,
            }),
        }

    # Queue sync jobs.
    #
    # fable-scale chunk 08: parallel fan-out via ThreadPoolExecutor. The
    # prior serial loop truncated at enterprise repo counts (each
    # sqs.send_message is ~10ms + credential-provider warmup, so ~3000
    # repos hit the old 60s Lambda timeout). Workers default to 32
    # (GITHUB_SCHEDULER_PARALLEL_WORKERS env). Rollback: set env=1 to
    # force serial.
    workers = max(1, int(os.environ.get("GITHUB_SCHEDULER_PARALLEL_WORKERS", "32")))
    queued = 0
    failed = 0

    if workers == 1:
        # Backward-compat serial path (rollback via env).
        for repo in repos:
            if queue_sync_job(
                repo["workspace_id"],
                repo["repo_name"],
                repo["installation_id"],
            ):
                queued += 1
            else:
                failed += 1
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(
                lambda r: queue_sync_job(
                    r["workspace_id"], r["repo_name"], r["installation_id"],
                ),
                repos,
            )
            for ok in results:
                if ok:
                    queued += 1
                else:
                    failed += 1

    logger.info(
        f"Scheduler complete: {queued} queued, {failed} failed "
        f"(workers={workers}, repos={len(repos)})"
    )

    return {
        "statusCode": 200,
        "body": json.dumps({
            "repos_scanned": len(repos),
            "repos_queued": queued,
            "repos_failed": failed,
        }),
    }
