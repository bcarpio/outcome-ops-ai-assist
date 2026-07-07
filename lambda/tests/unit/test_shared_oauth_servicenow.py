"""Unit tests for shared/oauth/servicenow.py (env-SSM shape)."""

import base64
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("OAUTH_KMS_KEY_ARN", "arn:aws:kms:us-west-2:1:key/test")
os.environ.setdefault("SERVICENOW_INSTANCE_URL", "https://dev307154.service-now.com")
os.environ.setdefault("SERVICENOW_CLIENT_ID", "test-client-id")
os.environ.setdefault(
    "SERVICENOW_CLIENT_SECRET_SSM_PARAM",
    "/test/outcome-ops-ai-assist/oauth/servicenow/client-secret",
)

import shared.oauth.servicenow as sn  # noqa: E402


@pytest.fixture(autouse=True)
def reset_globals():
    sn._kms_client = None
    sn._dynamodb = None
    sn._ssm_client = None
    sn._client_secret_cache = None
    # Module-scope env-var caches may have picked up a different value if
    # another test file imported shared.oauth.servicenow first. Snap this
    # test's expected values in explicitly, then restore on teardown so
    # test order across files doesn't corrupt these constants.
    saved = (
        sn.SERVICENOW_INSTANCE_URL,
        sn.SERVICENOW_CLIENT_ID,
        sn.SERVICENOW_CLIENT_SECRET_SSM_PARAM,
    )
    sn.SERVICENOW_INSTANCE_URL = "https://dev307154.service-now.com"
    sn.SERVICENOW_CLIENT_ID = "test-client-id"
    sn.SERVICENOW_CLIENT_SECRET_SSM_PARAM = (
        "/test/outcome-ops-ai-assist/oauth/servicenow/client-secret"
    )
    yield
    (
        sn.SERVICENOW_INSTANCE_URL,
        sn.SERVICENOW_CLIENT_ID,
        sn.SERVICENOW_CLIENT_SECRET_SSM_PARAM,
    ) = saved


@pytest.fixture
def mock_kms():
    client = MagicMock()
    client.encrypt.return_value = {"CiphertextBlob": b"CIPHER"}
    client.decrypt.return_value = {"Plaintext": b"plain"}
    sn._kms_client = client
    return client


@pytest.fixture
def mock_table():
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    sn._dynamodb = resource
    return table


@pytest.fixture
def mock_ssm():
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "the-secret"}}
    sn._ssm_client = client
    return client


class TestConfigLoaders:
    def test_get_instance_url_returns_env(self):
        assert sn.get_instance_url() == "https://dev307154.service-now.com"

    def test_get_instance_url_missing_raises(self):
        sn.SERVICENOW_INSTANCE_URL = ""
        with pytest.raises(ValueError, match="SERVICENOW_INSTANCE_URL"):
            sn.get_instance_url()

    def test_get_client_id_returns_env(self):
        assert sn.get_client_id() == "test-client-id"

    def test_get_client_id_missing_raises(self):
        sn.SERVICENOW_CLIENT_ID = ""
        with pytest.raises(ValueError, match="SERVICENOW_CLIENT_ID"):
            sn.get_client_id()

    def test_load_client_secret_cached(self, mock_ssm):
        assert sn._load_client_secret() == "the-secret"
        assert sn._load_client_secret() == "the-secret"
        mock_ssm.get_parameter.assert_called_once()

    def test_load_client_secret_missing_ssm_env_raises(self, mock_ssm):
        with patch.object(sn, "SERVICENOW_CLIENT_SECRET_SSM_PARAM", ""):
            sn._client_secret_cache = None
            with pytest.raises(ValueError, match="SSM_PARAM"):
                sn._load_client_secret()


class TestSaveTokens:
    def test_save_refresh_token_kms_encrypts(self, mock_kms, mock_table):
        sn.save_refresh_token("conn-1", "rf-x")
        mock_kms.encrypt.assert_called_once()
        assert mock_kms.encrypt.call_args.kwargs["Plaintext"] == b"rf-x"
        mock_table.update_item.assert_called_once()

    def test_delete_tokens_removes_row(self, mock_table):
        sn.delete_tokens("conn-1")
        mock_table.delete_item.assert_called_once()
        assert mock_table.delete_item.call_args.kwargs["Key"] == {
            "PK": "SERVICENOW_TOKEN#conn-1",
            "SK": "CREDENTIALS",
        }


class TestExchange:
    @patch.object(sn, "_load_client_secret", return_value="secret")
    @patch.object(
        sn,
        "_post_token",
        return_value=(
            200,
            {"access_token": "a", "refresh_token": "r", "expires_in": 1800},
        ),
    )
    def test_ok(self, mock_post, mock_secret):
        access, refresh, expires = sn.exchange_code_for_tokens(
            "conn-1", "code", "https://cb"
        )
        assert access == "a"
        assert refresh == "r"
        assert expires == 1800
        # POST hit the env-configured instance URL
        assert mock_post.call_args.args[0].startswith(
            "https://dev307154.service-now.com/oauth_token.do"
        )
        form = mock_post.call_args.args[1]
        assert form["client_id"] == "test-client-id"
        assert form["client_secret"] == "secret"

    @patch.object(sn, "_load_client_secret", return_value="s")
    @patch.object(sn, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    def test_failure_raises(self, mock_post, mock_secret):
        with pytest.raises(ValueError, match="Token exchange failed"):
            sn.exchange_code_for_tokens("conn-1", "bad", "https://cb")


class TestRefresh:
    @patch.object(sn, "_load_client_secret", return_value="s")
    @patch.object(
        sn,
        "_post_token",
        return_value=(200, {"access_token": "new", "refresh_token": "rotated"}),
    )
    def test_rotates_refresh_token(self, mock_post, mock_secret, mock_kms, mock_table):
        mock_kms.decrypt.return_value = {"Plaintext": b"old-refresh"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with patch.object(sn, "save_refresh_token") as mock_save:
            token = sn.refresh_access_token("ws1", "conn-1")
        assert token == "new"
        mock_save.assert_called_once_with("conn-1", "rotated")

    @patch.object(sn, "_load_client_secret", return_value="s")
    @patch.object(sn, "_post_token", return_value=(200, {"access_token": "new"}))
    def test_missing_new_refresh_does_not_rewrite(
        self, mock_post, mock_secret, mock_kms, mock_table
    ):
        mock_kms.decrypt.return_value = {"Plaintext": b"old-refresh"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with patch.object(sn, "save_refresh_token") as mock_save:
            token = sn.refresh_access_token("ws1", "conn-1")
        assert token == "new"
        mock_save.assert_not_called()

    @patch.object(sn, "_load_client_secret", return_value="s")
    @patch.object(sn, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    def test_invalid_grant_marks_reauth_and_raises(
        self, mock_post, mock_secret, mock_kms, mock_table
    ):
        mock_kms.decrypt.return_value = {"Plaintext": b"stale"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with pytest.raises(ValueError, match="Token expired"):
            sn.refresh_access_token("ws1", "conn-1")
        calls = [
            c
            for c in mock_table.update_item.call_args_list
            if c.kwargs["Key"]["SK"].startswith("SERVICENOW_CONNECTION#")
        ]
        assert len(calls) == 1
        assert calls[0].kwargs["ExpressionAttributeValues"][":s"] == "reauth_required"


class TestAuthorizeUrl:
    def test_shape_uses_env_config(self):
        url = sn.build_authorize_url(redirect_uri="https://ui/cb", state="XYZ")
        assert url.startswith("https://dev307154.service-now.com/oauth_auth.do?")
        assert "response_type=code" in url
        assert "client_id=test-client-id" in url
        assert "state=XYZ" in url
