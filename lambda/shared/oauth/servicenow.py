"""
Token manager for ServiceNow OAuth (per-workspace credentials -- ADR-044).

Unlike Atlassian/Microsoft/GitHub App OAuth, ServiceNow OAuth apps are
registered per-customer on each customer's own ServiceNow instance.
There is no env-level SSM param carrying the client_id/client_secret;
each connection stores its own credentials in DynamoDB (client_id and
instance_url on the connection row, client_secret KMS-encrypted on the
token row).

Ported from ``../app.retrieveit.ai/lambda/servicenow-integration/
servicenow_token_manager.py`` and adapted to OutcomeOps conventions:
- Uses ``WORKSPACES_TABLE`` (not ``TENANTS_TABLE``).
- Encrypts with the shared ``OAUTH_KMS_KEY_ARN`` (same key that wraps
  Atlassian / Microsoft refresh tokens).
- HTTP via ``urllib.request`` with ``timeout=30`` (chunk 14 convention).
- Lives under ``shared/oauth/`` from day one (chunk 16 pattern -- no
  per-Lambda copy).

DynamoDB shape:

- Connection row (per workspace): PK=``WS#{ws}`` SK=``SERVICENOW_CONNECTION#{conn}``
  fields: ``instance_url``, ``client_id``, ``last_sync_status``,
  ``servicenow_user_id``, ``servicenow_user_login``, ``config.cursor``
- Token row (per connection): PK=``SERVICENOW_TOKEN#{conn}`` SK=``CREDENTIALS``
  fields: ``encrypted_client_secret``, ``encrypted_refresh_token``,
  ``entity_type=servicenow_token`` (for the GSI the scheduler queries)
"""

from __future__ import annotations

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

logger = logging.getLogger(__name__)


WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
OAUTH_KMS_KEY_ARN = os.environ.get("OAUTH_KMS_KEY_ARN")


_kms_client = None
_dynamodb = None


def _get_kms_client():
    global _kms_client
    if _kms_client is None:
        _kms_client = boto3.client("kms")
    return _kms_client


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _encrypt(plaintext: str) -> str:
    """KMS-encrypt a plaintext string; return base64 ciphertext."""
    resp = _get_kms_client().encrypt(
        KeyId=OAUTH_KMS_KEY_ARN,
        Plaintext=plaintext.encode("utf-8"),
    )
    return base64.b64encode(resp["CiphertextBlob"]).decode("utf-8")


def _decrypt(ciphertext_b64: str) -> str:
    """Reverse of _encrypt."""
    resp = _get_kms_client().decrypt(
        KeyId=OAUTH_KMS_KEY_ARN,
        CiphertextBlob=base64.b64decode(ciphertext_b64),
    )
    return resp["Plaintext"].decode("utf-8")


def _post_token(url: str, form: dict) -> Tuple[int, dict]:
    """POST ``form`` as x-www-form-urlencoded to ``url``; return (status, body)."""
    data = urllib.parse.urlencode(form).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "outcome-ops-ai-assist/servicenow-oauth",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = json.loads(response.read().decode("utf-8"))
            return response.getcode(), body
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = {}
        return e.code, body


# ============================================================================
# DDB row shape helpers
# ============================================================================


def get_credentials_from_db(workspace_id: str, connection_id: str) -> Tuple[str, str, str]:
    """Read connection + token rows, return (instance_url, client_id, client_secret).

    Direct GetItem on both rows -- never Scan. Raises ValueError if either
    row is missing or if the encrypted client_secret is absent.
    """
    table = _get_dynamodb().Table(WORKSPACES_TABLE)

    token_row = table.get_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    ).get("Item")
    if not token_row:
        raise ValueError(f"No token row for connection_id {connection_id}")
    encrypted_secret = token_row.get("encrypted_client_secret")
    if not encrypted_secret:
        raise ValueError(f"Missing client_secret for connection_id {connection_id}")
    client_secret = _decrypt(encrypted_secret)

    conn = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"SERVICENOW_CONNECTION#{connection_id}",
        }
    ).get("Item")
    if not conn:
        raise ValueError(
            f"No connection row for workspace_id={workspace_id} connection_id={connection_id}"
        )
    return conn["instance_url"], conn["client_id"], client_secret


def save_client_secret(connection_id: str, client_secret: str) -> None:
    """Upsert the encrypted client_secret on the token row.

    Called by the setup-form submission BEFORE OAuth has happened. Does
    NOT touch encrypted_refresh_token.
    """
    encrypted = _encrypt(client_secret)
    now = datetime.now(timezone.utc).isoformat()
    _get_dynamodb().Table(WORKSPACES_TABLE).update_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"},
        UpdateExpression=(
            "SET entity_type = :et, connection_id = :cid, "
            "encrypted_client_secret = :cs, updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={
            ":et": "servicenow_token",
            ":cid": connection_id,
            ":cs": encrypted,
            ":now": now,
        },
    )
    logger.info(f"Saved ServiceNow client_secret for connection_id {connection_id}")


def save_refresh_token(connection_id: str, refresh_token: str) -> None:
    """Upsert the encrypted refresh_token on the token row.

    Does NOT touch encrypted_client_secret. Called from the OAuth callback
    AND on refresh-token rotation (newer ServiceNow instances rotate).
    """
    encrypted = _encrypt(refresh_token)
    now = datetime.now(timezone.utc).isoformat()
    _get_dynamodb().Table(WORKSPACES_TABLE).update_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"},
        UpdateExpression=(
            "SET entity_type = :et, connection_id = :cid, "
            "encrypted_refresh_token = :rt, updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={
            ":et": "servicenow_token",
            ":cid": connection_id,
            ":rt": encrypted,
            ":now": now,
        },
    )
    logger.info(f"Saved ServiceNow refresh_token for connection_id {connection_id}")


def delete_tokens(connection_id: str) -> None:
    """Delete the token row (removes both client_secret and refresh_token)."""
    _get_dynamodb().Table(WORKSPACES_TABLE).delete_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    )
    logger.info(f"Deleted ServiceNow tokens for connection_id {connection_id}")


# ============================================================================
# OAuth exchange + refresh
# ============================================================================


def exchange_code_for_tokens(
    workspace_id: str,
    connection_id: str,
    code: str,
    redirect_uri: str,
) -> Tuple[str, str, int]:
    """Exchange an authorization code for tokens against the customer's
    ServiceNow instance.

    Credentials are pulled from DDB (the setup form persists client_id +
    client_secret + instance_url BEFORE the OAuth click).

    Returns (access_token, refresh_token, expires_in_seconds).
    Raises ValueError on exchange failure.
    """
    instance_url, client_id, client_secret = get_credentials_from_db(
        workspace_id, connection_id
    )
    status, body = _post_token(
        f"{instance_url}/oauth_token.do",
        {
            "code": code,
            "grant_type": "authorization_code",
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
        },
    )
    if status != 200:
        logger.error(
            f"ServiceNow token exchange failed (status {status}): {body}"
        )
        raise ValueError(f"Token exchange failed: {status}")
    return (
        body["access_token"],
        body.get("refresh_token") or "",
        int(body.get("expires_in", 1800)),
    )


def refresh_access_token(workspace_id: str, connection_id: str) -> str:
    """Get a fresh access token for a ServiceNow connection.

    On ``invalid_grant`` the connection is marked ``last_sync_status =
    reauth_required`` and a ValueError is raised. Callers treat ValueError
    as a 401-equivalent (no retry, user must reconnect).

    Newer ServiceNow instances rotate the refresh token on every refresh;
    the new one is persisted transparently. Older instances may return the
    same value or omit it -- both cases are no-ops.
    """
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    token_item = table.get_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    ).get("Item")
    if not token_item:
        raise ValueError(f"No token row for connection_id {connection_id}")
    encrypted_refresh = token_item.get("encrypted_refresh_token")
    if not encrypted_refresh:
        raise ValueError(f"No refresh_token for connection_id {connection_id}")
    refresh_token = _decrypt(encrypted_refresh)

    instance_url, client_id, client_secret = get_credentials_from_db(
        workspace_id, connection_id
    )
    status, body = _post_token(
        f"{instance_url}/oauth_token.do",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if status != 200:
        logger.error(f"ServiceNow token refresh failed (status {status}): {body}")
        if body.get("error") == "invalid_grant":
            _mark_connection_reauth_required(workspace_id, connection_id)
            raise ValueError("Token expired; user must re-authenticate")
        raise ValueError(f"Token refresh failed: {status}")

    access_token = body["access_token"]
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        logger.info(f"Persisting rotated refresh_token for {connection_id}")
        save_refresh_token(connection_id, new_refresh)
    return access_token


def _mark_connection_reauth_required(workspace_id: str, connection_id: str) -> None:
    """Flip the connection row's ``last_sync_status`` to reauth_required.

    Direct UpdateItem on the WS#/SERVICENOW_CONNECTION# key -- no Scan,
    no FilterExpression, no chance of missing the row on a large table.
    """
    try:
        _get_dynamodb().Table(WORKSPACES_TABLE).update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"SERVICENOW_CONNECTION#{connection_id}",
            },
            UpdateExpression=(
                "SET last_sync_status = :s, last_sync_error = :e, updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":s": "reauth_required",
                ":e": "Refresh token rejected by ServiceNow; reauthentication required",
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.exception(
            f"Failed to mark connection {connection_id} reauth_required "
            f"(workspace_id={workspace_id})"
        )


def build_authorize_url(
    instance_url: str,
    client_id: str,
    redirect_uri: str,
    state: str,
) -> str:
    """Build the OAuth authorize URL for a ServiceNow instance.

    ServiceNow uses ``/oauth_auth.do`` (not the standard /authorize path)
    and the KB scope is implicit in the OAuth app's registration (there's
    no ``scope=`` param on the authorize URL).
    """
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{instance_url}/oauth_auth.do?{params}"
