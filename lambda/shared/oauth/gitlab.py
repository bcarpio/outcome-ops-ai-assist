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
Multi-instance (ADR-050 v2.0): the config-consuming functions accept an
``instance`` (a ``shared.gitlab_instances.GitLabInstance``) carrying
base_url + client_id + client_secret_ssm_param. Passing ``None`` resolves
to the single configured instance and RAISES on a multi-instance deploy,
so a caller that forgot to thread the connection's instance fails loudly.
The legacy env vars below are the single-instance fallback:
- ``GITLAB_BASE_URL`` / ``GITLAB_CLIENT_ID`` / ``GITLAB_CLIENT_SECRET_SSM_PARAM``
  -- synthesized into one instance with id "default" by gitlab_instances.
- ``WORKSPACES_TABLE`` / ``OAUTH_KMS_KEY_ARN`` -- consumed by the shared storage.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal
from typing import Dict, Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from shared import entra_id_oauth

logger = logging.getLogger()

PK_PREFIX = "GITLAB_TOKEN#"
# ``api`` (read/write) rather than ``read_api read_repository``: MR-analysis
# (ADR-050 chunk 05) posts and updates MR notes with this token, and GitLab
# has no finer-grained note-write scope -- ``api`` is the narrowest scope
# that can write a comment. Ingest-only deploys still work (api supersedes
# both read scopes). The group-owned OAuth application's registration must
# list the same scope, and tokens minted before the change stay read-only
# until the user reconnects.
SCOPES = "api"
_DEFAULT_BASE_URL = "https://gitlab.com"

# Access-token cache + single-flight refresh (ADR-050 v1.1). GitLab rotates
# the refresh token on EVERY refresh, so concurrent consumers that each
# refresh collide -- the loser's refresh token is invalidated and its call
# 401s, and it may persist a now-dead token, poisoning the connection.
# We cache the short-lived access token (KMS-encrypted) in the CREDENTIALS
# row and serialise refresh with a per-connection DDB lock: concurrent
# consumers reuse one cached token, and refreshes run one-at-a-time so the
# rotation never collides.
_ACCESS_CT_ATTR = "gl_access_ct"          # KMS-encrypted access token
_ACCESS_EXP_ATTR = "gl_access_exp"        # int epoch: access-token expiry
_REFRESH_LOCK_ATTR = "gl_refresh_lock"    # int epoch: refresh-lock expiry
_ACCESS_SKEW_SECONDS = 120                # refresh if within 2 min of expiry
_REFRESH_LOCK_TTL_SECONDS = 25            # lock auto-expires (> HTTP timeout)
_LOCK_WAIT_MAX_SECONDS = 25               # max wait for another refresher
_LOCK_WAIT_POLL_SECONDS = 1.0

_ssm_client = None
_dynamodb = None
_secret_cache: Dict[str, str] = {}


def _get_ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def base_url() -> str:
    """GitLab instance base URL (no trailing slash). SaaS default."""
    return (os.environ.get("GITLAB_BASE_URL") or _DEFAULT_BASE_URL).rstrip("/")


def get_client_id() -> str:
    """Legacy single-instance client id (env). Prefer instance.client_id."""
    return os.environ.get("GITLAB_CLIENT_ID", "")


def _resolve_instance(instance):
    """Return the GitLabInstance for this call.

    ``None`` resolves to the sole configured instance (raising on a
    multi-instance deploy, so a caller that forgot to pass the connection's
    instance fails loudly rather than authorizing against the wrong
    GitLab).
    """
    if instance is not None:
        return instance
    from shared.gitlab_instances import get_instance
    return get_instance(None)


def instance_for_connection(workspaces_table: str, connection_id: str):
    """Resolve the GitLabInstance a connection targets, from the instance id
    stamped on its CREDENTIALS row. Consumers that hold only a connection_id
    (CI-triggered MR checks, read_source) use this instead of a workspace
    lookup. Falls back to the sole instance for pre-multi-instance rows."""
    from shared.gitlab_instances import get_instance
    try:
        row = _get_ddb().Table(workspaces_table).get_item(
            Key=_cred_key(connection_id)).get("Item") or {}
    except ClientError:
        row = {}
    return get_instance(row.get("gl_instance_id"))


def _get_ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _load_client_secret(ssm_param: Optional[str] = None) -> str:
    """Read the OAuth app secret from SSM (cached per path). Never logged.

    ``ssm_param`` comes from the resolved instance; falls back to the legacy
    env var for single-instance back-compat."""
    path = ssm_param or os.environ.get("GITLAB_CLIENT_SECRET_SSM_PARAM", "")
    if not path:
        raise RuntimeError("GITLAB_CLIENT_SECRET_SSM_PARAM not set")
    if path not in _secret_cache:
        _secret_cache[path] = _get_ssm().get_parameter(
            Name=path, WithDecryption=True)["Parameter"]["Value"]
    return _secret_cache[path]


def build_authorize_url(redirect_uri: str, state: str, *,
                        instance=None) -> str:
    """The GitLab consent URL the user is sent to (one-click authorize)."""
    inst = _resolve_instance(instance)
    params = {
        "client_id": inst.client_id or get_client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": SCOPES,
        "state": state,
    }
    return f"{inst.base_url}/oauth/authorize?" + urllib.parse.urlencode(params)


def _post_token(form: Dict[str, str], *, base: Optional[str] = None) -> Dict:
    """POST application/x-www-form-urlencoded to GitLab's token endpoint."""
    data = urllib.parse.urlencode(form).encode()
    req = urllib.request.Request(
        f"{(base or base_url())}/oauth/token", data=data,
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
    *,
    instance=None,
) -> str:
    """Exchange an authorization code, persist the (rotating) refresh token, and
    return the access token."""
    inst = _resolve_instance(instance)
    body = _post_token({
        "client_id": inst.client_id or get_client_id(),
        "client_secret": _load_client_secret(inst.client_secret_ssm_param),
        "code": code,
        "grant_type": "authorization_code",
        "redirect_uri": redirect_uri,
    }, base=inst.base_url)
    entra_id_oauth.save_tokens(
        workspaces_table=workspaces_table,
        kms_key_arn=kms_key_arn,
        pk_prefix=PK_PREFIX,
        connection_id=connection_id,
        access_token=body["access_token"],
        refresh_token=body["refresh_token"],
        expires_in=int(body.get("expires_in", 7200)),
    )
    # Stamp the instance id on the CREDENTIALS row so consumers holding only
    # a connection_id (MR checks, read_source) can resolve which GitLab this
    # connection targets without a workspace lookup.
    try:
        _get_ddb().Table(workspaces_table).update_item(
            Key=_cred_key(connection_id),
            UpdateExpression="SET gl_instance_id = :iid",
            ExpressionAttributeValues={":iid": inst.id},
        )
    except ClientError:
        logger.warning("Failed to stamp instance id on GitLab CREDENTIALS row")
    return body["access_token"]


def _cred_key(connection_id: str) -> Dict[str, str]:
    return {"PK": f"{PK_PREFIX}{connection_id}", "SK": "CREDENTIALS"}


def _cached_access_if_valid(
    row: Optional[Dict], now: float, skew: int = _ACCESS_SKEW_SECONDS
) -> Optional[str]:
    """Decrypt + return the cached access token when it is present and not
    within ``skew`` seconds of expiry; otherwise None."""
    if not row:
        return None
    ct = row.get(_ACCESS_CT_ATTR)
    exp = row.get(_ACCESS_EXP_ATTR)
    if ct and exp is not None and float(exp) > now + skew:
        return entra_id_oauth.decrypt_token(ct)
    return None


def _try_acquire_lock(table, connection_id: str, now: float) -> bool:
    """Conditionally claim the per-connection refresh lock. Fails (returns
    False) when the row is absent or another holder's lock is still live."""
    try:
        table.update_item(
            Key=_cred_key(connection_id),
            UpdateExpression=f"SET {_REFRESH_LOCK_ATTR} = :until",
            ConditionExpression=(
                f"attribute_exists(PK) AND (attribute_not_exists({_REFRESH_LOCK_ATTR}) "
                f"OR {_REFRESH_LOCK_ATTR} < :now)"
            ),
            ExpressionAttributeValues={
                ":until": Decimal(int(now + _REFRESH_LOCK_TTL_SECONDS)),
                ":now": Decimal(int(now)),
            },
        )
        return True
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


def _acquire_lock_blocking(table, connection_id: str) -> bool:
    """Wait up to ``_LOCK_WAIT_MAX_SECONDS`` for the refresh lock. Refreshes
    are thus serialised: even under rotation, one completes + persists the
    new refresh token before the next reads it, so they never collide."""
    deadline = time.time() + _LOCK_WAIT_MAX_SECONDS
    while True:
        if _try_acquire_lock(table, connection_id, time.time()):
            return True
        if time.time() >= deadline:
            return False
        time.sleep(_LOCK_WAIT_POLL_SECONDS)


def _release_lock(table, connection_id: str) -> None:
    try:
        table.update_item(
            Key=_cred_key(connection_id),
            UpdateExpression=f"REMOVE {_REFRESH_LOCK_ATTR}",
            ConditionExpression="attribute_exists(PK)",
        )
    except ClientError:
        # Best-effort: the lock value carries its own TTL, so a missed
        # release self-heals after _REFRESH_LOCK_TTL_SECONDS.
        pass


def _raw_refresh(workspaces_table: str, connection_id: str,
                 inst=None) -> Optional[Dict]:
    """POST the stored refresh token to GitLab. Returns the token body
    (access_token / refresh_token / expires_in) or None when no refresh
    token is stored."""
    refresh_token = entra_id_oauth.get_refresh_token(
        workspaces_table, PK_PREFIX, connection_id)
    if not refresh_token:
        return None
    inst = _resolve_instance(inst)
    return _post_token({
        "client_id": inst.client_id or get_client_id(),
        "client_secret": _load_client_secret(inst.client_secret_ssm_param),
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }, base=inst.base_url)


def _persist_refresh(
    table, kms_key_arn: str, connection_id: str, body: Dict
) -> None:
    """Atomically persist the rotated refresh token + cache the new access
    token (both KMS-encrypted) and release the lock, in one update_item so
    the cache and the refresh token can never diverge."""
    now = time.time()
    expires_in = int(body.get("expires_in", 7200))
    expires_at = Decimal(int(now + expires_in))
    table.update_item(
        Key=_cred_key(connection_id),
        UpdateExpression=(
            "SET encrypted_refresh_token = :rt, expires_at = :exp, "
            f"{_ACCESS_CT_ATTR} = :act, {_ACCESS_EXP_ATTR} = :aexp, "
            "updated_at = :upd REMOVE " + _REFRESH_LOCK_ATTR
        ),
        ExpressionAttributeValues={
            ":rt": entra_id_oauth.encrypt_token(body["refresh_token"], kms_key_arn),
            ":exp": expires_at,
            ":act": entra_id_oauth.encrypt_token(body["access_token"], kms_key_arn),
            ":aexp": expires_at,
            ":upd": datetime.now(timezone.utc).isoformat(),
        },
    )


def get_valid_access_token(
    workspaces_table: str,
    kms_key_arn: str,
    connection_id: str,
    *,
    force_refresh: bool = False,
    instance=None,
) -> Optional[str]:
    """Return a usable GitLab access token, refreshing only when needed and
    only one caller at a time per connection.

    Fast path: a cached, unexpired access token is returned with no network
    call. Otherwise a per-connection DDB lock serialises the refresh so
    GitLab's per-use refresh-token rotation never collides across concurrent
    consumers (the bug that 401'd concurrent syncs). ``force_refresh=True``
    (used after a 401) skips the cache and mints a token under the lock.
    Returns None when the connection has no refresh token (caller must
    prompt reconnect)."""
    table = _get_ddb().Table(workspaces_table)

    # Resolve which instance to refresh against. An explicit instance wins;
    # otherwise derive it from the connection's stamped id so a caller with
    # only a connection_id still refreshes against the right GitLab on a
    # multi-instance deploy.
    if instance is None:
        from shared.gitlab_instances import is_multi_instance
        if is_multi_instance():
            instance = instance_for_connection(workspaces_table, connection_id)

    if not force_refresh:
        cached = _cached_access_if_valid(
            _read_cred_row(table, connection_id), time.time())
        if cached:
            return cached

    if not _acquire_lock_blocking(table, connection_id):
        # Couldn't serialise; fall back to any still-usable cached token
        # rather than risk a colliding unlocked refresh.
        if not force_refresh:
            return _cached_access_if_valid(
                _read_cred_row(table, connection_id), time.time(), skew=0)
        return None

    try:
        if not force_refresh:
            # A concurrent holder may have refreshed just before we locked.
            cached = _cached_access_if_valid(
                _read_cred_row(table, connection_id), time.time())
            if cached:
                return cached
        body = _raw_refresh(workspaces_table, connection_id, instance)
        if not body or not body.get("access_token"):
            return None
        _persist_refresh(table, kms_key_arn, connection_id, body)
        return body["access_token"]
    finally:
        _release_lock(table, connection_id)


def _read_cred_row(table, connection_id: str) -> Optional[Dict]:
    resp = table.get_item(Key=_cred_key(connection_id))
    return resp.get("Item")


def refresh_access_token(
    workspaces_table: str,
    kms_key_arn: str,
    connection_id: str,
    *,
    instance=None,
) -> Optional[str]:
    """Back-compat shim: force a fresh access token through the cached,
    single-flight path. Prefer ``get_valid_access_token`` (cache-first) in
    new code."""
    return get_valid_access_token(
        workspaces_table, kms_key_arn, connection_id,
        force_refresh=True, instance=instance)


def delete_tokens(workspaces_table: str, connection_id: str) -> None:
    entra_id_oauth.delete_tokens(workspaces_table, PK_PREFIX, connection_id)


def get_user_profile(access_token: str, *, base_url_arg: Optional[str] = None) -> Tuple[str, str]:
    """Return ``(user_id, username)`` for the authorized user (GET /api/v4/user)."""
    req = urllib.request.Request(
        f"{(base_url_arg or base_url())}/api/v4/user",
        headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    return str(body.get("id", "")), body.get("username", "")
