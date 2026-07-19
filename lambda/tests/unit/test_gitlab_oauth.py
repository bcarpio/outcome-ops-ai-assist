"""Unit tests for shared.oauth.gitlab (ADR-050 delegated OAuth token manager)."""

from __future__ import annotations

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

from shared.oauth import gitlab  # noqa: E402


@pytest.fixture(autouse=True)
def _env():
    gitlab._secret_cache.clear()
    gitlab._ssm_client = None
    with patch.dict(os.environ, {
        "GITLAB_CLIENT_ID": "app-123",
        "GITLAB_CLIENT_SECRET_SSM_PARAM": "/prd/app/gitlab/client-secret",
    }, clear=False):
        yield


class TestBaseUrl:
    def test_default_is_saas(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITLAB_BASE_URL", None)
            assert gitlab.base_url() == "https://gitlab.com"

    def test_env_override_strips_trailing_slash(self):
        with patch.dict(os.environ, {"GITLAB_BASE_URL": "https://gl.acme.com/"}):
            assert gitlab.base_url() == "https://gl.acme.com"


class TestAuthorizeUrl:
    def test_contains_all_oauth_params(self):
        url = gitlab.build_authorize_url("https://ui/api/gitlab/callback", "st8")
        assert url.startswith("https://gitlab.com/oauth/authorize?")
        assert "client_id=app-123" in url
        assert "response_type=code" in url
        assert "scope=read_api+read_repository" in url
        assert "state=st8" in url
        assert "redirect_uri=https%3A%2F%2Fui%2Fapi%2Fgitlab%2Fcallback" in url


class TestClientSecret:
    def test_reads_ssm_once_and_caches(self):
        ssm = MagicMock()
        ssm.get_parameter.return_value = {"Parameter": {"Value": "s3cret"}}
        with patch.object(gitlab, "_get_ssm", return_value=ssm):
            assert gitlab._load_client_secret() == "s3cret"
            assert gitlab._load_client_secret() == "s3cret"
        ssm.get_parameter.assert_called_once()

    def test_missing_param_raises(self):
        with patch.dict(os.environ, {"GITLAB_CLIENT_SECRET_SSM_PARAM": ""}):
            with pytest.raises(RuntimeError):
                gitlab._load_client_secret()


class TestExchange:
    def test_persists_refresh_and_returns_access(self):
        token_body = {"access_token": "at", "refresh_token": "rt",
                      "expires_in": 7200}
        with patch.object(gitlab, "_post_token", return_value=token_body), \
             patch.object(gitlab, "_load_client_secret", return_value="s"), \
             patch.object(gitlab.entra_id_oauth, "save_tokens") as save:
            out = gitlab.exchange_code_for_tokens(
                "code9", "https://ui/cb", "wt", "kms-arn", "conn1")
        assert out == "at"
        kwargs = save.call_args.kwargs
        assert kwargs["pk_prefix"] == "GITLAB_TOKEN#"
        assert kwargs["connection_id"] == "conn1"
        assert kwargs["refresh_token"] == "rt"


def _mock_table():
    """A mock DDB table whose update_item succeeds (lock/persist) by default."""
    table = MagicMock()
    return table


def _patch_ddb(table):
    ddb = MagicMock()
    ddb.Table.return_value = table
    return patch.object(gitlab, "_get_ddb", return_value=ddb)


def _patch_crypto():
    return (
        patch.object(gitlab.entra_id_oauth, "encrypt_token",
                     side_effect=lambda t, k: f"enc({t})"),
        patch.object(gitlab.entra_id_oauth, "decrypt_token",
                     side_effect=lambda ct: ct[4:-1] if ct.startswith("enc(") else ct),
    )


class TestGetValidAccessToken:
    def test_cache_hit_returns_without_refresh(self):
        # A valid cached access token is served with no lock + no network.
        table = _mock_table()
        table.get_item.return_value = {"Item": {
            "gl_access_ct": "enc(cachedAT)",
            "gl_access_exp": 9999999999,  # far future
        }}
        enc, dec = _patch_crypto()
        with _patch_ddb(table), enc, dec, \
                patch.object(gitlab, "_post_token") as post:
            out = gitlab.get_valid_access_token("wt", "kms-arn", "conn1")
        assert out == "cachedAT"
        post.assert_not_called()
        table.update_item.assert_not_called()  # no lock taken

    def test_refresh_when_no_cache_rotates_and_caches(self):
        # No cached access token -> lock, refresh, persist rotated refresh +
        # cached access, release. GitLab returns a NEW refresh token.
        table = _mock_table()
        table.get_item.return_value = {"Item": {
            "encrypted_refresh_token": "enc(rt1)",
        }}
        new = {"access_token": "at2", "refresh_token": "rt2", "expires_in": 7200}
        enc, dec = _patch_crypto()
        with _patch_ddb(table), enc, dec, \
                patch.object(gitlab.entra_id_oauth, "get_refresh_token",
                             return_value="rt1"), \
                patch.object(gitlab, "_post_token", return_value=new), \
                patch.object(gitlab, "_load_client_secret", return_value="s"):
            out = gitlab.get_valid_access_token("wt", "kms-arn", "conn1")
        assert out == "at2"
        # One of the update_item calls persists the rotated refresh token +
        # the freshly cached access token.
        persisted = [
            c for c in table.update_item.call_args_list
            if ":rt" in (c.kwargs.get("ExpressionAttributeValues") or {})
        ]
        assert persisted, "expected a persist update_item with the rotated token"
        vals = persisted[0].kwargs["ExpressionAttributeValues"]
        assert vals[":rt"] == "enc(rt2)"
        assert vals[":act"] == "enc(at2)"

    def test_force_refresh_ignores_valid_cache(self):
        # After a 401 the caller forces a refresh even though the cache looks
        # valid -- a fresh token must be minted under the lock.
        table = _mock_table()
        table.get_item.return_value = {"Item": {
            "gl_access_ct": "enc(staleAT)",
            "gl_access_exp": 9999999999,
            "encrypted_refresh_token": "enc(rt1)",
        }}
        new = {"access_token": "freshAT", "refresh_token": "rt2", "expires_in": 7200}
        enc, dec = _patch_crypto()
        with _patch_ddb(table), enc, dec, \
                patch.object(gitlab.entra_id_oauth, "get_refresh_token",
                             return_value="rt1"), \
                patch.object(gitlab, "_post_token", return_value=new) as post, \
                patch.object(gitlab, "_load_client_secret", return_value="s"):
            out = gitlab.get_valid_access_token(
                "wt", "kms-arn", "conn1", force_refresh=True)
        assert out == "freshAT"
        post.assert_called_once()  # refreshed despite the valid-looking cache

    def test_contended_lock_reuses_cache_without_refreshing(self):
        # Another worker holds the refresh lock: we must NOT refresh (which
        # would collide on rotation) -- reuse the still-usable cached token.
        # Token is inside the skew window: the initial skew=120 check misses
        # (so we try to lock) but the skew=0 fallback still accepts it.
        import time as _t
        table = _mock_table()
        table.get_item.return_value = {"Item": {
            "gl_access_ct": "enc(cachedAT)",
            "gl_access_exp": int(_t.time()) + 60,
            "encrypted_refresh_token": "enc(rt1)",
        }}
        enc, dec = _patch_crypto()
        with _patch_ddb(table), enc, dec, \
                patch.object(gitlab, "_acquire_lock_blocking", return_value=False), \
                patch.object(gitlab, "_post_token") as post:
            out = gitlab.get_valid_access_token("wt", "kms-arn", "conn1")
        assert out == "cachedAT"
        post.assert_not_called()

    def test_none_when_no_stored_refresh(self):
        table = _mock_table()
        table.get_item.return_value = {"Item": {}}  # no refresh token
        with _patch_ddb(table), \
                patch.object(gitlab.entra_id_oauth, "get_refresh_token",
                             return_value=None), \
                patch.object(gitlab, "_post_token") as post:
            assert gitlab.get_valid_access_token(
                "wt", "kms-arn", "conn1", force_refresh=True) is None
        post.assert_not_called()


class TestRefreshShim:
    def test_delegates_to_get_valid_force_refresh(self):
        with patch.object(gitlab, "get_valid_access_token",
                          return_value="at") as gv:
            out = gitlab.refresh_access_token("wt", "kms-arn", "conn1")
        assert out == "at"
        assert gv.call_args.kwargs["force_refresh"] is True


class TestProfile:
    def test_returns_id_and_username(self):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b'{"id": 42, "username": "bc"}'
        cm.__exit__.return_value = False
        with patch.object(gitlab.urllib.request, "urlopen", return_value=cm):
            assert gitlab.get_user_profile("at") == ("42", "bc")
