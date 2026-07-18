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


class TestRefresh:
    def test_rotates_and_resaves_refresh_token(self):
        # GitLab returns a NEW refresh token; it must be re-persisted.
        new = {"access_token": "at2", "refresh_token": "rt2", "expires_in": 7200}
        with patch.object(gitlab.entra_id_oauth, "get_refresh_token",
                          return_value="rt1"), \
             patch.object(gitlab, "_post_token", return_value=new), \
             patch.object(gitlab, "_load_client_secret", return_value="s"), \
             patch.object(gitlab.entra_id_oauth, "save_tokens") as save:
            out = gitlab.refresh_access_token("wt", "kms-arn", "conn1")
        assert out == "at2"
        assert save.call_args.kwargs["refresh_token"] == "rt2"

    def test_none_when_no_stored_refresh(self):
        with patch.object(gitlab.entra_id_oauth, "get_refresh_token",
                          return_value=None), \
             patch.object(gitlab, "_post_token") as post:
            assert gitlab.refresh_access_token("wt", "kms-arn", "conn1") is None
        post.assert_not_called()


class TestProfile:
    def test_returns_id_and_username(self):
        cm = MagicMock()
        cm.__enter__.return_value.read.return_value = b'{"id": 42, "username": "bc"}'
        cm.__exit__.return_value = False
        with patch.object(gitlab.urllib.request, "urlopen", return_value=cm):
            assert gitlab.get_user_profile("at") == ("42", "bc")
