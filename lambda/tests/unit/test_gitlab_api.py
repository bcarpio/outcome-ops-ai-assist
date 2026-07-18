"""Unit tests for shared.gitlab_api (ADR-050 GitLab API v4 raw-urllib client)."""

from __future__ import annotations

import os
import sys
from unittest.mock import patch

import pytest

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared import gitlab_api  # noqa: E402


class TestBaseAndEncoding:
    def test_default_base(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLAB_BASE_URL", None)
            assert gitlab_api.base_url() == "https://gitlab.com"

    def test_nested_subgroup_path_is_url_encoded(self):
        # the load-bearing nested-subgroup case (ADR-050 Decision 6/7)
        assert gitlab_api._enc("group/sub/deep/project") == \
            "group%2Fsub%2Fdeep%2Fproject"


class _Headers(dict):
    def get(self, k, default=None):  # mimic http headers .get
        return super().get(k, default)


def _one_page(items):
    """_request stub returning a single page with no X-Next-Page."""
    def stub(access_token, url, raw=False):
        return items, _Headers()
    return stub


class TestListing:
    def test_list_groups_uses_reporter_floor(self):
        captured = {}

        def stub(access_token, path, params=None):
            captured["params"] = params
            return [{"id": 1, "full_path": "g", "name": "g"}]
        with patch.object(gitlab_api, "_get_paginated", stub):
            out = gitlab_api.list_groups("at")
        # Reporter (20), not Developer (30) -- read-only integration floor
        assert captured["params"]["min_access_level"] == 20
        assert out == [{"id": 1, "full_path": "g", "name": "g"}]

    def test_list_group_projects_uses_path_with_namespace_as_slug(self):
        projects = [
            {"id": 1, "path_with_namespace": "grp/sub/proj", "name": "proj",
             "default_branch": "main"},
        ]
        with patch.object(gitlab_api, "_request", _one_page(projects)):
            out = gitlab_api.list_group_projects("at", 99)
        assert out == [{"id": 1, "slug": "grp/sub/proj", "name": "proj",
                        "default_branch": "main"}]

    def test_pagination_follows_x_next_page(self):
        calls = {"n": 0}

        def stub(access_token, url, raw=False):
            calls["n"] += 1
            if calls["n"] == 1:
                return ([{"id": 1, "path_with_namespace": "g/a", "name": "a",
                          "default_branch": "main"}],
                        _Headers({"X-Next-Page": "2"}))
            return ([{"id": 2, "path_with_namespace": "g/b", "name": "b",
                      "default_branch": "main"}], _Headers())
        with patch.object(gitlab_api, "_request", stub):
            out = gitlab_api.list_group_projects("at", 1)
        assert [p["slug"] for p in out] == ["g/a", "g/b"]
        assert calls["n"] == 2


class TestRepoReads:
    def test_default_branch(self):
        with patch.object(gitlab_api, "_get",
                          return_value={"default_branch": "develop"}):
            assert gitlab_api.get_default_branch("at", "g/p") == "develop"

    def test_latest_commit_sha(self):
        with patch.object(gitlab_api, "_get", return_value=[{"id": "abc123"}]):
            assert gitlab_api.get_latest_commit_sha("at", "g/p", "main") == "abc123"

    def test_latest_commit_sha_empty(self):
        with patch.object(gitlab_api, "_get", return_value=[]):
            assert gitlab_api.get_latest_commit_sha("at", "g/p", "main") is None

    def test_tree_returns_only_blob_paths(self):
        entries = [
            {"type": "tree", "path": "src"},
            {"type": "blob", "path": "src/main.py"},
            {"type": "blob", "path": "README.md"},
        ]
        with patch.object(gitlab_api, "_get_paginated", return_value=entries):
            assert gitlab_api.get_tree("at", "g/p", "main") == \
                ["src/main.py", "README.md"]

    def test_file_content_raw(self):
        def stub(access_token, url, raw=False):
            assert raw is True
            assert "/repository/files/" in url and "/raw" in url
            return b"print('hi')", _Headers()
        with patch.object(gitlab_api, "_request", stub):
            assert gitlab_api.get_file_content("at", "g/p", "a/b.py", "main") == \
                b"print('hi')"
