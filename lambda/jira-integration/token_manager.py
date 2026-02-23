"""
Token Manager for Jira OAuth tokens.

Handles secure storage and refresh of OAuth tokens using:
- DynamoDB for token storage
- KMS for encryption
- Rolling refresh token support (Atlassian-specific)

Reuses the same Atlassian OAuth app as Confluence (same client_id/secret).
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

# Atlassian OAuth endpoints
ATLASSIAN_TOKEN_URL = "https://auth.atlassian.com/oauth/token"

# Cached clients
_dynamodb = None
_kms = None
_ssm = None
_oauth_config = None


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


def _get_ssm():
    global _ssm
    if _ssm is None:
        _ssm = boto3.client("ssm")
    return _ssm


def _load_oauth_config() -> dict:
    """Load OAuth configuration from SSM (reuses Confluence OAuth app)."""
    global _oauth_config
    if _oauth_config is not None:
        return _oauth_config

    env = os.environ.get("ENV", "dev")
    app_name = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
    ssm = _get_ssm()
    prefix = f"/{env}/{app_name}/oauth/atlassian"

    try:
        client_id_response = ssm.get_parameter(Name=f"{prefix}/client-id")
        client_id = client_id_response["Parameter"]["Value"]

        client_secret_response = ssm.get_parameter(
            Name=f"{prefix}/client-secret",
            WithDecryption=True,
        )
        client_secret = client_secret_response["Parameter"]["Value"]

        callback_url_response = ssm.get_parameter(Name=f"{prefix}/jira-callback-url")
        callback_url = callback_url_response["Parameter"]["Value"]

        _oauth_config = {
            "client_id": client_id,
            "client_secret": client_secret,
            "callback_url": callback_url,
        }
        return _oauth_config
    except ClientError as e:
        logger.error(f"Failed to load OAuth config from SSM: {e}")
        raise


def get_oauth_config() -> dict:
    """Get OAuth configuration."""
    return _load_oauth_config()


def _encrypt_token(token: str) -> str:
    """
    Encrypt a token using KMS.

    Args:
        token: Plain text token

    Returns:
        Base64-encoded encrypted token
    """
    kms = _get_kms()
    response = kms.encrypt(
        KeyId=KMS_KEY_ARN,
        Plaintext=token.encode("utf-8"),
    )
    return base64.b64encode(response["CiphertextBlob"]).decode("utf-8")


def _decrypt_token(encrypted_token: str) -> str:
    """
    Decrypt a token using KMS.

    Args:
        encrypted_token: Base64-encoded encrypted token

    Returns:
        Plain text token
    """
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
    """
    Save OAuth tokens for a connection.

    Args:
        connection_id: Unique connection identifier
        access_token: OAuth access token
        refresh_token: OAuth refresh token
        expires_in: Token expiry in seconds

    Returns:
        True if successful
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + expires_in

    try:
        table.put_item(
            Item={
                "PK": f"JIRA_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
                "connection_id": connection_id,
                "encrypted_refresh_token": _encrypt_token(refresh_token),
                "expires_at": int(expires_at),
                "updated_at": now.isoformat(),
            }
        )
        logger.info(f"Saved tokens for connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to save tokens: {e}")
        return False


def get_refresh_token(connection_id: str) -> Optional[str]:
    """
    Get the refresh token for a connection.

    Args:
        connection_id: Connection identifier

    Returns:
        Decrypted refresh token or None
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        response = table.get_item(
            Key={
                "PK": f"JIRA_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        item = response.get("Item")
        if not item:
            logger.warning(f"No tokens found for connection {connection_id}")
            return None

        encrypted = item.get("encrypted_refresh_token")
        if not encrypted:
            return None

        return _decrypt_token(encrypted)
    except ClientError as e:
        logger.error(f"Failed to get refresh token: {e}")
        return None


def refresh_access_token(connection_id: str) -> Optional[str]:
    """
    Refresh the access token for a connection.

    IMPORTANT: Atlassian uses rolling refresh tokens. Each refresh returns
    a NEW refresh token that must be saved. The old token is invalidated.

    Args:
        connection_id: Connection identifier

    Returns:
        New access token or None if refresh failed
    """
    refresh_token = get_refresh_token(connection_id)
    if not refresh_token:
        logger.error(f"No refresh token found for connection {connection_id}")
        return None

    config = get_oauth_config()

    # Request new tokens
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "refresh_token": refresh_token,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            ATLASSIAN_TOKEN_URL,
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

        # CRITICAL: Save new refresh token (rolling tokens)
        if new_refresh_token and new_refresh_token != refresh_token:
            logger.info(f"Saving new rolling refresh token for {connection_id}")
            save_tokens(connection_id, new_access_token, new_refresh_token, expires_in)
        elif new_refresh_token:
            # Same token, just update timestamp
            save_tokens(connection_id, new_access_token, new_refresh_token, expires_in)

        return new_access_token

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"Token refresh failed: {e.code} - {error_body}")

        # Check for invalid_grant (token revoked)
        if "invalid_grant" in error_body:
            logger.warning(f"Refresh token revoked for connection {connection_id}")

        return None
    except urllib.error.URLError as e:
        logger.error(f"Network error during token refresh: {e.reason}")
        return None


def delete_tokens(connection_id: str) -> bool:
    """
    Delete tokens for a connection.

    Args:
        connection_id: Connection identifier

    Returns:
        True if successful
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(WORKSPACES_TABLE)

    try:
        table.delete_item(
            Key={
                "PK": f"JIRA_TOKEN#{connection_id}",
                "SK": "CREDENTIALS",
            }
        )
        logger.info(f"Deleted tokens for connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to delete tokens: {e}")
        return False


def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """
    Exchange authorization code for tokens.

    Args:
        code: Authorization code from OAuth callback
        redirect_uri: Redirect URI used in authorization

    Returns:
        Tuple of (access_token, refresh_token, expires_in) or (None, None, None)
    """
    config = get_oauth_config()

    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": config["client_id"],
        "client_secret": config["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            ATLASSIAN_TOKEN_URL,
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
