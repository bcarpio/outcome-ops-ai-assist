"""
Token manager for ServiceNow OAuth (env-level SSM credentials).

Follows the same shape as ``shared.oauth`` peers: one OAuth app per
deploy, one instance URL per deploy. The deploy operator populates
``servicenow_instance_url`` and ``servicenow_client_id`` in tfvars and
``/{env}/{app}/oauth/servicenow/client-secret`` in SSM SecureString.
Every workspace connecting ServiceNow authorizes against that single
Application Registry entry on the customer's single ServiceNow
instance.

DynamoDB shape:

- Connection row (per workspace): PK=``WS#{ws}`` SK=``SERVICENOW_CONNECTION#{conn}``
  fields: ``last_sync_status``, ``servicenow_user_id``,
  ``servicenow_user_login``, ``config.cursor``. NO ``instance_url``,
  NO ``client_id``, NO ``encrypted_client_secret`` -- those are env-scoped.
- Token row (per connection): PK=``SERVICENOW_TOKEN#{conn}`` SK=``CREDENTIALS``
  fields: ``encrypted_refresh_token`` only. ``entity_type=servicenow_token``
  for the GSI the scheduler queries.
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
SERVICENOW_INSTANCE_URL = os.environ.get("SERVICENOW_INSTANCE_URL", "").rstrip("/")
SERVICENOW_CLIENT_ID = os.environ.get("SERVICENOW_CLIENT_ID", "")
SERVICENOW_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "SERVICENOW_CLIENT_SECRET_SSM_PARAM", ""
)


_kms_client = None
_dynamodb = None
_ssm_client = None
_client_secret_cache: Optional[str] = None


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


def _get_ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _load_client_secret() -> str:
    """Fetch + cache the ServiceNow OAuth client_secret from SSM SecureString.

    Cached at module scope so cold-start cost is one GetParameter; warm
    invocations reuse the plaintext. If the operator rotates the SSM
    value, the next cold start picks up the change.
    """
    global _client_secret_cache
    if _client_secret_cache is not None:
        return _client_secret_cache
    if not SERVICENOW_CLIENT_SECRET_SSM_PARAM:
        raise ValueError(
            "SERVICENOW_CLIENT_SECRET_SSM_PARAM env var not configured"
        )
    resp = _get_ssm().get_parameter(
        Name=SERVICENOW_CLIENT_SECRET_SSM_PARAM,
        WithDecryption=True,
    )
    _client_secret_cache = resp["Parameter"]["Value"]
    return _client_secret_cache


def get_instance_url() -> str:
    """Return the configured ServiceNow instance URL (no trailing slash)."""
    if not SERVICENOW_INSTANCE_URL:
        raise ValueError("SERVICENOW_INSTANCE_URL env var not configured")
    return SERVICENOW_INSTANCE_URL


def get_client_id() -> str:
    """Return the configured ServiceNow OAuth client_id."""
    if not SERVICENOW_CLIENT_ID:
        raise ValueError("SERVICENOW_CLIENT_ID env var not configured")
    return SERVICENOW_CLIENT_ID


def _encrypt(plaintext: str) -> str:
    resp = _get_kms_client().encrypt(
        KeyId=OAUTH_KMS_KEY_ARN,
        Plaintext=plaintext.encode("utf-8"),
    )
    return base64.b64encode(resp["CiphertextBlob"]).decode("utf-8")


def _decrypt(ciphertext_b64: str) -> str:
    resp = _get_kms_client().decrypt(
        KeyId=OAUTH_KMS_KEY_ARN,
        CiphertextBlob=base64.b64decode(ciphertext_b64),
    )
    return resp["Plaintext"].decode("utf-8")


def _post_token(url: str, form: dict) -> Tuple[int, dict]:
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


def save_refresh_token(connection_id: str, refresh_token: str) -> None:
    """Upsert the encrypted refresh_token on the token row."""
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
    """Delete the token row (removes the refresh_token)."""
    _get_dynamodb().Table(WORKSPACES_TABLE).delete_item(
        Key={"PK": f"SERVICENOW_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    )
    logger.info(f"Deleted ServiceNow tokens for connection_id {connection_id}")


def exchange_code_for_tokens(
    connection_id: str,
    code: str,
    redirect_uri: str,
) -> Tuple[str, str, int]:
    """Exchange an authorization code for tokens against the deploy's
    configured ServiceNow instance.

    Returns (access_token, refresh_token, expires_in_seconds).
    Raises ValueError on exchange failure.
    """
    instance_url = get_instance_url()
    client_id = get_client_id()
    client_secret = _load_client_secret()
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

    instance_url = get_instance_url()
    client_id = get_client_id()
    client_secret = _load_client_secret()
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
    """Flip the connection row's ``last_sync_status`` to reauth_required."""
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


def build_authorize_url(redirect_uri: str, state: str) -> str:
    """Build the OAuth authorize URL for the configured ServiceNow instance.

    ServiceNow uses ``/oauth_auth.do`` (not the standard /authorize path)
    and there is no ``scope=`` param; the OAuth app inherits the user's
    ACLs at consent time.
    """
    instance_url = get_instance_url()
    client_id = get_client_id()
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
    )
    return f"{instance_url}/oauth_auth.do?{params}"
