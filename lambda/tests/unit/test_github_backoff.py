# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for shared/github_backoff ().

Covers:
- 403 with rate-limit body -> retry
- 403 with permissions body -> no retry
- 429 with Retry-After -> retry after honored delay
- X-RateLimit-Reset -> retry when delay is within budget
- Retry-After exceeds MAX_DELAY_S -> give up
- 5xx -> retry with exponential backoff
- 2xx -> no retry
- Non-retryable HTTPError bubbles from urllib wrapper
"""
import importlib
import os
import sys
import time
import urllib.error
from unittest.mock import MagicMock, patch


def _load(**envs):
    for k, v in envs.items():
        os.environ[k] = v
    if "outcomeops_github_backoff" in sys.modules:
        del sys.modules["outcomeops_github_backoff"]
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    return importlib.import_module("shared.github_backoff")


class TestClassifiers:
    def test_permissions_403_not_retryable(self):
        mod = _load()
        assert mod._looks_like_rate_limit(403, "Not authorized") is False

    def test_rate_limit_403_retryable(self):
        mod = _load()
        assert mod._looks_like_rate_limit(
            403, "API rate limit exceeded for user"
        ) is True

    def test_secondary_rate_limit_403_retryable(self):
        mod = _load()
        assert mod._looks_like_rate_limit(
            403, "You have exceeded a secondary rate limit"
        ) is True

    def test_should_retry_429_regardless_of_body(self):
        mod = _load()
        assert mod._should_retry_status(429, "") is True

    def test_should_retry_5xx(self):
        mod = _load()
        for code in (500, 502, 503, 504):
            assert mod._should_retry_status(code, "") is True

    def test_should_not_retry_400(self):
        mod = _load()
        assert mod._should_retry_status(400, "bad params") is False


class TestRetryDelay:
    def test_retry_after_honored(self):
        mod = _load(GITHUB_BACKOFF_MAX_DELAY_S="60")
        delay = mod._retry_delay({"Retry-After": "5"}, attempt=1)
        assert delay == 5.0

    def test_retry_after_over_max_returns_none(self):
        mod = _load(GITHUB_BACKOFF_MAX_DELAY_S="10")
        delay = mod._retry_delay({"Retry-After": "600"}, attempt=1)
        assert delay is None

    def test_ratelimit_reset_computes_from_now(self):
        mod = _load(GITHUB_BACKOFF_MAX_DELAY_S="120")
        reset = int(time.time()) + 3
        delay = mod._retry_delay(
            {"X-RateLimit-Reset": str(reset)}, attempt=1,
        )
        assert delay is not None
        assert 0 <= delay <= 4  # tolerance for test timing

    def test_no_header_falls_back_to_backoff(self):
        mod = _load()
        delay = mod._retry_delay({}, attempt=1)
        assert delay is not None
        assert 0 <= delay <= 8


class TestGithubRequestWrapper:
    def test_2xx_returned_immediately(self):
        mod = _load()
        fake_resp = MagicMock(status_code=200, text="ok")
        fake_resp.headers = {}
        with patch("requests.request", return_value=fake_resp) as m:
            resp = mod.github_request("GET", "https://x")
        assert resp is fake_resp
        assert m.call_count == 1

    def test_rate_limit_403_retries_then_succeeds(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="3", GITHUB_BACKOFF_MAX_DELAY_S="60")
        bad = MagicMock(status_code=403, text="API rate limit exceeded")
        bad.headers = {"Retry-After": "0"}
        good = MagicMock(status_code=200, text="ok")
        good.headers = {}
        with patch("requests.request", side_effect=[bad, good]) as m, \
             patch("shared.github_backoff.time.sleep"):
            resp = mod.github_request("GET", "https://x")
        assert resp is good
        assert m.call_count == 2

    def test_permissions_403_not_retried(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="5")
        bad = MagicMock(status_code=403, text="Not authorized to access this")
        bad.headers = {}
        with patch("requests.request", return_value=bad) as m:
            resp = mod.github_request("GET", "https://x")
        assert resp is bad
        assert m.call_count == 1

    def test_gives_up_after_max_attempts(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="3", GITHUB_BACKOFF_MAX_DELAY_S="60")
        bad = MagicMock(status_code=429, text="")
        bad.headers = {"Retry-After": "0"}
        with patch("requests.request", return_value=bad) as m, \
             patch("shared.github_backoff.time.sleep"):
            resp = mod.github_request("GET", "https://x")
        assert resp.status_code == 429
        assert m.call_count == 3


class TestUrllibWrapper:
    def _http_error(self, code, body="", headers=None):
        # HTTPError extends urllib.error.URLError; kwargs must include hdrs+fp
        # for real HTTPError behavior. We fake .read() and .headers.
        fp = MagicMock()
        fp.read.return_value = body.encode() if isinstance(body, str) else body
        err = urllib.error.HTTPError(
            "https://x", code, "err", headers or {}, fp,
        )
        # Ensure read() works even after HTTPError constructor consumes it.
        err.read = lambda: body.encode() if isinstance(body, str) else body
        return err

    def test_2xx_returned_immediately(self):
        mod = _load()
        fake_resp = object()
        with patch(
            "shared.github_backoff.urllib.request.urlopen",
            return_value=fake_resp,
        ) as m:
            resp = mod.github_urllib_request(MagicMock(full_url="https://x"))
        assert resp is fake_resp
        assert m.call_count == 1

    def test_429_retries_then_succeeds(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="3", GITHUB_BACKOFF_MAX_DELAY_S="60")
        err = self._http_error(429, "", {"Retry-After": "0"})
        fake_resp = object()
        with patch(
            "shared.github_backoff.urllib.request.urlopen",
            side_effect=[err, fake_resp],
        ) as m, \
             patch("shared.github_backoff.time.sleep"):
            resp = mod.github_urllib_request(MagicMock(full_url="https://x"))
        assert resp is fake_resp
        assert m.call_count == 2

    def test_permissions_403_raises_immediately(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="5")
        err = self._http_error(403, "Not authorized", {})
        with patch(
            "shared.github_backoff.urllib.request.urlopen",
            side_effect=err,
        ) as m:
            try:
                mod.github_urllib_request(MagicMock(full_url="https://x"))
                assert False, "expected HTTPError"
            except urllib.error.HTTPError as raised:
                assert raised.code == 403
        assert m.call_count == 1

    def test_gives_up_and_raises_after_max_attempts(self):
        mod = _load(GITHUB_BACKOFF_MAX_ATTEMPTS="3", GITHUB_BACKOFF_MAX_DELAY_S="60")
        err = self._http_error(429, "", {"Retry-After": "0"})
        with patch(
            "shared.github_backoff.urllib.request.urlopen",
            side_effect=err,
        ) as m, \
             patch("shared.github_backoff.time.sleep"):
            try:
                mod.github_urllib_request(MagicMock(full_url="https://x"))
                assert False, "expected HTTPError"
            except urllib.error.HTTPError as raised:
                assert raised.code == 429
        assert m.call_count == 3
