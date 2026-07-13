"""Unit tests for the native SonarQube Cloud MCP tools (lambda/mcp-server/sonarcloud.py)."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_MCP_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "mcp-server"))


def _load():
    path = os.path.join(_MCP_DIR, "sonarcloud.py")
    spec = importlib.util.spec_from_file_location("sonarcloud_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["sonarcloud_under_test"] = mod
    spec.loader.exec_module(mod)
    return mod


S = _load()


@pytest.fixture(autouse=True)
def _env():
    S._TOKEN_CACHE["value"] = "tok-123"  # skip SSM
    with patch.dict(os.environ, {"SONARQUBE_ORG": "bcarpio",
                                 "SONARQUBE_URL": "https://sonarcloud.io"}):
        yield


def _mock_urlopen(payload):
    """Return a context-manager mock whose .read() yields json(payload), and
    capture the requested URL."""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["auth"] = req.headers.get("Authorization")
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = json.dumps(payload).encode()
        cm.__exit__.return_value = False
        return cm

    return fake_urlopen, captured


class TestCsv:
    def test_list_joined(self):
        assert S._csv(["HIGH", "BLOCKER"]) == "HIGH,BLOCKER"

    def test_string_passthrough(self):
        assert S._csv("HIGH") == "HIGH"


class TestProjects:
    def test_transforms_components_to_projects(self):
        fake, cap = _mock_urlopen(
            {"components": [{"key": "k1", "name": "n1", "x": 9}], "paging": {"total": 1}})
        with patch.object(S.urllib.request, "urlopen", fake):
            out = S.search_my_sonarqube_projects({"pageSize": 5}, "e@x")
        body = json.loads(out["text"])
        assert body["projects"] == [{"key": "k1", "name": "n1"}]
        assert "organization=bcarpio" in cap["url"]
        assert cap["auth"] == "Bearer tok-123"


class TestQualityGate:
    def test_unwraps_project_status(self):
        fake, cap = _mock_urlopen({"projectStatus": {"status": "ERROR", "conditions": []}})
        with patch.object(S.urllib.request, "urlopen", fake):
            out = S.get_project_quality_gate_status({"projectKey": "pk"}, "e@x")
        assert json.loads(out["text"]) == {"status": "ERROR", "conditions": []}
        assert "projectKey=pk" in cap["url"]


class TestMeasures:
    def test_metric_keys_joined(self):
        fake, cap = _mock_urlopen({"component": {"measures": []}})
        with patch.object(S.urllib.request, "urlopen", fake):
            S.get_component_measures(
                {"projectKey": "pk", "metricKeys": ["bugs", "coverage"]}, "e@x")
        assert "metricKeys=bugs%2Ccoverage" in cap["url"]
        assert "component=pk" in cap["url"]


class TestIssues:
    def test_lists_joined_and_org_scoped(self):
        fake, cap = _mock_urlopen({"issues": [], "paging": {}})
        with patch.object(S.urllib.request, "urlopen", fake):
            S.search_sonar_issues_in_projects(
                {"projects": ["pk"], "severities": ["HIGH", "BLOCKER"],
                 "issueStatuses": ["OPEN"]}, "e@x")
        assert "organization=bcarpio" in cap["url"]
        # new impact-severity vocab goes to impactSeverities, not severities
        assert "impactSeverities=HIGH%2CBLOCKER" in cap["url"]
        assert "issueStatuses=OPEN" in cap["url"]


class TestErrors:
    def test_http_error_returns_error_dict(self):
        import urllib.error

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with patch.object(S.urllib.request, "urlopen", boom):
            out = S.get_project_quality_gate_status({"projectKey": "pk"}, "e@x")
        assert "error" in out and "401" in out["error"]

    def test_missing_token(self):
        S._TOKEN_CACHE["value"] = None
        with patch.dict(os.environ, {"SONARQUBE_TOKEN_SSM_PATH": ""}, clear=False):
            out = S.search_my_sonarqube_projects({}, "e@x")
        assert "error" in out and "not configured" in out["error"]
