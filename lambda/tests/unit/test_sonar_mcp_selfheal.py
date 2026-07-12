"""Unit tests for the SonarQube MCP self-heal Lambda (debounced auto-restart)."""

from __future__ import annotations

import datetime
import importlib.util
import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("CLUSTER", "prd-outcome-ops-ai-assist-ui")
os.environ.setdefault("SERVICE", "sonarqube-mcp")
os.environ.setdefault("DEBOUNCE_SECONDS", "600")


def _load():
    path = os.path.join(
        os.path.dirname(__file__), "../../sonar-mcp-selfheal/handler.py")
    spec = importlib.util.spec_from_file_location("sonar_selfheal_handler", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sonar_selfheal_handler"] = mod
    spec.loader.exec_module(mod)
    return mod


H = _load()


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _svc(deployments):
    return {"services": [{"deployments": deployments}]}


@pytest.fixture(autouse=True)
def _fresh_ecs():
    H.ecs = MagicMock()
    yield


def test_restarts_when_deployment_old_and_stable():
    H.ecs.describe_services.return_value = _svc([
        {"status": "PRIMARY", "rolloutState": "COMPLETED",
         "createdAt": _now() - datetime.timedelta(hours=2)},
    ])
    out = H.handler({}, None)
    assert out["restarted"] is True
    H.ecs.update_service.assert_called_once()
    kwargs = H.ecs.update_service.call_args.kwargs
    assert kwargs["forceNewDeployment"] is True
    assert kwargs["service"] == "sonarqube-mcp"


def test_debounced_when_deployment_recent():
    H.ecs.describe_services.return_value = _svc([
        {"status": "PRIMARY", "rolloutState": "COMPLETED",
         "createdAt": _now() - datetime.timedelta(seconds=30)},
    ])
    out = H.handler({}, None)
    assert out["restarted"] is False and out["reason"] == "debounced"
    H.ecs.update_service.assert_not_called()


def test_skips_when_rollout_in_progress():
    H.ecs.describe_services.return_value = _svc([
        {"status": "PRIMARY", "rolloutState": "IN_PROGRESS",
         "createdAt": _now() - datetime.timedelta(hours=2)},
    ])
    out = H.handler({}, None)
    assert out["restarted"] is False and out["reason"] == "rollout_in_progress"
    H.ecs.update_service.assert_not_called()


def test_service_not_found():
    H.ecs.describe_services.return_value = {"services": []}
    out = H.handler({}, None)
    assert out["restarted"] is False and out["reason"] == "service_not_found"
    H.ecs.update_service.assert_not_called()
