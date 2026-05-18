"""Local-AppConfig role check for HA active/passive failover.

Each region's AppConfig holds a single value describing what THAT region
is: "active" or "passive". Schedulers short-circuit when their local
region is "passive"; writers and read-side surfaces (chat / MCP /
find_callers etc.) do NOT gate on this — they run wherever invoked.

Used by:
- Scheduler Lambdas: `if not is_active_region(): return`

Public surface:
- ``get_region_role()`` -> "active" | "passive"
- ``is_active_region()`` -> bool
- ``get_activated_at()`` -> int | None  (epoch seconds; for split-brain check)

Caching: 30s in-memory cache. AppConfigData GetLatestConfiguration is
rate-limited and a failover flip is operator-driven, so up to 30s of
staleness is acceptable.

Single-region fast-path: if APPCONFIG_APPLICATION_ID is unset (the env
var block is empty for non-HA deploys), this module returns "active"
without any AWS calls. Single-region deployments never check AppConfig.

Failure modes (fail-closed):
- AppConfig unreachable + cached value present -> returns cached.
- AppConfig unreachable + no cached value (cold start) -> returns
  "passive". The scheduler skips its tick and tries again in an hour
  once AppConfig recovers. Chat / MCP / read-side don't gate on
  AppConfig, so user-facing features stay live during the outage.

Rationale for fail-closed (vs fail-open): an AppConfig outage
temporarily halts ingestion in BOTH regions, which is recoverable in
one scheduler tick. Fail-open would have both regions interpret
AppConfig unreachability as "I am active" and fire schedulers in
parallel, doubling Bedrock spend during a correlated outage.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _appconfig_ids() -> tuple[str, str, str]:
    """Read the AppConfig identifiers from env at call time.

    Read lazily so unit tests that swap env vars between tests see
    fresh values without needing to importlib.reload() this module.
    """

    return (
        os.environ.get("APPCONFIG_APPLICATION_ID", ""),
        os.environ.get("APPCONFIG_ENVIRONMENT_ID", ""),
        os.environ.get("APPCONFIG_CONFIGURATION_PROFILE_ID", ""),
    )


# Module-level shims for backwards compatibility with tests that
# imported these as constants. Reads are still lazy via _appconfig_ids
# at call sites in this module; these globals are not consulted.
APPCONFIG_APP_ID = ""
APPCONFIG_ENV_ID = ""
APPCONFIG_PROFILE_ID = ""

_CACHE_TTL_SECONDS = 30


_role_cache: dict = {
    "value": None,
    "activated_at": None,
    "expires_at": 0.0,
}
_session_token: str = ""

_appconfigdata_client = None
_test_client_override = None


def _client():
    global _appconfigdata_client
    if _test_client_override is not None:
        return _test_client_override
    if _appconfigdata_client is None:
        _appconfigdata_client = boto3.client("appconfigdata")
    return _appconfigdata_client


def _set_client_for_tests(client) -> None:
    """Inject a stub client so unit tests don't reach real AppConfig."""

    global _test_client_override
    _test_client_override = client


def _reset_for_tests() -> None:
    """Clear cache + injected client so tests start from cold state."""

    global _appconfigdata_client, _test_client_override, _session_token
    _appconfigdata_client = None
    _test_client_override = None
    _session_token = ""
    _role_cache["value"] = None
    _role_cache["activated_at"] = None
    _role_cache["expires_at"] = 0.0


def _start_session() -> str:
    """Begin a new AppConfigData session and return the initial token."""

    app_id, env_id, profile_id = _appconfig_ids()
    response = _client().start_configuration_session(
        ApplicationIdentifier=app_id,
        EnvironmentIdentifier=env_id,
        ConfigurationProfileIdentifier=profile_id,
    )
    return response["InitialConfigurationToken"]


def _fetch_role_from_appconfig() -> tuple[str, Optional[int]]:
    """Pull the current role + activated_at from AppConfig.

    Manages the AppConfigData session-token lifecycle: begins a session
    on first call, reuses the rotating token thereafter. If a token has
    aged out, AppConfigData raises BadRequestException and we restart
    the session transparently.

    Returns ``(role, activated_at_epoch_seconds_or_None)``.
    """

    global _session_token

    if not _session_token:
        _session_token = _start_session()

    try:
        response = _client().get_latest_configuration(
            ConfigurationToken=_session_token
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("BadRequestException", "InvalidParameterException"):
            _session_token = _start_session()
            response = _client().get_latest_configuration(
                ConfigurationToken=_session_token
            )
        else:
            raise

    # The token rotates on every successful call; persist for next call.
    _session_token = response.get(
        "NextPollConfigurationToken", _session_token
    )

    body = response.get("Configuration")
    if body is None:
        # AppConfigData returns empty Configuration when the client is
        # already at the latest version. The caller should treat this as
        # "no change" — we want to keep the cached value. Signal that
        # via the sentinel role="__unchanged__".
        return ("__unchanged__", None)

    raw = body.read() if hasattr(body, "read") else body
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    if not raw:
        return ("__unchanged__", None)

    parsed = json.loads(raw)
    role = parsed.get("role", "passive")
    activated_at = parsed.get("activated_at")

    activated_epoch: Optional[int] = None
    if isinstance(activated_at, (int, float)):
        activated_epoch = int(activated_at)
    elif isinstance(activated_at, str) and activated_at:
        try:
            activated_epoch = int(
                time.mktime(time.strptime(activated_at[:19], "%Y-%m-%dT%H:%M:%S"))
            )
        except (ValueError, OverflowError):
            activated_epoch = None

    return (role, activated_epoch)


def get_region_role() -> str:
    """Return this region's role: "active" or "passive".

    Cached for 30s. On AppConfig error, returns the last cached value
    if present, otherwise "passive" (fail-closed; see module docstring).
    Single-region deployments (AppConfig env vars unset) always return
    "active" without any AWS call.
    """

    app_id, env_id, profile_id = _appconfig_ids()
    if not (app_id and env_id and profile_id):
        return "active"

    now = time.time()
    if _role_cache["value"] is not None and now < _role_cache["expires_at"]:
        return _role_cache["value"]

    try:
        role, activated_at = _fetch_role_from_appconfig()
        if role == "__unchanged__":
            # Keep cached value, just refresh the TTL.
            if _role_cache["value"] is not None:
                _role_cache["expires_at"] = now + _CACHE_TTL_SECONDS
                return _role_cache["value"]
            # No cached value AND AppConfig says "no change" — this is
            # the first call returning empty, treat as fail-closed.
            return "passive"

        _role_cache["value"] = role
        _role_cache["activated_at"] = activated_at
        _role_cache["expires_at"] = now + _CACHE_TTL_SECONDS
        return role
    except ClientError as exc:
        cached = _role_cache["value"]
        if cached is not None:
            logger.warning(
                "AppConfig unreachable; returning cached role=%s (error=%s)",
                cached,
                exc.response.get("Error", {}).get("Code", "unknown"),
            )
            return cached
        logger.warning(
            "AppConfig unreachable on cold start; failing closed to passive (error=%s)",
            exc.response.get("Error", {}).get("Code", "unknown"),
        )
        return "passive"


def get_activated_at() -> Optional[int]:
    """Epoch seconds when this region last transitioned roles.

    Used by ``make ha-status`` for the split-brain check
    (most-recently-activated wins). Returns None if AppConfig has never
    been read successfully or if the single-region fast-path is active.
    """

    app_id, env_id, profile_id = _appconfig_ids()
    if not (app_id and env_id and profile_id):
        return None

    if _role_cache["value"] is None:
        # Force a fetch so the cache has activated_at too.
        get_region_role()
    return _role_cache.get("activated_at")


def is_active_region() -> bool:
    """True iff this region's AppConfig role is "active".

    Single-region deployments (AppConfig env vars unset) always return
    True so existing schedulers keep running unchanged.
    """

    return get_region_role() == "active"
