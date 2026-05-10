"""
Token Manager for Slack OAuth

Handles encryption/decryption of bot tokens using KMS. Slack bot tokens
(`xoxb-` prefix) **don't expire by default** — the workspace admin
either keeps them or uninstalls the app. There's no refresh-token dance.
refresh_access_token therefore returns the stored token directly with
no API round-trip; the function name preserves the cross-integration
contract used by file-ingestion's per-source bundled token managers.

Reuses the shared OAuth-token KMS key (aws_kms_key.gdrive_tokens) used
by every other workspace integration.
"""

import base64
import logging
import os
from datetime import datetime, timezone

import boto3

logger = logging.getLogger(__name__)

ENV = os.environ.get("ENV", "dev")
APP_NAME = os.environ.get("APP_NAME", "app.retrieveit.ai")
TENANTS_TABLE = os.environ.get("TENANTS_TABLE")
SLACK_KMS_KEY_ARN = os.environ.get("SLACK_KMS_KEY_ARN")
SLACK_CLIENT_ID = os.environ.get("SLACK_CLIENT_ID", "")

_kms_client = None
_ssm_client = None
_dynamodb = None

_slack_client_secret = None


def _get_kms_client():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _get_ssm_client():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_client_id() -> str:
    return SLACK_CLIENT_ID


def _get_client_secret() -> str:
    """Get Slack OAuth client secret from SSM."""
    global _slack_client_secret
    if _slack_client_secret is None:
        client = _get_ssm_client()
        param_name = f"/{ENV}/{APP_NAME}/secrets/slack/client_secret"
        response = client.get_parameter(Name=param_name, WithDecryption=True)
        _slack_client_secret = response["Parameter"]["Value"]
    return _slack_client_secret


def encrypt_token(bot_token: str) -> str:
    """Encrypt a bot token using KMS. Returns base64 ciphertext."""
    kms = _get_kms_client()
    response = kms.encrypt(
        KeyId=SLACK_KMS_KEY_ARN,
        Plaintext=bot_token.encode("utf-8"),
    )
    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a base64-encoded ciphertext blob to plaintext bot token."""
    kms = _get_kms_client()
    ciphertext = base64.b64decode(encrypted_token)
    response = kms.decrypt(
        KeyId=SLACK_KMS_KEY_ARN,
        CiphertextBlob=ciphertext,
    )
    return response["Plaintext"].decode("utf-8")


def refresh_access_token(connection_id: str) -> str:
    """Return the stored bot token for the given connection.

    Slack bot tokens don't expire under our v1 install model (no token
    rotation enabled), so this is a DynamoDB read + KMS decrypt — no
    Slack API call. Function name preserves the contract every other
    integration's token_manager exposes (file-ingestion calls it
    polymorphically).
    """
    table = _get_dynamodb().Table(TENANTS_TABLE)
    response = table.get_item(
        Key={
            "PK": f"SLACK_TOKEN#{connection_id}",
            "SK": "CREDENTIALS",
        }
    )
    item = response.get("Item")
    if not item:
        raise ValueError(f"No token found for connection_id {connection_id}")
    encrypted = item.get("encrypted_bot_token")
    if not encrypted:
        raise ValueError(f"No bot token for connection_id {connection_id}")
    return decrypt_token(encrypted)


def save_tokens(connection_id: str, bot_token: str) -> None:
    """Save encrypted bot token to DynamoDB."""
    encrypted = encrypt_token(bot_token)
    now = datetime.now(timezone.utc).isoformat()

    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.put_item(
        Item={
            "PK": f"SLACK_TOKEN#{connection_id}",
            "SK": "CREDENTIALS",
            "entity_type": "slack_token",
            "connection_id": connection_id,
            "encrypted_bot_token": encrypted,
            "created_at": now,
            "updated_at": now,
        }
    )
    logger.info(f"Saved Slack bot token for connection_id {connection_id}")


def delete_tokens(connection_id: str) -> None:
    """Delete bot token for a Slack connection."""
    table = _get_dynamodb().Table(TENANTS_TABLE)
    table.delete_item(
        Key={
            "PK": f"SLACK_TOKEN#{connection_id}",
            "SK": "CREDENTIALS",
        }
    )
    logger.info(f"Deleted Slack bot token for connection_id {connection_id}")
