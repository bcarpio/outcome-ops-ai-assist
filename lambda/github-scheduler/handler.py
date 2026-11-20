# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
GitHub Scheduler Lambda for OutcomeOps AI Assist.

Triggered hourly by EventBridge to scan all workspace GitHub repos
and queue sync jobs for repos that have code-maps enabled.

Flow:
1. Scan DynamoDB for all GITHUB_REPO# items with include_code_maps=true
2. Grant at most GITHUB_SYNC_HEAL_MAX_PER_RUN heal slots to repos whose
   completed pipeline SHAs lag the dispatched SHA (oldest-eligible first)
3. Queue a sync job to github-sync SQS for each repo, with
   "heal_allowed": true on the ones granted a slot
4. Return summary of queued repos
"""

import json
import logging
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

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
# Max repos per hourly run allowed to heal (redo the gap between a completed
# SHA and the dispatched SHA). 0 disables healing entirely.
GITHUB_SYNC_HEAL_MAX_PER_RUN = int(os.environ.get("GITHUB_SYNC_HEAL_MAX_PER_RUN", "20"))

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
                        "repo_type": item.get("repo_type"),
                        "last_commit_sha": item.get("last_commit_sha"),
                        "last_docs_sha": item.get("last_docs_sha"),
                        "last_code_map_sha": item.get("last_code_map_sha"),
                        "heal_next_at": item.get("heal_next_at"),
                    })

        logger.info(f"Found {len(repos)} repos to sync")
        return repos

    except ClientError as e:
        logger.error(f"Failed to scan repos: {e}")
        return []


def _is_heal_candidate(repo: Dict, now: float) -> bool:
    """
    Decide from the repo row alone whether a scheduled sync may heal it.

    A candidate has a dispatched SHA (last_commit_sha) that a completed
    pipeline SHA has not reached: docs behind, or (for non-standards repos)
    code map behind. A completed column that is absent does not count as
    behind, so legacy rows never trigger a first sync from here. Stalled
    repos and repos still in heal backoff (heal_next_at in the future) are
    excluded.
    """
    last_commit_sha = repo.get("last_commit_sha")
    if not last_commit_sha:
        return False
    if repo.get("last_sync_status") == "stalled":
        return False

    heal_next_at = repo.get("heal_next_at")
    if heal_next_at is not None and float(heal_next_at) > now:
        return False

    docs_sha = repo.get("last_docs_sha")
    docs_behind = bool(docs_sha) and docs_sha != last_commit_sha

    code_map_sha = repo.get("last_code_map_sha")
    code_map_behind = (
        repo.get("repo_type") != "standards"
        and bool(code_map_sha)
        and code_map_sha != last_commit_sha
    )
    return docs_behind or code_map_behind


def _heal_candidates(repos: Iterable[Dict], now: float) -> List[Dict]:
    """Return heal candidates ordered oldest-eligible first.

    Order: heal_next_at ascending with absent first, then repo_name.
    """
    candidates = [r for r in repos if _is_heal_candidate(r, now)]

    def _sort_key(r: Dict) -> Tuple[int, float, str]:
        nxt: Optional[Any] = r.get("heal_next_at")
        if nxt is None:
            return (0, 0.0, r.get("repo_name") or "")
        return (1, float(nxt), r.get("repo_name") or "")

    candidates.sort(key=_sort_key)
    return candidates


def select_heal_slots(
    repos: Iterable[Dict], now: float, max_slots: int,
) -> Set[Tuple[str, str]]:
    """
    Pick which repos may heal this run.

    Args:
        repos: Repo dicts as returned by scan_github_repos_for_sync.
        now: Current time, epoch seconds (compared against heal_next_at).
        max_slots: Maximum slots to grant; 0 or less grants none.

    Returns:
        Set of (workspace_id, repo_name) granted a heal slot.
    """
    if max_slots <= 0:
        return set()
    return {
        (r["workspace_id"], r["repo_name"])
        for r in _heal_candidates(repos, now)[:max_slots]
    }


def queue_sync_job(
    workspace_id: str,
    repo_name: str,
    installation_id: str,
    heal_allowed: bool = False,
) -> bool:
    """Queue a sync job to the github-sync queue.

    "heal_allowed": true is added to the message only when this repo was
    granted a heal slot; absent means false to github-sync.
    """
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
        if heal_allowed:
            message["heal_allowed"] = True

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

    # Grant bounded heal slots, oldest-eligible first. Every repo is still
    # queued; only the granted ones may heal a lagging completed SHA.
    candidates = (
        _heal_candidates(repos, time.time())
        if GITHUB_SYNC_HEAL_MAX_PER_RUN > 0
        else []
    )
    heal_slots: Set[Tuple[str, str]] = {
        (r["workspace_id"], r["repo_name"])
        for r in candidates[:GITHUB_SYNC_HEAL_MAX_PER_RUN]
    }

    def _queue(r: Dict) -> bool:
        return queue_sync_job(
            r["workspace_id"],
            r["repo_name"],
            r["installation_id"],
            heal_allowed=(r["workspace_id"], r["repo_name"]) in heal_slots,
        )

    # Queue sync jobs.
    #
    # parallel fan-out via ThreadPoolExecutor. The
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
            if _queue(repo):
                queued += 1
            else:
                failed += 1
    else:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=workers) as executor:
            results = executor.map(_queue, repos)
            for ok in results:
                if ok:
                    queued += 1
                else:
                    failed += 1

    logger.info(
        f"[github-scheduler] queued {queued} repos, {len(heal_slots)} heal slots "
        f"granted ({len(candidates)} candidates)"
    )
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
