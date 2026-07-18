"""GitLab OAuth 2.0 token manager (ADR-050, Model A delegated OAuth).

GitLab is authorized per-user (authorization-code + refresh). The deployment has
ONE admin-registered group-owned OAuth app; each user authorizes it once and gets
a user-scoped token.

Persistence reuses the provider-agnostic, KMS-encrypted DDB storage in
``shared.entra_id_oauth`` (``save_tokens`` / ``get_refresh_token`` /
``delete_tokens``, keyed by ``pk_prefix="GITLAB_TOKEN#"``). Only the GitLab-
specific OAuth HTTP lives here (authorize URL, code exchange, refresh, profile) --
GitLab is not a Microsoft IdP, so Entra's token exchange cannot be reused.

Env / config:
- ``GITLAB_BASE_URL``   -- instance base, default ``https://gitlab.com`` (SaaS).
                           A later chunk makes this a per-deploy tfvar for
                           self-managed GitLab.
- ``GITLAB_CLIENT_ID``  -- OAuth application id (non-secret).
- ``GITLAB_CLIENT_SECRET_SSM_PARAM`` -- SSM SecureString path holding the app
                           secret (operator-managed; terraform does not create it).
- ``WORKSPACES_TABLE`` / ``OAUTH_KMS_KEY_ARN`` -- consumed by the shared storage.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Optional, Tuple

import boto3

from shared import entra_id_oauth

logger = logging.getLogger()

PK_PREFIX = "GITLAB_TOKEN#"
SCOPES = "read_api read_repository"
_DEFAULT_BASE_URL = "https://gitlab.com"

_ssm_client = None
_secret_cache: Dict[str, str] = {}


def base_url() -> str:
    """GitLab instance base URL (no trailing slash). SaaS default."""
    return (os.environ.get("GITLAB_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def get_client_id() -> str:
    return os.environ.get("GITLAB_CLIENT_ID", "")


def _get_ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _load_client_secret() -> str:
    """Read the OAuth app secret from SSM (cached per process). Never logged."""
    path = os.environ.get("GITLAB_CLIENT_SECRET_SSM_PARAM", "")
    if not path:
        raise RuntimeError("GITLAB_CLIENT_SECRET_SSM_PARAM not set")
    if path not in _secret_cache:
        _secret_cache[path] = _get_ssm().get_parameter(
            Name=path, WithDecryption=True)["Parameter"]["Value"]
    return _secret_cache[path]


def build_authorize_url(redirect_uri: str, state: str) -> str:
    """The GitLab consent URL the user is sent to (one-click authorize)."""
    params = {
        "client_id": get_client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{base_url()}/oauth/authorize?" + urllib.parse.urlencode(params)


def _post_token(form: Dict[str, str]) -> Dict:
    """POST application/x-www-form-urlencoded to GitLab's token endpoint."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        f"{base_url()}/oauth/token", data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def exchange_code_for_tokens(
    code: str,
    redirect_uri: str,
    workspaces_table: str,
    kms_key_arn: str,
    connection_id: str,
) -> str:
    """Exchange an authorization code, persist the (rotating) refresh token, and
    return the access token."""
    body = _post_token({
        "client_id": get_client_id(),
        "client_secret": _load_client_secret(),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    })
    entra_id_oauth.save_tokens(
        workspaces_table=workspaces_table,
        kms_key_arn=kms_key_arn,
        pk_prefix=PK_PREFIX,
        connection_id=connection_id,
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_in=int(body.get("expires_in", 7200)),
    )
    return body["access_token"]


def refresh_access_token(
    workspaces_table: str,
    kms_key_arn: str,
    connection_id: str,
) -> Optional[str]:
    """Mint a fresh access token from the stored refresh token; re-persist the
    rotated refresh token GitLab returns. Returns None if no refresh token is
    stored (caller must re-auth)."""
    refresh_token = entra_id_oauth.get_refresh_token(
        workspaces_table, PK_PREFIX, connection_id)
    if not refresh_token:
        return None
    body = _post_token({
        "client_id": get_client_id(),
        "client_secret": _load_client_secret(),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    })
    # GitLab rotates refresh tokens on every use -- persist the new one or the
    # next refresh fails.
    entra_id_oauth.save_tokens(
        workspaces_table=workspaces_table,
        kms_key_arn=kms_key_arn,
        pk_prefix=PK_PREFIX,
        connection_id=connection_id,
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_in=int(body.get("expires_in", 7200)),
    )
    return body["access_token"]


def delete_tokens(workspaces_table: str, connection_id: str) -> None:
    entra_id_oauth.delete_tokens(workspaces_table, PK_PREFIX, connection_id)


def get_user_profile(access_token: str) -> Tuple[str, str]:
    """Return ``(user_id, username)`` for the authorized user (GET /api/v4/user)."""
    req = urllib.request.Request(
        f"{base_url()}/api/v4/user",
        headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    return str(body.get("id", "")), body.get("username", "")
