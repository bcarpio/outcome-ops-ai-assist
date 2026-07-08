"""
Token manager for Salesforce OAuth (env-level SSM credentials).

Follows the same shape as ``shared.oauth`` peers: one shared Connected
App per deploy. The operator populates ``salesforce_oauth_client_id`` in
tfvars and ``/{env}/{app}/oauth/salesforce/client-secret`` in SSM
SecureString. Workspace admins connect any Salesforce org (prod or
sandbox) against that single Connected App.

Salesforce OAuth specifics that differ from ServiceNow:

- Two authorize endpoints -- ``login.salesforce.com`` (prod) and
  ``test.salesforce.com`` (sandbox). Chosen per-connection at authorize
  time. Token endpoints follow the same host split.
- The token response body carries ``instance_url`` (the customer's My
  Domain, e.g. ``https://acme.my.salesforce.com``). Persisted on the
  connection row so subsequent SOQL/REST calls hit the right org.
- Scopes: ``refresh_token api`` at minimum. Adding ``id`` gets the
  identity URL for org/user resolution.

DynamoDB shape:

- Connection row (per workspace): PK=``WS#{ws}`` SK=``SALESFORCE_CONNECTION#{conn}``
  fields: ``last_sync_status``, ``instance_url`` (discovered),
  ``is_sandbox``, ``salesforce_org_id``, ``salesforce_user_id``,
  ``config.cursors`` (dict of {object_type: cursor}).
- Token row (per connection): PK=``SALESFORCE_TOKEN#{conn}`` SK=``CREDENTIALS``
  fields: ``encrypted_refresh_token`` only.
  ``entity_type=salesforce_token`` for the GSI the scheduler queries.
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
SALESFORCE_CLIENT_ID = os.environ.get("SALESFORCE_CLIENT_ID", "")
SALESFORCE_CLIENT_SECRET_SSM_PARAM = os.environ.get(
    "SALESFORCE_CLIENT_SECRET_SSM_PARAM", ""
)

# Salesforce login hosts. Choice depends on the connection's is_sandbox
# flag (set by the workspace admin at connect time).
PROD_LOGIN_HOST = "https://login.salesforce.com"
SANDBOX_LOGIN_HOST = "https://test.salesforce.com"

# Minimum OAuth scopes for CRM record ingest. `refresh_token` lets us
# rotate access tokens without re-authorizing; `api` grants REST access
# to Account/Contact/Opportunity + related objects.
DEFAULT_SCOPES = "refresh_token api"


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
    """Fetch + cache the Salesforce consumer_secret from SSM SecureString.

    Cached at module scope so cold-start cost is one GetParameter; warm
    invocations reuse the plaintext. If the operator rotates the SSM
    value, the next cold start picks up the change.
    """
    global _client_secret_cache
    if _client_secret_cache is not None:
        return _client_secret_cache
    if not SALESFORCE_CLIENT_SECRET_SSM_PARAM:
        raise ValueError(
            "SALESFORCE_CLIENT_SECRET_SSM_PARAM env var not configured"
        )
    resp = _get_ssm().get_parameter(
        Name=SALESFORCE_CLIENT_SECRET_SSM_PARAM,
        WithDecryption=True,
    )
    _client_secret_cache = resp["Parameter"]["Value"]
    return _client_secret_cache


def get_client_id() -> str:
    """Return the configured Salesforce Connected App consumer_key."""
    if not SALESFORCE_CLIENT_ID:
        raise ValueError("SALESFORCE_CLIENT_ID env var not configured")
    return SALESFORCE_CLIENT_ID


def login_host_for(is_sandbox: bool) -> str:
    """Return the Salesforce login host for a connection's sandbox flag."""
    return SANDBOX_LOGIN_HOST if is_sandbox else PROD_LOGIN_HOST


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
            "User-Agent": "outcome-ops-ai-assist/salesforce-oauth",
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
        Key={"PK": f"SALESFORCE_TOKEN#{connection_id}", "SK": "CREDENTIALS"},
        UpdateExpression=(
            "SET entity_type = :et, connection_id = :cid, "
            "encrypted_refresh_token = :rt, updated_at = :now, "
            "created_at = if_not_exists(created_at, :now)"
        ),
        ExpressionAttributeValues={
            ":et": "salesforce_token",
            ":cid": connection_id,
            ":rt": encrypted,
            ":now": now,
        },
    )
    logger.info(f"Saved Salesforce refresh_token for connection_id {connection_id}")


def delete_tokens(connection_id: str) -> None:
    """Delete the token row (removes the refresh_token)."""
    _get_dynamodb().Table(WORKSPACES_TABLE).delete_item(
        Key={"PK": f"SALESFORCE_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    )
    logger.info(f"Deleted Salesforce tokens for connection_id {connection_id}")


def exchange_code_for_tokens(
    connection_id: str,
    is_sandbox: bool,
    code: str,
    redirect_uri: str,
) -> dict:
    """Exchange an authorization code for tokens.

    Returns the full token response dict so the caller can persist
    ``instance_url``, ``id`` (identity URL for org/user), and any other
    fields on the connection row. Raises ValueError on exchange failure.

    Salesforce's response body:
    {
      "access_token": "...",
      "refresh_token": "...",
      "instance_url": "https://acme.my.salesforce.com",
      "id": "https://login.salesforce.com/id/00D.../005...",
      "token_type": "Bearer",
      "issued_at": "1234567890",
      "signature": "..."
    }
    """
    client_id = get_client_id()
    client_secret = _load_client_secret()
    status, body = _post_token(
        f"{login_host_for(is_sandbox)}/services/oauth2/token",
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
            f"Salesforce token exchange failed (status {status}): {body}"
        )
        raise ValueError(f"Token exchange failed: {status}")
    return body


def refresh_access_token(
    workspace_id: str,
    connection_id: str,
) -> Tuple[str, str]:
    """Get a fresh access token for a Salesforce connection.

    Returns ``(access_token, instance_url)``. The instance_url is
    returned because Salesforce sometimes updates it post-refresh (e.g.
    after an org rename or a My Domain change); the sync worker uses
    the freshest value for the current invocation.

    On ``invalid_grant`` the connection is marked ``last_sync_status =
    reauth_required`` and a ValueError is raised. Callers treat ValueError
    as a 401-equivalent (no retry, user must reconnect).
    """
    table = _get_dynamodb().Table(WORKSPACES_TABLE)
    token_item = table.get_item(
        Key={"PK": f"SALESFORCE_TOKEN#{connection_id}", "SK": "CREDENTIALS"}
    ).get("Item")
    if not token_item:
        raise ValueError(f"No token row for connection_id {connection_id}")
    encrypted_refresh = token_item.get("encrypted_refresh_token")
    if not encrypted_refresh:
        raise ValueError(f"No refresh_token for connection_id {connection_id}")
    refresh_token = _decrypt(encrypted_refresh)

    # Look up the connection to get is_sandbox for the login host choice.
    conn = table.get_item(
        Key={
            "PK": f"WS#{workspace_id}",
            "SK": f"SALESFORCE_CONNECTION#{connection_id}",
        }
    ).get("Item") or {}
    is_sandbox = bool(conn.get("is_sandbox"))

    client_id = get_client_id()
    client_secret = _load_client_secret()
    status, body = _post_token(
        f"{login_host_for(is_sandbox)}/services/oauth2/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
            "client_secret": client_secret,
        },
    )
    if status != 200:
        logger.error(f"Salesforce token refresh failed (status {status}): {body}")
        if body.get("error") == "invalid_grant":
            _mark_connection_reauth_required(workspace_id, connection_id)
            raise ValueError("Token expired; user must re-authenticate")
        raise ValueError(f"Token refresh failed: {status}")

    access_token = body["access_token"]
    instance_url = body.get("instance_url") or conn.get("instance_url", "")
    # Salesforce refresh tokens don't rotate by default; some org configs do,
    # so persist a new one if returned.
    new_refresh = body.get("refresh_token")
    if new_refresh and new_refresh != refresh_token:
        logger.info(f"Persisting rotated refresh_token for {connection_id}")
        save_refresh_token(connection_id, new_refresh)
    return access_token, instance_url


def _mark_connection_reauth_required(workspace_id: str, connection_id: str) -> None:
    """Flip the connection row's ``last_sync_status`` to reauth_required."""
    try:
        _get_dynamodb().Table(WORKSPACES_TABLE).update_item(
            Key={
                "PK": f"WS#{workspace_id}",
                "SK": f"SALESFORCE_CONNECTION#{connection_id}",
            },
            UpdateExpression=(
                "SET last_sync_status = :s, last_sync_error = :e, updated_at = :now"
            ),
            ExpressionAttributeValues={
                ":s": "reauth_required",
                ":e": "Refresh token rejected by Salesforce; reauthentication required",
                ":now": datetime.now(timezone.utc).isoformat(),
            },
        )
    except Exception:
        logger.exception(
            f"Failed to mark connection {connection_id} reauth_required "
            f"(workspace_id={workspace_id})"
        )


def build_authorize_url(
    is_sandbox: bool,
    redirect_uri: str,
    state: str,
    scopes: str = DEFAULT_SCOPES,
) -> str:
    """Build the OAuth authorize URL for prod or sandbox.

    Sandbox flag drives login-vs-test host choice. Scopes default to
    ``refresh_token api`` (minimum for CRM record ingest).
    """
    client_id = get_client_id()
    params = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": scopes,
        }
    )
    return f"{login_host_for(is_sandbox)}/services/oauth2/authorize?{params}"


def parse_org_and_user_id(identity_url: str) -> Tuple[str, str]:
    """Extract (org_id, user_id) from a Salesforce identity URL.

    Identity URL format: https://login.salesforce.com/id/{org_id}/{user_id}
    (or the customer's My Domain if issued from there). Returns
    ("", "") if the URL doesn't match the expected shape.
    """
    if not identity_url:
        return "", ""
    parts = identity_url.rstrip("/").split("/")
    if len(parts) < 2:
        return "", ""
    user_id = parts[-1]
    org_id = parts[-2]
    return org_id, user_id
