"""Unit tests for shared/oauth/salesforce.py."""

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
os.environ.setdefault("SALESFORCE_CLIENT_ID", "test-client-id")
os.environ.setdefault(
    "SALESFORCE_CLIENT_SECRET_SSM_PARAM",
    "/test/outcome-ops-ai-assist/oauth/salesforce/client-secret",
)

import shared.oauth.salesforce as sf  # noqa: E402


@pytest.fixture(autouse=True)
def reset_globals():
    sf._kms_client = None
    sf._dynamodb = None
    sf._ssm_client = None
    sf._client_secret_cache = None
    saved = (sf.SALESFORCE_CLIENT_ID, sf.SALESFORCE_CLIENT_SECRET_SSM_PARAM)
    sf.SALESFORCE_CLIENT_ID = "test-client-id"
    sf.SALESFORCE_CLIENT_SECRET_SSM_PARAM = (
        "/test/outcome-ops-ai-assist/oauth/salesforce/client-secret"
    )
    yield
    sf.SALESFORCE_CLIENT_ID, sf.SALESFORCE_CLIENT_SECRET_SSM_PARAM = saved


@pytest.fixture
def mock_kms():
    client = MagicMock()
    client.encrypt.return_value = {"CiphertextBlob": b"CIPHER"}
    client.decrypt.return_value = {"Plaintext": b"plain"}
    sf._kms_client = client
    return client


@pytest.fixture
def mock_table():
    table = MagicMock()
    resource = MagicMock()
    resource.Table.return_value = table
    sf._dynamodb = resource
    return table


@pytest.fixture
def mock_ssm():
    client = MagicMock()
    client.get_parameter.return_value = {"Parameter": {"Value": "the-secret"}}
    sf._ssm_client = client
    return client


class TestConfigLoaders:
    def test_get_client_id_returns_env(self):
        assert sf.get_client_id() == "test-client-id"

    def test_get_client_id_missing_raises(self):
        sf.SALESFORCE_CLIENT_ID = ""
        with pytest.raises(ValueError, match="SALESFORCE_CLIENT_ID"):
            sf.get_client_id()

    def test_load_client_secret_cached(self, mock_ssm):
        assert sf._load_client_secret() == "the-secret"
        assert sf._load_client_secret() == "the-secret"
        mock_ssm.get_parameter.assert_called_once()

    def test_load_client_secret_missing_ssm_env_raises(self, mock_ssm):
        with patch.object(sf, "SALESFORCE_CLIENT_SECRET_SSM_PARAM", ""):
            sf._client_secret_cache = None
            with pytest.raises(ValueError, match="SSM_PARAM"):
                sf._load_client_secret()


class TestLoginHost:
    def test_prod_default(self):
        assert sf.login_host_for(False) == "https://login.salesforce.com"

    def test_sandbox(self):
        assert sf.login_host_for(True) == "https://test.salesforce.com"


class TestAuthorizeUrl:
    def test_prod_shape(self):
        url = sf.build_authorize_url(is_sandbox=False, redirect_uri="https://ui/cb", state="XYZ")
        assert url.startswith("https://login.salesforce.com/services/oauth2/authorize?")
        assert "response_type=code" in url
        assert "client_id=test-client-id" in url
        assert "state=XYZ" in url
        assert "scope=refresh_token+api" in url

    def test_sandbox_shape(self):
        url = sf.build_authorize_url(is_sandbox=True, redirect_uri="https://ui/cb", state="XYZ")
        assert url.startswith("https://test.salesforce.com/services/oauth2/authorize?")


class TestParseIdentityUrl:
    def test_extracts_org_and_user(self):
        org, user = sf.parse_org_and_user_id(
            "https://login.salesforce.com/id/00D000000000001EAA/005000000000001AAA"
        )
        assert org == "00D000000000001EAA"
        assert user == "005000000000001AAA"

    def test_handles_my_domain(self):
        org, user = sf.parse_org_and_user_id(
            "https://acme.my.salesforce.com/id/00D.../005..."
        )
        assert org == "00D..."
        assert user == "005..."

    def test_empty_input(self):
        assert sf.parse_org_and_user_id("") == ("", "")


class TestSaveTokens:
    def test_save_refresh_token_kms_encrypts(self, mock_kms, mock_table):
        sf.save_refresh_token("conn-1", "rf-x")
        mock_kms.encrypt.assert_called_once()
        assert mock_kms.encrypt.call_args.kwargs["Plaintext"] == b"rf-x"
        mock_table.update_item.assert_called_once()

    def test_delete_tokens(self, mock_table):
        sf.delete_tokens("conn-1")
        mock_table.delete_item.assert_called_once()
        assert mock_table.delete_item.call_args.kwargs["Key"] == {
            "PK": "SALESFORCE_TOKEN#conn-1",
            "SK": "CREDENTIALS",
        }


class TestExchange:
    @patch.object(sf, "_load_client_secret", return_value="secret")
    @patch.object(
        sf,
        "_post_token",
        return_value=(
            200,
            {
                "access_token": "a",
                "refresh_token": "r",
                "instance_url": "https://acme.my.salesforce.com",
                "id": "https://login.salesforce.com/id/00D/005",
                "issued_at": "123",
            },
        ),
    )
    def test_returns_full_response_dict(self, mock_post, mock_secret):
        resp = sf.exchange_code_for_tokens(
            connection_id="c1", is_sandbox=False,
            code="code", redirect_uri="https://cb",
        )
        assert resp["access_token"] == "a"
        assert resp["refresh_token"] == "r"
        assert resp["instance_url"] == "https://acme.my.salesforce.com"
        assert mock_post.call_args.args[0].startswith(
            "https://login.salesforce.com/services/oauth2/token"
        )

    @patch.object(sf, "_load_client_secret", return_value="secret")
    @patch.object(
        sf,
        "_post_token",
        return_value=(200, {"access_token": "a", "refresh_token": "r"}),
    )
    def test_sandbox_uses_test_endpoint(self, mock_post, mock_secret):
        sf.exchange_code_for_tokens("c1", True, "code", "https://cb")
        assert mock_post.call_args.args[0].startswith(
            "https://test.salesforce.com/services/oauth2/token"
        )

    @patch.object(sf, "_load_client_secret", return_value="s")
    @patch.object(sf, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    def test_failure_raises(self, mock_post, mock_secret):
        with pytest.raises(ValueError, match="Token exchange failed"):
            sf.exchange_code_for_tokens("c1", False, "bad", "https://cb")


class TestRefresh:
    @patch.object(sf, "_load_client_secret", return_value="s")
    @patch.object(
        sf,
        "_post_token",
        return_value=(
            200,
            {
                "access_token": "new-access",
                "instance_url": "https://acme.my.salesforce.com",
            },
        ),
    )
    def test_returns_access_and_instance_url(
        self, mock_post, mock_secret, mock_kms, mock_table,
    ):
        mock_kms.decrypt.return_value = {"Plaintext": b"stored-refresh"}
        # Two get_item calls: token row, then connection row
        mock_table.get_item.side_effect = [
            {"Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}},
            {"Item": {"is_sandbox": False, "instance_url": "https://old.my.salesforce.com"}},
        ]
        access, url = sf.refresh_access_token("ws1", "c1")
        assert access == "new-access"
        assert url == "https://acme.my.salesforce.com"

    @patch.object(sf, "_load_client_secret", return_value="s")
    @patch.object(sf, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    def test_invalid_grant_marks_reauth_and_raises(
        self, mock_post, mock_secret, mock_kms, mock_table,
    ):
        mock_kms.decrypt.return_value = {"Plaintext": b"stale"}
        mock_table.get_item.side_effect = [
            {"Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}},
            {"Item": {"is_sandbox": False}},
        ]
        with pytest.raises(ValueError, match="Token expired"):
            sf.refresh_access_token("ws1", "c1")
        # update_item called for the reauth_required mark
        calls = [
            c for c in mock_table.update_item.call_args_list
            if c.kwargs["Key"]["SK"].startswith("SALESFORCE_CONNECTION#")
        ]
        assert len(calls) == 1
        assert calls[0].kwargs["ExpressionAttributeValues"][":s"] == "reauth_required"

    @patch.object(sf, "_load_client_secret", return_value="s")
    @patch.object(
        sf, "_post_token",
        return_value=(200, {"access_token": "a", "refresh_token": "rotated", "instance_url": "u"}),
    )
    def test_rotates_refresh_when_returned(
        self, mock_post, mock_secret, mock_kms, mock_table,
    ):
        mock_kms.decrypt.return_value = {"Plaintext": b"old-refresh"}
        mock_table.get_item.side_effect = [
            {"Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}},
            {"Item": {"is_sandbox": False}},
        ]
        with patch.object(sf, "save_refresh_token") as mock_save:
            sf.refresh_access_token("ws1", "c1")
        mock_save.assert_called_once_with("c1", "rotated")
