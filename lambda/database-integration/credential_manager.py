# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Credential Manager for database connections.

Mirrors the OneNote token_manager pattern but stores an engine-specific
credential payload (dict) instead of an OAuth refresh token. The payload
is opaque to this module — callers (database-sync) treat it as JSON
that gets forwarded into the schema introspection engine's connector
config for the target engine.

Storage:
    PK = DB_CREDENTIAL#{connection_id}
    SK = CREDENTIALS
    encrypted_payload   (KMS-encrypted JSON blob)
    updated_at          (ISO timestamp)

Payload shapes (engine-specific; this module doesn't validate):
    postgres / mysql / mssql:
        {"username": "...", "password": "...", "ssl_mode": "..." | None,
         "extra_params": {...optional driver kwargs...}}
    snowflake (future):
        {"username": "...", "private_key_pem": "...", "account": "..."}
    bigquery (future):
        {"service_account_json": "..."}

Why this lives outside Secrets Manager — DDB+KMS keeps every credential
access logged through the unified audit table (DB_CREDENTIAL_USED rows)
rather than splitting forensic evidence between audit + CloudTrail.
"""

import base64
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KMS_KEY_ARN = os.environ.get("DB_CREDENTIAL_KMS_KEY_ARN")

_dynamodb = None
_kms = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_kms():
    global _kms
    if _kms is None:
        _kms = boto3.client("kms")
    return _kms


def _encrypt_payload(payload: Dict[str, Any]) -> str:
    kms = _get_kms()
    plaintext = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    response = kms.encrypt(KeyId=KMS_KEY_ARN, Plaintext=plaintext)
    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")


def _decrypt_payload(encrypted: str) -> Dict[str, Any]:
    kms = _get_kms()
    response = kms.decrypt(CiphertextBlob=base64.b64decode(encrypted))
    return json.loads(response["Plaintext"].decode("utf-8"))


def save_credential(connection_id: str, payload: Dict[str, Any]) -> bool:
    """Encrypt + persist a credential payload for a DB connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    now = datetime.now(timezone.utc).isoformat()
    try:
        table.put_item(
            Item={
                "PK": f"DB_CREDENTIAL#{connection_id}",
                "SK": "CREDENTIALS",
                "connection_id": connection_id,
                "encrypted_payload": _encrypt_payload(payload),
                "updated_at": now,
            }
        )
        logger.info(f"Saved credential for database connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to save credential: {e}")
        return False


def get_credential(connection_id: str) -> Optional[Dict[str, Any]]:
    """Fetch + decrypt a credential payload. None if not found."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        response = table.get_item(
            Key={
                "PK": f"DB_CREDENTIAL#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        item = response.get("Item")
        if not item:
            logger.warning(f"No credential found for database connection {connection_id}")
            return None

        encrypted = item.get("encrypted_payload")
        if not encrypted:
            return None

        return _decrypt_payload(encrypted)
    except ClientError as e:
        logger.error(f"Failed to get credential: {e}")
        return None


def delete_credential(connection_id: str) -> bool:
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        table.delete_item(
            Key={
                "PK": f"DB_CREDENTIAL#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        logger.info(f"Deleted credential for database connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to delete credential: {e}")
        return False
