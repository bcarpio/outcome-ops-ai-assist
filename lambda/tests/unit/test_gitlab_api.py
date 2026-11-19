# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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

        def stub(access_token, path, params=None, *, base=None):
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


class _UrlopenResp:
    """Context-manager stub matching urllib.request.urlopen's contract.

    ``read`` raises if invoked -- the delete path (204 No Content) must NOT
    try to parse an empty body.
    """

    def __init__(self, body=b""):
        self._body = body
        self.read_called = False

    def read(self):
        self.read_called = True
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class TestDiscussionNotes:
    """Inline (line-anchored) MR discussion note CRUD -- the inline-review
    machinery. ``base_url`` is passed explicitly so ``_resolve_base`` never
    hits the gitlab_instances registry.
    """

    _BASE = "https://gitlab.example.com"

    def test_post_discussion_hits_discussions_endpoint_with_body_and_position(self):
        captured = {}

        def fake_send(access_token, path, payload, method, *, base):
            captured["path"] = path
            captured["payload"] = payload
            captured["method"] = method
            captured["base"] = base
            return {"id": "disc-1", "notes": [{"id": 55}]}

        position = {
            "position_type": "text",
            "base_sha": "b", "start_sha": "s", "head_sha": "h",
            "new_path": "src/a.py", "new_line": 12,
        }
        with patch.object(gitlab_api, "_send", fake_send):
            out = gitlab_api.post_merge_request_discussion(
                "at", "group/sub/project", 7, "the body", position,
                base_url=self._BASE)

        assert captured["method"] == "POST"
        assert captured["path"].endswith("/merge_requests/7/discussions")
        assert "group%2Fsub%2Fproject" in captured["path"]
        assert captured["payload"] == {"body": "the body", "position": position}
        assert captured["base"] == self._BASE
        assert out == {"id": "disc-1", "notes": [{"id": 55}]}

    def test_update_discussion_note_puts_to_notes_endpoint(self):
        captured = {}

        def fake_send(access_token, path, payload, method, *, base):
            captured["path"] = path
            captured["payload"] = payload
            captured["method"] = method
            return {"id": 55}

        with patch.object(gitlab_api, "_send", fake_send):
            gitlab_api.update_merge_request_discussion_note(
                "at", "g/p", 7, "disc-1", 55, "new body", base_url=self._BASE)

        assert captured["method"] == "PUT"
        assert captured["path"].endswith(
            "/merge_requests/7/discussions/disc-1/notes/55")
        assert captured["payload"] == {"body": "new body"}

    def test_delete_discussion_note_issues_delete_and_does_not_read_body(self):
        resp = _UrlopenResp(body=b"")  # 204 No Content

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return resp

        with patch.object(gitlab_api.urllib.request, "urlopen", fake_urlopen):
            out = gitlab_api.delete_merge_request_discussion_note(
                "at", "group/sub/project", 7, "disc-1", 55, base_url=self._BASE)

        assert out is None
        # 204 path must not attempt to parse an empty body.
        assert resp.read_called is False
        req = captured["req"]
        assert req.get_method() == "DELETE"
        assert req.full_url.endswith(
            "/merge_requests/7/discussions/disc-1/notes/55")
        assert "group%2Fsub%2Fproject" in req.full_url


class TestRateLimitRetry:
    """HTTP 429 is retried in place (Retry-After honoured, capped) instead of
    failing the caller -- a failed PR-check note post used to re-run the whole
    check via SQS redelivery."""

    _BASE = "https://gitlab.example.com"

    @staticmethod
    def _429(retry_after=None):
        import email.message
        import urllib.error

        headers = email.message.Message()
        if retry_after is not None:
            headers["Retry-After"] = retry_after
        return urllib.error.HTTPError(
            "https://gitlab.example.com/x", 429, "Too Many Requests", headers, None)

    def _run(self, outcomes):
        """Patch urlopen to raise/return ``outcomes`` in order; return
        (result, urlopen call count, sleeps)."""
        calls = {"n": 0}
        sleeps = []

        def fake_urlopen(req, timeout=None):
            outcome = outcomes[calls["n"]]
            calls["n"] += 1
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(gitlab_api.urllib.request, "urlopen", fake_urlopen), \
                patch.object(gitlab_api.time, "sleep", sleeps.append):
            result = gitlab_api.post_merge_request_note(
                "at", "g/p", 7, "body", base_url=self._BASE)
        return result, calls["n"], sleeps

    def test_retries_429_then_succeeds_honouring_retry_after(self):
        ok = _UrlopenResp(body=b'{"id": 1}')
        result, n, sleeps = self._run([self._429("2"), ok])
        assert result == {"id": 1}
        assert n == 2
        assert sleeps == [2.0]

    def test_backoff_when_no_retry_after(self):
        ok = _UrlopenResp(body=b'{"id": 1}')
        _, n, sleeps = self._run([self._429(), self._429(), ok])
        assert n == 3
        assert sleeps == [1.0, 2.0]

    def test_retry_after_is_capped(self):
        ok = _UrlopenResp(body=b'{"id": 1}')
        _, _, sleeps = self._run([self._429("3600"), ok])
        assert sleeps == [gitlab_api._RATE_LIMIT_MAX_SLEEP_S]

    def test_gives_up_after_max_retries(self):
        import urllib.error

        outcomes = [self._429("1")] * (gitlab_api._RATE_LIMIT_RETRIES + 1)
        with pytest.raises(urllib.error.HTTPError) as exc:
            self._run(outcomes)
        assert exc.value.code == 429

    def test_non_numeric_retry_after_falls_back_to_backoff(self):
        """GitLab may send Retry-After as an HTTP-date; that is not a number
        of seconds, so the exponential backoff applies instead."""
        ok = _UrlopenResp(body=b'{"id": 1}')
        _, n, sleeps = self._run(
            [self._429("Wed, 21 Oct 2026 07:28:00 GMT"), ok])
        assert n == 2
        assert sleeps == [1.0]

    def test_get_requests_retry_429_too(self):
        """Reads go through the same retry as writes."""
        ok = _UrlopenResp(body=b'{"iid": 7}')
        ok.headers = {}
        outcomes = [self._429("1"), ok]
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            outcome = outcomes[calls["n"]]
            calls["n"] += 1
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        with patch.object(gitlab_api.urllib.request, "urlopen", fake_urlopen), \
                patch.object(gitlab_api.time, "sleep", lambda s: None):
            result = gitlab_api.get_merge_request(
                "at", "g/p", 7, base_url=self._BASE)
        assert result == {"iid": 7}
        assert calls["n"] == 2

    def test_non_429_errors_are_not_retried(self):
        import email.message
        import urllib.error

        err = urllib.error.HTTPError(
            "https://gitlab.example.com/x", 500, "boom", email.message.Message(), None)
        calls = {"n": 0}

        def fake_urlopen(req, timeout=None):
            calls["n"] += 1
            raise err

        with patch.object(gitlab_api.urllib.request, "urlopen", fake_urlopen), \
                patch.object(gitlab_api.time, "sleep", lambda s: None):
            with pytest.raises(urllib.error.HTTPError):
                gitlab_api.post_merge_request_note(
                    "at", "g/p", 7, "body", base_url=self._BASE)
        assert calls["n"] == 1
