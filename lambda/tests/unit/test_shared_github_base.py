"""Unit tests for shared.github_base — GHES base-URL resolution.

The invariant that matters most: with GITHUB_BASE_URL unset (every deploy
that exists today), all helpers return the exact SaaS values the codebase
hardcoded before this module — byte-identical behavior.
"""

import pytest

from shared import github_base
from shared.source_fetch import _github_raw_request


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("GITHUB_BASE_URL", raising=False)


class TestSaasDefaults:
    def test_base_url_default(self):
        assert github_base.github_base_url() == "https://github.com"

    def test_api_base_default(self):
        assert github_base.github_api_base() == "https://api.github.com"

    def test_is_saas_default(self):
        assert github_base.is_saas() is True

    def test_host_default(self):
        assert github_base.github_host() == "github.com"

    def test_empty_env_is_saas(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "")
        assert github_base.github_api_base() == "https://api.github.com"


class TestGhes:
    def test_api_base_gets_v3_prefix(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "https://github.example.com")
        assert github_base.github_api_base() == "https://github.example.com/api/v3"

    def test_trailing_slash_normalized(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "https://github.example.com/")
        assert github_base.github_base_url() == "https://github.example.com"
        assert github_base.is_saas() is False

    def test_host_for_clone_urls(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "https://github.example.com")
        assert github_base.github_host() == "github.example.com"


class TestRawRequestRouting:
    """source_fetch's raw-fetch builder: SaaS keeps the raw host (rate-limit
    posture unchanged); GHES has no raw host and must use the contents API
    with the raw Accept header."""

    def test_saas_uses_raw_host(self):
        req = _github_raw_request(
            repo="bcarpio/ooai-license-server", ref="main",
            file_path="lambda/api/handler.py", github_token="tok",
            user_agent="ua",
        )
        assert req.full_url == (
            "https://raw.githubusercontent.com/bcarpio/ooai-license-server/"
            "main/lambda/api/handler.py"
        )
        assert not req.has_header("Accept")

    def test_ghes_uses_contents_api_with_raw_accept(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "https://github.example.com")
        req = _github_raw_request(
            repo="org/repo", ref="a" * 40,
            file_path="src/x.py", github_token="tok",
            user_agent="ua",
        )
        assert req.full_url == (
            "https://github.example.com/api/v3/repos/org/repo/contents/"
            "src/x.py?ref=" + "a" * 40
        )
        assert req.get_header("Accept") == "application/vnd.github.raw"

    def test_leading_slash_and_encoding(self, monkeypatch):
        monkeypatch.setenv("GITHUB_BASE_URL", "https://ghe.corp")
        req = _github_raw_request(
            repo="o/r", ref="feature/branch name",
            file_path="/dir/file with space.md", github_token="t",
            user_agent="ua",
        )
        # path segments stay slash-separated; ref is fully encoded
        assert "/contents/dir/file%20with%20space.md?ref=feature%2Fbranch%20name" in req.full_url
