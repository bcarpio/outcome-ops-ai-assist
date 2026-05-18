"""Unit tests for ``shared.region_guard``.

Covers the active / passive dispatch, the audit-row emit on block,
and the standard 403 response shape.
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


@pytest.fixture(autouse=True)
def _hot_module(monkeypatch):
    """Reload region_guard for each test to avoid env-var leakage."""

    # active_region reads env vars lazily, so we can switch between
    # tests without re-importing it. region_guard is module-level
    # global imports; reload to pick up any patched env state.
    from shared import region_guard  # type: ignore

    importlib.reload(region_guard)
    yield region_guard


def test_guard_active_returns_silently_when_active(_hot_module, monkeypatch):
    """When the local region is active, guard_active is a no-op."""

    monkeypatch.delenv("APPCONFIG_APPLICATION_ID", raising=False)
    monkeypatch.delenv("APPCONFIG_ENVIRONMENT_ID", raising=False)
    monkeypatch.delenv("APPCONFIG_CONFIGURATION_PROFILE_ID", raising=False)

    # Should not raise and should not call into audit_writer.
    _hot_module.guard_active("create_workspace")


def test_guard_active_raises_when_passive_and_emits_audit(_hot_module, monkeypatch):
    """When passive, raises RegionPassiveError and writes an audit row."""

    monkeypatch.setenv("AUDIT_TABLE", "test-audit")
    with patch.object(_hot_module, "is_active_region", return_value=False), patch(
        "shared.audit_writer.log_admin_action"
    ) as mock_log:
        with pytest.raises(_hot_module.RegionPassiveError) as exc_info:
            _hot_module.guard_active(
                "create_workspace",
                user_email="alice@example.com",
                workspace_id="ws_test",
            )
        assert exc_info.value.operation == "create_workspace"

        mock_log.assert_called_once()
        call = mock_log.call_args
        assert call.kwargs["audit_table_name"] == "test-audit"
        assert call.kwargs["action"] == "ADMIN_MUTATION_BLOCKED_PASSIVE"
        assert call.kwargs["user_email"] == "alice@example.com"
        assert call.kwargs["workspace_id"] == "ws_test"
        assert call.kwargs["details"]["operation"] == "create_workspace"


def test_guard_active_swallows_audit_failure(_hot_module, monkeypatch):
    """A failing audit emit must NOT prevent the RegionPassiveError from
    reaching the caller — the user still needs the 403, even if the
    audit pipeline is down."""

    monkeypatch.setenv("AUDIT_TABLE", "test-audit")
    with patch.object(_hot_module, "is_active_region", return_value=False), patch(
        "shared.audit_writer.log_admin_action", side_effect=Exception("ddb down")
    ):
        with pytest.raises(_hot_module.RegionPassiveError):
            _hot_module.guard_active("create_workspace")


def test_guard_active_works_without_audit_table_env(_hot_module, monkeypatch):
    """When AUDIT_TABLE is unset, skip the audit emit but still raise."""

    monkeypatch.delenv("AUDIT_TABLE", raising=False)
    with patch.object(_hot_module, "is_active_region", return_value=False):
        with pytest.raises(_hot_module.RegionPassiveError):
            _hot_module.guard_active("create_workspace")


def test_passive_response_shape(_hot_module):
    """The 403 body must contain error, message, and operation fields."""

    resp = _hot_module.passive_response("create_workspace")
    assert resp["statusCode"] == 403
    assert resp["headers"]["Content-Type"] == "application/json"

    body = json.loads(resp["body"])
    assert body["error"] == "region_passive"
    assert body["operation"] == "create_workspace"
    assert "passive" in body["message"].lower()


def test_passive_response_empty_operation_still_well_formed(_hot_module):
    """Calling passive_response() with no operation still returns valid JSON."""

    resp = _hot_module.passive_response()
    body = json.loads(resp["body"])
    assert body["error"] == "region_passive"
    assert body["operation"] == ""


def test_action_admin_mutation_blocked_passive_in_admin_actions():
    """The new audit action constant is in the ADMIN_ACTIONS frozenset
    so the audit-query API accepts ?action=ADMIN_MUTATION_BLOCKED_PASSIVE
    filters from the dashboard."""

    from shared import audit_writer  # type: ignore

    assert (
        audit_writer.ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE
        in audit_writer.ADMIN_ACTIONS
    )
    assert (
        audit_writer.ACTION_ADMIN_MUTATION_BLOCKED_PASSIVE
        not in audit_writer.HIDDEN_DASHBOARD_ACTIONS
    ), "operators want passive-mode blocks visible on the dashboard"
