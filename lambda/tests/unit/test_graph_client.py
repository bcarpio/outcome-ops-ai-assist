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


class TestGroupCentric:
    """group_member_ids + groups_containing_user (ADR-048 v1.3)."""

    def setup_method(self):
        graph_client._members_cache.clear()

    def test_group_member_ids_paginates_and_casts_to_user(self, enabled_env):
        pages = [
            {"value": [{"id": "u1"}, {"id": "u2"}],
             "@odata.nextLink": "https://graph/next"},
            {"value": [{"id": "u3"}]},
        ]
        seen = {}

        def fake_get(path, tok, headers=None):
            seen.setdefault("first_path", path)
            return pages.pop(0)

        with patch.object(graph_client, "_token", return_value="tok"), \
             patch.object(graph_client, "_get", side_effect=fake_get):
            ids = graph_client.group_member_ids("grp-1")
        assert ids == {"u1", "u2", "u3"}
        # group-scoped, cast to user (no directory/user read)
        assert "/groups/grp-1/members/microsoft.graph.user" in seen["first_path"]

    def test_group_member_ids_cached(self, enabled_env):
        calls = {"n": 0}

        def fake_get(path, tok, headers=None):
            calls["n"] += 1
            return {"value": [{"id": "u1"}]}

        with patch.object(graph_client, "_token", return_value="tok"), \
             patch.object(graph_client, "_get", side_effect=fake_get):
            graph_client.group_member_ids("grp-1")
            graph_client.group_member_ids("grp-1")
        assert calls["n"] == 1  # second call served from cache

    def test_group_member_ids_raises_without_token(self, enabled_env):
        with patch.object(graph_client, "_token", return_value=None):
            with pytest.raises(RuntimeError):
                graph_client.group_member_ids("grp-1")

    def test_groups_containing_user_returns_subset(self, enabled_env):
        members = {"g1": {"u1", "u2"}, "g2": {"u9"}, "g3": {"u1"}}
        with patch.object(graph_client, "group_member_ids",
                          side_effect=lambda gid: members[gid]):
            got = graph_client.groups_containing_user("u1", ["g1", "g2", "g3"])
        assert got == ["g1", "g3"]

    def test_groups_containing_user_raises_when_disabled(self):
        with patch.dict(os.environ,
                        {"AZURE_AD_GROUP_GRAPH_FALLBACK_ENABLED": "false"}):
            with pytest.raises(RuntimeError):
                graph_client.groups_containing_user("u1", ["g1"])

    def test_groups_containing_user_propagates_lookup_failure(self, enabled_env):
        def boom(gid):
            raise RuntimeError("graph down")

        with patch.object(graph_client, "group_member_ids", side_effect=boom):
            with pytest.raises(RuntimeError):
                graph_client.groups_containing_user("u1", ["g1"])

    def test_groups_containing_user_empty_oid(self, enabled_env):
        assert graph_client.groups_containing_user("", ["g1"]) == []
