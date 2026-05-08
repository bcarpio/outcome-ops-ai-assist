"""
Audit Stream Publisher - DynamoDB Streams consumer that re-emits audit-log
INSERT events into a Kinesis Data Stream as OCSF v1.3.0 envelopes.

This is the platform's SIEM-export hook. Customers' security teams point
their own consumer (Splunk, Datadog, Sumo, AWS Security Lake, a Firehose
to S3, a Lambda to anywhere they want) at the Kinesis stream we publish
to. They decide which events they want via FilterCriteria on their
consumer event source mapping - we publish everything, they filter on
consumption.

The transformer keeps the OCSF wire format stable: every row gets a
proper category_uid / class_uid / activity_id / severity_id, the actor
and src_endpoint structures are populated from our user_email / source_ip
/ user_agent, and the entire raw payload is preserved under ``unmapped``.

See lambda/shared/ocsf_mapper.py for the mapping logic.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
from typing import Any, Dict, List

import boto3
from botocore.exceptions import ClientError

# shared/ is bundled by terraform via a second source_path block.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "shared"))

from ocsf_mapper import map_audit_row_to_ocsf, partition_key_for  # noqa: E402

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

STREAM_NAME = os.environ.get("AUDIT_EXPORT_STREAM_NAME", "")

# Kinesis PutRecords limit: 500 records or 5 MB per call. We batch-by-25 from
# the DDB stream side to match the analytics-aggregator config, so we never
# come close. Per-record limit is 1 MB; OCSF envelope + unmapped is well under.
MAX_RETRIES = 3
RETRY_BASE_DELAY = 0.2  # seconds; exponential backoff

_kinesis = None


def _client():
    global _kinesis
    if _kinesis is None:
        _kinesis = boto3.client("kinesis")
    return _kinesis


def handler(event: Dict[str, Any], context: Any) -> Dict[str, Any]:
    """DynamoDB Streams batch handler. Returns the AWS Lambda partial-batch
    response shape so failed records get retried while successful ones are
    not redelivered."""
    if not STREAM_NAME:
        logger.error("AUDIT_EXPORT_STREAM_NAME unset - dropping batch")
        return {"batchItemFailures": []}

    records = event.get("Records", [])
    failures: List[Dict[str, str]] = []
    kinesis_records: List[Dict[str, Any]] = []
    record_id_by_index: List[str] = []

    for rec in records:
        if rec.get("eventName") != "INSERT":
            continue
        image = (rec.get("dynamodb") or {}).get("NewImage")
        if not image:
            continue
        try:
            ocsf = map_audit_row_to_ocsf(image)
            kinesis_records.append(
                {
                    "Data": json.dumps(ocsf, default=str).encode("utf-8"),
                    "PartitionKey": partition_key_for(image),
                }
            )
            record_id_by_index.append(rec.get("eventID", ""))
        except Exception:
            logger.exception(
                "Failed to map audit row to OCSF; eventID=%s",
                rec.get("eventID"),
            )
            failures.append({"itemIdentifier": rec.get("eventID", "")})

    if not kinesis_records:
        return {"batchItemFailures": failures}

    # PutRecords with retry on partial failures. Kinesis returns a per-record
    # ErrorCode field; we keep retrying just the failed ones up to MAX_RETRIES.
    pending = list(zip(kinesis_records, record_id_by_index))
    for attempt in range(MAX_RETRIES):
        try:
            resp = _client().put_records(
                StreamName=STREAM_NAME,
                Records=[r for r, _ in pending],
            )
        except ClientError:
            logger.exception(
                "Kinesis PutRecords failed entirely (attempt %d)", attempt
            )
            if attempt == MAX_RETRIES - 1:
                failures.extend(
                    {"itemIdentifier": rid} for _, rid in pending if rid
                )
                break
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))
            continue

        failed_count = resp.get("FailedRecordCount", 0)
        if failed_count == 0:
            break

        next_pending = []
        for (rec, rid), result in zip(pending, resp.get("Records", [])):
            if result.get("ErrorCode"):
                next_pending.append((rec, rid))
        pending = next_pending
        if not pending:
            break
        if attempt == MAX_RETRIES - 1:
            failures.extend(
                {"itemIdentifier": rid} for _, rid in pending if rid
            )
        else:
            time.sleep(RETRY_BASE_DELAY * (2 ** attempt))

    return {"batchItemFailures": failures}
