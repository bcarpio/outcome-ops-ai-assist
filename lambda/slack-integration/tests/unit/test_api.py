"""Tests for slack-integration slack_api.py.

Covers the {ok: bool} envelope check, retry/backoff, pagination, and
per-method behavior. No real network calls.
"""

import importlib
import sys
from unittest.mock import MagicMock, patch

import pytest


def _reload_module():
    if "slack_api" in sys.modules:
        del sys.modules["slack_api"]
    return importlib.import_module("slack_api")


def _ok(json_body):
    """Slack 'ok' response."""
    r = MagicMock()
    r.status_code = 200
    r.headers = {}
    r.text = ""
    body = {"ok": True, **json_body}
    r.json.return_value = body
    return r


def _slack_error(error="invalid_auth", status_code=200):
    r = MagicMock()
    r.status_code = status_code
    r.headers = {}
    r.text = ""
    r.json.return_value = {"ok": False, "error": error}
    return r


def _http_status(status_code, retry_after=None):
    r = MagicMock()
    r.status_code = status_code
    r.headers = {"Retry-After": retry_after} if retry_after else {}
    r.text = "error"
    r.json.return_value = {}
    return r


class TestCallBasic:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_sets_authorization_header(self):
        with patch("slack_api.requests.post", return_value=_ok({})) as mp:
            self.mod._call("auth.test", access_token="tok")
        headers = mp.call_args.kwargs["headers"]
        assert headers["Authorization"] == "Bearer tok"

    def test_no_token_omits_authorization(self):
        with patch("slack_api.requests.post", return_value=_ok({})) as mp:
            self.mod._call("oauth.v2.access", access_token=None, params={"x": "y"})
        headers = mp.call_args.kwargs["headers"]
        assert "Authorization" not in headers

    def test_returns_body_on_ok(self):
        with patch("slack_api.requests.post", return_value=_ok({"channels": []})):
            data = self.mod._call("conversations.list", access_token="tok")
        assert data == {"ok": True, "channels": []}

    def test_ok_false_raises_value_error(self):
        with patch("slack_api.requests.post", return_value=_slack_error("not_in_channel")), \
             pytest.raises(ValueError, match="not_in_channel"):
            self.mod._call("conversations.history", access_token="tok")

    def test_ok_false_ratelimited_raises_typed_error(self):
        with patch("slack_api.requests.post", return_value=_slack_error("ratelimited")), \
             patch("slack_api.time.sleep"), \
             pytest.raises(self.mod.SlackRateLimitedError):
            # Three retries, all ratelimited
            with patch("slack_api.requests.post",
                       side_effect=[_slack_error("ratelimited")] * 3):
                self.mod._call("conversations.history", access_token="tok")


class TestRetries:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_429_retries_then_succeeds(self):
        with patch("slack_api.requests.post",
                   side_effect=[_http_status(429, retry_after="0"), _ok({"channels": []})]), \
             patch("slack_api.time.sleep") as mock_sleep:
            self.mod._call("conversations.list", access_token="tok")
        mock_sleep.assert_called_once()

    def test_429_exhausts_retries(self):
        with patch("slack_api.requests.post",
                   side_effect=[_http_status(429, retry_after="0")] * 3), \
             patch("slack_api.time.sleep"), \
             pytest.raises(self.mod.SlackRateLimitedError):
            self.mod._call("conversations.list", access_token="tok")

    def test_5xx_backs_off_then_succeeds(self):
        with patch("slack_api.requests.post",
                   side_effect=[_http_status(503), _ok({"channels": []})]), \
             patch("slack_api.time.sleep"):
            self.mod._call("conversations.list", access_token="tok")

    def test_5xx_exhausts_retries(self):
        with patch("slack_api.requests.post",
                   side_effect=[_http_status(500)] * 3), \
             patch("slack_api.time.sleep"), \
             pytest.raises(ValueError, match="server error"):
            self.mod._call("conversations.list", access_token="tok")

    def test_honors_retry_after_header(self):
        with patch("slack_api.requests.post",
                   side_effect=[_http_status(429, retry_after="2.5"), _ok({})]), \
             patch("slack_api.time.sleep") as mock_sleep:
            self.mod._call("auth.test", access_token="tok")
        mock_sleep.assert_called_with(2.5)

    def test_4xx_other_than_429_raises_immediately(self):
        with patch("slack_api.requests.post", return_value=_http_status(400)), \
             pytest.raises(ValueError, match="HTTP error"):
            self.mod._call("auth.test", access_token="tok")


class TestOAuth:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_oauth_v2_access_passes_credentials(self):
        with patch("slack_api.requests.post", return_value=_ok({
            "access_token": "xoxb-bot",
            "team": {"id": "T1", "name": "Test"},
            "bot_user_id": "U1",
        })) as mp:
            data = self.mod.oauth_v2_access(
                client_id="cid", client_secret="csec",
                code="abc", redirect_uri="https://x/y",
            )
        sent = mp.call_args.kwargs["data"]
        assert sent["client_id"] == "cid"
        assert sent["client_secret"] == "csec"
        assert sent["code"] == "abc"
        assert sent["redirect_uri"] == "https://x/y"
        assert data["access_token"] == "xoxb-bot"

    def test_oauth_v2_access_does_not_send_bearer_token(self):
        with patch("slack_api.requests.post", return_value=_ok({
            "access_token": "xoxb",
            "team": {"id": "T1"},
        })) as mp:
            self.mod.oauth_v2_access(
                client_id="cid", client_secret="csec",
                code="abc", redirect_uri="https://x/y",
            )
        assert "Authorization" not in mp.call_args.kwargs["headers"]


class TestUsersList:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_iterate_users_paginates(self):
        page1 = _ok({"members": [{"id": "U1"}, {"id": "U2"}],
                     "response_metadata": {"next_cursor": "abc"}})
        page2 = _ok({"members": [{"id": "U3"}]})
        with patch("slack_api.requests.post", side_effect=[page1, page2]):
            users = list(self.mod.iterate_users("tok"))
        assert [u["id"] for u in users] == ["U1", "U2", "U3"]


class TestConversations:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_iterate_member_channels_filters_to_is_member(self):
        page = _ok({"channels": [
            {"id": "C1", "name": "alpha", "is_member": True},
            {"id": "C2", "name": "beta", "is_member": False},
            {"id": "C3", "name": "gamma", "is_member": True},
        ]})
        with patch("slack_api.requests.post", return_value=page):
            channels = list(self.mod.iterate_member_channels("tok"))
        assert [c["id"] for c in channels] == ["C1", "C3"]

    def test_iterate_channel_messages_passes_oldest(self):
        page = _ok({"messages": [{"ts": "100"}, {"ts": "200"}]})
        with patch("slack_api.requests.post", return_value=page) as mp:
            list(self.mod.iterate_channel_messages("tok", "C1", oldest="50"))
        sent = mp.call_args.kwargs["data"]
        assert sent["channel"] == "C1"
        assert sent["oldest"] == "50"

    def test_iterate_thread_replies_skips_parent(self):
        """The first message in conversations.replies is the parent —
        skip it because it's already in conversations.history results."""
        page = _ok({"messages": [
            {"ts": "100", "text": "parent"},
            {"ts": "101", "text": "reply 1"},
            {"ts": "102", "text": "reply 2"},
        ]})
        with patch("slack_api.requests.post", return_value=page):
            replies = list(self.mod.iterate_thread_replies("tok", "C1", "100"))
        assert [r["text"] for r in replies] == ["reply 1", "reply 2"]


class TestUsersInfo:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()

    def test_users_info_passes_user_id(self):
        with patch("slack_api.requests.post", return_value=_ok({
            "user": {"id": "U1", "profile": {"display_name": "Alice"}},
        })) as mp:
            data = self.mod.users_info("tok", "U1")
        assert mp.call_args.kwargs["data"]["user"] == "U1"
        assert data["user"]["profile"]["display_name"] == "Alice"
