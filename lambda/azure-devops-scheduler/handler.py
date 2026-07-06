"""
Azure DevOps Sync Scheduler Lambda.

Triggered hourly by EventBridge. Scans WORKSPACES_TABLE for every
enabled AZDO_REPO# row and enqueues one sync job per repo. The
sync worker (azure-devops-sync) consumes the queue and does the
actual API calls.

Behavior notes worth keeping in mind when editing:

1. **Active/passive aware.** Both regions get the same hourly
   EventBridge rule under the multi-region pattern. The active
   region does the work; the passive region returns immediately
   after the ``is_active_region()`` short-circuit. No DDB scan, no
   SQS enqueue. Worst case during a flip mid-tick is a few seconds
   of dual scheduling -- acceptable.

2. **Paginated scan.** A workspace with many AZDO_REPO# rows can
   exceed DDB's default 1MB scan-page boundary. Per
   ``feedback_enterprise_scale_pagination``, we loop on
   ``LastEvaluatedKey`` until exhausted -- pagination is inline so
   the contract is visible at the call site.

3. **Filter is applied client-side.** We use ``FilterExpression`` on
   ``begins_with(SK, "AZDO_REPO#")`` to narrow the scan, but the
   ``enabled`` flag is checked in Python because the scan would
   otherwise need ``ExpressionAttributeNames`` for the reserved
   word and we'd rather pay the filter-skip than the readability
   cost.
"""

import json
import logging
import os
import uuid
from typing import Any, Dict, Iterator, List

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
AZURE_DEVOPS_SYNC_QUEUE = os.environ.get("AZURE_DEVOPS_SYNC_QUEUE")


dynamodb = boto3.resource("dynamodb", config=_BOTO_CONFIG)
sqs = boto3.client("sqs", config=_BOTO_CONFIG)


def _scan_azdo_repos() -> Iterator[Dict[str, Any]]:
    """Yield every AZDO_REPO# row, paginating through all DDB scan pages.

    The scan-page-size + 1MB page boundary makes single-shot scan calls
    silently truncate on enterprise-scale workspaces. Loop on
    ``LastEvaluatedKey`` until the table is exhausted; pagination
    must NOT be hidden in a shared helper -- per
    ``feedback_enterprise_scale_pagination`` it stays inline so the
    next reader can confirm at a glance that we don't quietly drop
    a page.
    """
    table = dynamodb.Table(WORKSPACES_TABLE)
    scan_kwargs: Dict[str, Any] = {
        "FilterExpression": "begins_with(SK, :sk_prefix)",
        "ExpressionAttributeValues": {":sk_prefix": "AZDO_REPO#"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            yield item
        cursor = response.get("LastEvaluatedKey")
        if not cursor:
            return
        scan_kwargs["ExclusiveStartKey"] = cursor


def _queue_sync_job(repo: Dict[str, Any]) -> bool:
    """Send one sync job to the ADO sync queue. Returns True on success."""
    pk = repo.get("PK", "")
    workspace_id = pk[3:] if pk.startswith("WS#") else repo.get("workspace_id")
    slug = repo.get("slug")
    connection_id = repo.get("connection_id")
    org = repo.get("org")
    project = repo.get("project")
    repo_name = repo.get("repo")
    if not (workspace_id and slug and connection_id):
        logger.warning("Skipping malformed AZDO_REPO row: %s", repo)
        return False

    message = {
        "workspace_id": workspace_id,
        "connection_id": connection_id,
        "slug": slug,
        "org": org,
        "project": project,
        "repo": repo_name,
        "triggered_by": "scheduler",
        "sync_id": str(uuid.uuid4()),
    }
    try:
        sqs.send_message(
            QueueUrl=AZURE_DEVOPS_SYNC_QUEUE,
            MessageBody=json.dumps(message),
        )
        logger.info("Queued ADO sync for %s/%s", workspace_id, slug)
        return True
    except ClientError as e:
        logger.error("Failed to queue ADO sync for %s/%s: %s", workspace_id, slug, e)
        return False


def lambda_handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """EventBridge entry point. Returns a summary dict for observability."""
    logger.info("Azure DevOps scheduler invoked")

    from shared.active_region import is_active_region  # type: ignore

    if not is_active_region():
        logger.info("[azure-devops-scheduler] region is passive; skipping this tick")
        return {"statusCode": 200, "body": "passive-region-skip"}

    if not AZURE_DEVOPS_SYNC_QUEUE:
        logger.error("AZURE_DEVOPS_SYNC_QUEUE not configured")
        return {"statusCode": 500, "body": "Queue not configured"}

    scanned = 0
    queued = 0
    skipped_disabled = 0

    for item in _scan_azdo_repos():
        scanned += 1
        if not item.get("enabled", True):
            skipped_disabled += 1
            continue
        if _queue_sync_job(item):
            queued += 1

    logger.info(
        "ADO scheduler tick: scanned=%d queued=%d skipped_disabled=%d",
        scanned, queued, skipped_disabled,
    )
    return {
        "statusCode": 200,
        "body": json.dumps({
            "scanned": scanned,
            "queued": queued,
            "skipped_disabled": skipped_disabled,
        }),
    }
