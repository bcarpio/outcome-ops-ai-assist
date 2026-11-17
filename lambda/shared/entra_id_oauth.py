# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""shared.entra_id_oauth — single source of truth for Entra ID OAuth.

Replaces the drifted per-integration ``token_manager.py`` copies that
``outlook-integration``, ``teams-integration``, ``onedrive-integration``,
``onenote-integration``, and ``sharepoint-integration`` each carried.
A sixth consumer (``azure-devops-integration``, in flight) joins this
module on first ship instead of starting yet another drifted copy.

What this module owns (the parts that don't vary across integrations):
- HTTPS POST to Microsoft's token endpoint
- KMS encrypt/decrypt of the refresh token at rest
- DynamoDB read/write of the ``CREDENTIALS`` row keyed by the caller's
  ``pk_prefix``
- The refresh-rotation correctness contract (new refresh_token from
  Microsoft replaces the prior one)
- urllib error → ``None``/``False`` return mapping (caller-side
  no-throw contract, matching the legacy per-integration behavior)

What the caller owns (the parts that DO vary per integration):
- The PK prefix (``OUTLOOK_TOKEN#``, ``TEAMS_TOKEN#``,
  ``ONEDRIVE_TOKEN#``, ``ONENOTE_TOKEN#``, ``SHAREPOINT_TOKEN#``,
  ``AZDO_TOKEN#``)
- The OAuth scope string (Microsoft-Graph-flavored for the 5 existing
  integrations; ``499b84ac-…/.default`` for Azure DevOps)
- The tenant ID — defaults to ``"common"`` (the multi-tenant
  authority the Microsoft-Graph integrations use today); Azure DevOps
  passes the customer's specific tenant per ADR-035-style
  single-tenant-per-customer model
- The ``WORKSPACES_TABLE`` and ``OAUTH_KMS_KEY_ARN`` env var values
  (passed as args rather than read here so this module is
  re-usable in tests without env-var fixture noise)

Reconciled deltas (canonical choices documented for future readers):
- **Docstring style**: One-liners. The 4 newer copies
  (teams/onedrive/onenote/sharepoint) used one-liners; outlook had
  long Args/Returns blocks. The shared helper's contract is
  documented HERE; per-function docs stay terse.
- **Logger messages**: Drop the integration-name qualifier. The 4
  newer copies wrote "for X connection {id}"; outlook wrote "for
  connection {id}". Logger context already carries the function
  name + the connection_id is globally unique. Dropping the X is
  cleaner and matches the older outlook style; if log searchability
  by integration is needed, callers can wrap the logger.
- **Scope source**: Caller passes a ``scopes`` argument. The 4 newer
  copies referenced a module-level ``*_SCOPES`` constant; outlook
  hardcoded the scope string inline in two functions. Both shapes
  pre-supposed the integration. Promoting it to a parameter is the
  only way the shared helper can serve 6 different scope sets.
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


_MICROSOFT_TOKEN_URL_TEMPLATE = (
    "https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"
)


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


# ============================================================================
# Internal helpers (KMS encrypt/decrypt the refresh token at rest)
# ============================================================================


def _encrypt_token(token: str, kms_key_arn: str) -> str:
    """Encrypt a token using KMS."""
    kms = _get_kms()
    response = kms.encrypt(
        KeyId=kms_key_arn,
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


# Public wrappers so provider wrappers (e.g. shared.oauth.gitlab) can cache
# an extra secret (the short-lived access token) in the same CREDENTIALS
# row using the identical KMS-at-rest logic, without reaching into the
# leading-underscore internals. Additive; no behavior change for existing
# consumers.
def encrypt_token(token: str, kms_key_arn: str) -> str:
    """KMS-encrypt a token to a base64 ciphertext string."""
    return _encrypt_token(token, kms_key_arn)


def decrypt_token(encrypted_token: str) -> str:
    """Decrypt a base64 ciphertext string produced by ``encrypt_token``."""
    return _decrypt_token(encrypted_token)


# ============================================================================
# Public surface (the 5 functions every consuming wrapper calls into)
# ============================================================================


def save_tokens(
    workspaces_table: str,
    kms_key_arn: str,
    pk_prefix: str,
    connection_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int = 3600,
) -> bool:
    """Save OAuth tokens for a connection.

    ``access_token`` is accepted for API-shape continuity with the legacy
    per-integration token_managers but is NOT persisted — the design
    only stores the encrypted refresh_token + expires_at, and short-
    lived access tokens are obtained on demand via refresh.
    """
    del access_token  # legacy API shape; intentionally unused
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(workspaces_table)

    now = datetime.now(timezone.utc)
    expires_at = now.timestamp() + expires_in

    try:
        table.put_item(
            Item={
                "PK": f"{pk_prefix}{connection_id}",
                "SK": "CREDENTIALS",
                "connection_id": connection_id,
                "encrypted_refresh_token": _encrypt_token(refresh_token, kms_key_arn),
                "expires_at": int(expires_at),
                "updated_at": now.isoformat(),
            }
        )
        logger.info(f"Saved tokens for connection {connection_id}")
        return True
    except ClientError as e:
        logger.error(f"Failed to save tokens: {e}")
        return False


def get_refresh_token(
    workspaces_table: str,
    pk_prefix: str,
    connection_id: str,
) -> Optional[str]:
    """Get the refresh token for a connection (decrypted via KMS).

    Returns None when the credentials row is missing, the encrypted_
    refresh_token attribute is absent, or DDB errors. Callers treat
    None as "needs re-auth"; the legacy behavior is preserved exactly.
    """
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(workspaces_table)

    try:
        response = table.get_item(
            Key={
                "PK": f"{pk_prefix}{connection_id}",
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


def refresh_access_token(
    workspaces_table: str,
    kms_key_arn: str,
    pk_prefix: str,
    connection_id: str,
    client_id: str,
    client_secret: str,
    scopes: str,
    tenant_id: str = "common",
) -> Optional[str]:
    """Refresh the access token for a connection.

    Exchanges the stored refresh_token at Microsoft's token endpoint
    and, when a new refresh_token comes back, calls ``save_tokens`` to
    persist the rotation. Returns the new access_token, or None on
    any failure (missing refresh_token, HTTP/network error, no
    access_token in the response). On ``invalid_grant`` the helper
    logs a warning marking the refresh token as revoked but still
    returns None — caller decides whether to surface that to the user.
    """
    refresh_token = get_refresh_token(workspaces_table, pk_prefix, connection_id)
    if not refresh_token:
        logger.error(f"No refresh token found for connection {connection_id}")
        return None

    token_url = _MICROSOFT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "scope": scopes,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            token_url,
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
            save_tokens(
                workspaces_table=workspaces_table,
                kms_key_arn=kms_key_arn,
                pk_prefix=pk_prefix,
                connection_id=connection_id,
                access_token=new_access_token,
                refresh_token=new_refresh_token,
                expires_in=expires_in,
            )

        return new_access_token

    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"Token refresh failed: {e.code} - {error_body}")

        if "invalid_grant" in error_body:
            logger.warning(f"Refresh token revoked for connection {connection_id}")

        return None
    except urllib.error.URLError as e:
        logger.error(f"Network error during token refresh: {e.reason}")
        return None


def delete_tokens(
    workspaces_table: str,
    pk_prefix: str,
    connection_id: str,
) -> bool:
    """Delete the tokens row for a connection."""
    dynamodb = _get_dynamodb()
    table = dynamodb.Table(workspaces_table)

    try:
        table.delete_item(
            Key={
                "PK": f"{pk_prefix}{connection_id}",
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
    client_id: str,
    client_secret: str,
    redirect_uri: str,
    scopes: str,
    tenant_id: str = "common",
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Exchange an authorization code for (access_token, refresh_token, expires_in).

    Returns (None, None, None) on any failure (HTTP/network error or
    a missing field in Microsoft's response). Caller decides how to
    surface the failure (redirect to error page, log + retry, etc.).
    """
    token_url = _MICROSOFT_TOKEN_URL_TEMPLATE.format(tenant_id=tenant_id)
    data = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri,
        "scope": scopes,
    }).encode("utf-8")

    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
    }

    try:
        request = urllib.request.Request(
            token_url,
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
