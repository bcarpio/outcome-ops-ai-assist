"""Unit tests for ``shared.active_region``.

Covers single-region fast-path, AppConfigData session-token flow,
30s cache, and the fail-closed semantics on AppConfig error.
"""

from __future__ import annotations

import importlib
import io
import json
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.active_region import ...`` works.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


APP_ID = "app-abc"
ENV_ID = "env-xyz"
PROFILE_ID = "prof-123"
INITIAL_TOKEN = "tok-initial"
NEXT_TOKEN = "tok-next"


def _config_body(role: str, activated_at: str = "2026-05-17T10:00:00Z") -> bytes:
    return json.dumps(
        {
            "schema_version": 1,
            "role": role,
            "activated_at": activated_at,
        }
    ).encode("utf-8")


def _make_stub_client(
    role: str = "active",
    activated_at: str = "2026-05-17T10:00:00Z",
) -> MagicMock:
    client = MagicMock()
    client.start_configuration_session.return_value = {
        "InitialConfigurationToken": INITIAL_TOKEN,
    }
    client.get_latest_configuration.return_value = {
        "Configuration": io.BytesIO(_config_body(role, activated_at=activated_at)),
        "NextPollConfigurationToken": NEXT_TOKEN,
        "ContentType": "application/json",
    }
    return client


@pytest.fixture(autouse=True)
def _hot_module(monkeypatch):
    """Configure the module env vars + reset its in-memory cache per test."""

    monkeypatch.setenv("APPCONFIG_APPLICATION_ID", APP_ID)
    monkeypatch.setenv("APPCONFIG_ENVIRONMENT_ID", ENV_ID)
    monkeypatch.setenv("APPCONFIG_CONFIGURATION_PROFILE_ID", PROFILE_ID)

    # Re-import the module under test so it reads the just-set env vars.
    # importlib.reload re-executes the module body (re-reading env), which
    # plain `from shared import active_region` after sys.modules-delete
    # doesn't do reliably because the `shared` package keeps a cached
    # attribute reference to the prior module object.
    from shared import active_region  # type: ignore  # noqa: E402

    importlib.reload(active_region)
    active_region._reset_for_tests()
    yield active_region
    active_region._reset_for_tests()


def test_single_region_deployment_always_active(monkeypatch):
    """When APPCONFIG_APPLICATION_ID is unset, returns "active" without AWS call."""

    monkeypatch.delenv("APPCONFIG_APPLICATION_ID", raising=False)
    monkeypatch.delenv("APPCONFIG_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("APPCONFIG_CONFIGURATION_PROFILE_ID", raising=False)

    from shared import active_region  # type: ignore

    importlib.reload(active_region)
    active_region._reset_for_tests()

    stub = MagicMock()
    active_region._set_client_for_tests(stub)

    assert active_region.get_region_role() == "active"
    assert active_region.is_active_region() is True
    stub.start_configuration_session.assert_not_called()
    stub.get_latest_configuration.assert_not_called()


def test_get_region_role_cache_hit(_hot_module):
    """First call hits AppConfig; second within 30s reuses the cache."""

    stub = _make_stub_client("active")
    _hot_module._set_client_for_tests(stub)

    assert _hot_module.get_region_role() == "active"
    assert _hot_module.get_region_role() == "active"

    assert stub.start_configuration_session.call_count == 1
    assert stub.get_latest_configuration.call_count == 1


def test_get_region_role_cache_expiry(_hot_module, monkeypatch):
    """A call after the 30s TTL re-fetches from AppConfig."""

    stub = _make_stub_client("active")
    _hot_module._set_client_for_tests(stub)

    real_time = time.time
    monkeypatch.setattr(time, "time", lambda: real_time())
    _hot_module.get_region_role()

    # Jump past TTL by patching time.time inside the module's namespace.
    future = real_time() + 60.0
    monkeypatch.setattr(_hot_module.time, "time", lambda: future)

    _hot_module.get_region_role()

    assert stub.get_latest_configuration.call_count == 2


def test_get_region_role_appconfig_error_fallback_to_cache(_hot_module):
    """ClientError after a successful call returns the cached value."""

    stub = _make_stub_client("active")
    _hot_module._set_client_for_tests(stub)

    assert _hot_module.get_region_role() == "active"

    # Now simulate AppConfig outage on the next call (forced via cache bust).
    _hot_module._role_cache["expires_at"] = 0
    stub.get_latest_configuration.side_effect = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "boom"}},
        "GetLatestConfiguration",
    )

    assert _hot_module.get_region_role() == "active"


def test_get_region_role_appconfig_error_no_cache_defaults_passive(_hot_module):
    """ClientError on cold start returns "passive" (fail-closed)."""

    stub = MagicMock()
    stub.start_configuration_session.return_value = {
        "InitialConfigurationToken": INITIAL_TOKEN,
    }
    stub.get_latest_configuration.side_effect = ClientError(
        {"Error": {"Code": "InternalServerException", "Message": "boom"}},
        "GetLatestConfiguration",
    )
    _hot_module._set_client_for_tests(stub)

    assert _hot_module.get_region_role() == "passive"
    assert _hot_module.is_active_region() is False


def test_is_active_region_when_appconfig_says_active(_hot_module):
    stub = _make_stub_client("active")
    _hot_module._set_client_for_tests(stub)
    assert _hot_module.is_active_region() is True


def test_is_active_region_when_appconfig_says_passive(_hot_module):
    stub = _make_stub_client("passive")
    _hot_module._set_client_for_tests(stub)
    assert _hot_module.is_active_region() is False


def test_activated_at_returned_with_role(_hot_module):
    """get_activated_at returns the timestamp from the AppConfig payload."""

    stub = _make_stub_client("active", activated_at="2026-05-17T10:00:00Z")
    _hot_module._set_client_for_tests(stub)

    assert _hot_module.get_region_role() == "active"
    ts = _hot_module.get_activated_at()
    assert isinstance(ts, int)
    assert ts > 0
