"""
Token Manager for Microsoft Teams OAuth tokens.

Handles secure storage and refresh of OAuth tokens using:
- DynamoDB for token storage
- KMS for encryption
- Microsoft identity platform token endpoint
"""

import base64
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Optional, Tuple

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Environment variables
WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KMS_KEY_ARN = os.environ.get("OAUTH_KMS_KEY_ARN")

# Microsoft OAuth endpoints
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/common/oauth2/v2.0/token"

# Teams OAuth scopes
TEAMS_SCOPES = "Team.ReadBasic.All Channel.ReadBasic.All ChannelMessage.Read.All Files.Read.All offline_access openid profile email"

# Cached clients
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


def _encrypt_token(token: str) -> str:
    """Encrypt a token using KMS."""
    kms = _get_kms()
    response = kms.encrypt(
        KeyId=KMS_KEY_ARN,
        Plaintext=token.encode("utf-8"),
    )
    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")


def _decrypt_token(encrypted_token: str) -> str:
    """Decrypt a token using KMS."""
    kms = _get_kms()
    response = kms.decrypt(
        CiphertextBlob=base64.b64decode(encrypted_token),
    )
    return response["Plaintext"].decode("utf-8")


def save_tokens(
    connection_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int = 3600,
) -> bool:
    """Save OAuth tokens for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + expires_in

    try:
        table.put_item(
            Item={
                "PK": f"TEAMS_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
                "connection_id": connection_id,
                "encrypted_refresh_token": _encrypt_token(refresh_token),
                "expires_at": int(expires_at),
                "updated_at": now.isoformat(),
            }
        )
        logger.info(f"Saved tokens for Teams connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to save tokens: {e}")
        return False


def get_refresh_token(connection_id: str) -> Optional[str]:
    """Get the refresh token for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        response = table.get_item(
            Key={
                "PK": f"TEAMS_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        item = response.get("Item")
        if not item:
            logger.warning(f"No tokens found for Teams connection {connection_id}")
            return None

        encrypted = item.get("encrypted_refresh_token")
        if not encrypted:
            return None

        return _decrypt_token(encrypted)
    except ClientError as e:
        logger.error(f"Failed to get refresh token: {e}")
        return None


def refresh_access_token(
    connection_id: str,
    client_id: str,
    client_secret: str,
) -> Optional[str]:
    """Refresh the access token for a connection."""
    refresh_token = get_refresh_token(connection_id)
    if not refresh_token:
        logger.error(f"No refresh token found for Teams connection {connection_id}")
        return None

    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": TEAMS_SCOPES,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            MICROSOFT_TOKEN_URL,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            token_data = json.loads(response.read().decode("utf-8"))

        new_access_token = token_data.get("access_token")
        new_refresh_token = token_data.get("refresh_token")
        expires_in = token_data.get("expires_in", 3600)

        if not new_access_token:
            logger.error("No access token in refresh response")
            return None

        if new_refresh_token:
            save_tokens(connection_id, new_access_token, new_refresh_token, expires_in)

        return new_access_token

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"Token refresh failed: {e.code} - {error_body}")

        if "invalid_grant" in error_body:
            logger.warning(f"Refresh token revoked for Teams connection {connection_id}")

        return None
    except urllib.error.URLError as e:
        logger.error(f"Network error during token refresh: {e.reason}")
        return None


def delete_tokens(connection_id: str) -> bool:
    """Delete tokens for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        table.delete_item(
            Key={
                "PK": f"TEAMS_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        logger.info(f"Deleted tokens for Teams connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to delete tokens: {e}")
        return False


def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Exchange authorization code for tokens."""
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": TEAMS_SCOPES,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            MICROSOFT_TOKEN_URL,
            data=data,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            token_data = json.loads(response.read().decode("utf-8"))

        return (
            token_data.get("access_token"),
            token_data.get("refresh_token"),
            token_data.get("expires_in", 3600),
        )

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"Token exchange failed: {e.code} - {error_body}")
        return None, None, None
    except urllib.error.URLError as e:
        logger.error(f"Network error during token exchange: {e.reason}")
        return None, None, None
