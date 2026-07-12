"""Unit tests for shared.graph_client (app-only Graph group picker)."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared import graph_client  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_caches():
    graph_client._token_cache.update({"value": None, "exp": 0.0})
    graph_client._secret_cache["value"] = None
    yield


@pytest.fixture
def enabled_env():
    env = {
        "AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED": "true",
        "AZURE_AD_GRAPH_CLIENT_ID": "client-123",
        "AZURE_AD_TENANT_ID": "tenant-123",
        "AZURE_AD_GROUP_PATTERNS": '["outcomeops-*", "*-outcomeops-*"]',
    }
    with patch.dict(os.environ, env, clear=False):
        yield


class TestDisabled:
    def test_search_returns_empty_when_disabled(self):
        with patch.dict(os.environ, {"AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED": "false"}):
            assert graph_client.search_groups("eng") == []

    def test_member_of_empty_when_disabled(self):
        with patch.dict(os.environ, {"AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED": "false"}):
            assert graph_client.member_of("u1") == []

    def test_disabled_when_client_id_missing(self, _reset_caches):
        env = {"AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED": "true",
               "AZURE_AD_GRAPH_CLIENT_ID": "", "AZURE_AD_TENANT_ID": "t"}
        with patch.dict(os.environ, env, clear=False):
            assert graph_client.search_groups() == []


class TestSearchGroups:
    def test_scopes_and_filters(self, enabled_env):
        captured = {}

        def fake_get(path, tok, headers=None):
            captured["path"] = path
            captured["headers"] = headers
            return {"value": [
                {"id": "1", "displayName": "outcomeops-eng"},
                {"id": "2", "displayName": "aws-eng"},          # filtered out
                {"id": "3", "displayName": "emea-outcomeops-x"},  # embedded-star
            ]}

        with patch.object(graph_client, "_token", return_value="tok"), \
             patch.object(graph_client, "_get", side_effect=fake_get):
            groups = graph_client.search_groups("")

        # aws-eng dropped by local pattern filter; the two outcomeops match
        assert {g["id"] for g in groups} == {"1", "3"}
        # server-side startswith uses the clean prefix + eventual consistency
        assert "startswith(displayName,'outcomeops-')" in captured["path"]
        assert captured["headers"]["ConsistencyLevel"] == "eventual"

    def test_query_narrows_client_side(self, enabled_env):
        def fake_get(path, tok, headers=None):
            return {"value": [
                {"id": "1", "displayName": "outcomeops-engineering"},
                {"id": "2", "displayName": "outcomeops-sales"},
            ]}

        with patch.object(graph_client, "_token", return_value="tok"), \
             patch.object(graph_client, "_get", side_effect=fake_get):
            groups = graph_client.search_groups("eng")
        assert [g["id"] for g in groups] == ["1"]

    def test_returns_empty_when_no_token(self, enabled_env):
        with patch.object(graph_client, "_token", return_value=None):
            assert graph_client.search_groups("eng") == []


class TestMemberOf:
    def test_paginates(self, enabled_env):
        pages = [
            {"value": [{"id": "g1"}, {"id": "g2"}],
             "@odata.nextLink": "https://graph/next"},
            {"value": [{"id": "g3"}]},
        ]

        def fake_get(path, tok, headers=None):
            return pages.pop(0)

        with patch.object(graph_client, "_token", return_value="tok"), \
             patch.object(graph_client, "_get", side_effect=fake_get):
            ids = graph_client.member_of("user-1")
        assert ids == ["g1", "g2", "g3"]
