"""Unit tests for shared/oauth/servicenow.py (chunk 02)."""

import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)

os.environ.setdefault("WORKSPACES_TABLE", "test-workspaces")
os.environ.setdefault("OAUTH_KMS_KEY_ARN", "arn:aws:kms:us-west-2:1:key/test")

import shared.oauth.servicenow as sn  # noqa: E402


@pytest.fixture(autouse=True)
def reset_globals():
    sn._kms_client = None
    sn._dynamodb = None
    yield


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


class TestCreds:
    def test_get_credentials_from_db_ok(self, mock_kms, mock_table):
        mock_kms.decrypt.return_value = {"Plaintext": b"secret-x"}
        def get_item(Key, **_):
            if Key["PK"].startswith("SERVICENOW_TOKEN#"):
                return {"Item": {"encrypted_client_secret": base64.b64encode(b"cipher").decode()}}
            if Key["SK"].startswith("SERVICENOW_CONNECTION#"):
                return {"Item": {"instance_url": "https://acme.service-now.com", "client_id": "abc"}}
            return {}
        mock_table.get_item.side_effect = get_item

        url, cid, secret = sn.get_credentials_from_db("ws1", "conn-1")
        assert url == "https://acme.service-now.com"
        assert cid == "abc"
        assert secret == "secret-x"

    def test_missing_token_row_raises(self, mock_table):
        mock_table.get_item.return_value = {}
        with pytest.raises(ValueError, match="No token row"):
            sn.get_credentials_from_db("ws1", "conn-1")

    def test_missing_client_secret_raises(self, mock_table):
        def get_item(Key, **_):
            if Key["PK"].startswith("SERVICENOW_TOKEN#"):
                # Row exists but the encrypted_client_secret field wasn't
                # written (e.g. mid-migration or corrupted row).
                return {"Item": {"entity_type": "servicenow_token"}}
            return {"Item": {"instance_url": "https://x", "client_id": "y"}}
        mock_table.get_item.side_effect = get_item
        with pytest.raises(ValueError, match="Missing client_secret"):
            sn.get_credentials_from_db("ws1", "conn-1")


class TestSaveTokens:
    def test_save_client_secret_kms_encrypts(self, mock_kms, mock_table):
        sn.save_client_secret("conn-1", "shhh")
        mock_kms.encrypt.assert_called_once()
        args = mock_kms.encrypt.call_args.kwargs
        assert args["Plaintext"] == b"shhh"
        mock_table.update_item.assert_called_once()

    def test_save_refresh_token_kms_encrypts(self, mock_kms, mock_table):
        sn.save_refresh_token("conn-1", "rf-x")
        mock_kms.encrypt.assert_called_once()
        assert mock_kms.encrypt.call_args.kwargs["Plaintext"] == b"rf-x"


class TestExchange:
    @patch.object(sn, "get_credentials_from_db", return_value=(
        "https://acme.service-now.com", "abc", "secret",
    ))
    @patch.object(sn, "_post_token", return_value=(200, {
        "access_token": "a", "refresh_token": "r", "expires_in": 1800,
    }))
    def test_ok(self, mock_post, mock_creds):
        access, refresh, expires = sn.exchange_code_for_tokens("ws1", "conn-1", "code", "https://cb")
        assert access == "a"
        assert refresh == "r"
        assert expires == 1800

    @patch.object(sn, "get_credentials_from_db", return_value=("u", "c", "s"))
    @patch.object(sn, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    def test_failure_raises(self, mock_post, mock_creds):
        with pytest.raises(ValueError, match="Token exchange failed"):
            sn.exchange_code_for_tokens("ws1", "conn-1", "bad", "https://cb")


class TestRefresh:
    @patch.object(sn, "_post_token", return_value=(200, {"access_token": "new", "refresh_token": "rotated"}))
    @patch.object(sn, "get_credentials_from_db", return_value=("u", "c", "s"))
    def test_rotates_refresh_token(self, mock_creds, mock_post, mock_kms, mock_table):
        mock_kms.decrypt.return_value = {"Plaintext": b"old-refresh"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with patch.object(sn, "save_refresh_token") as mock_save:
            token = sn.refresh_access_token("ws1", "conn-1")
        assert token == "new"
        mock_save.assert_called_once_with("conn-1", "rotated")

    @patch.object(sn, "_post_token", return_value=(200, {"access_token": "new"}))
    @patch.object(sn, "get_credentials_from_db", return_value=("u", "c", "s"))
    def test_missing_new_refresh_does_not_rewrite(self, mock_creds, mock_post, mock_kms, mock_table):
        mock_kms.decrypt.return_value = {"Plaintext": b"old-refresh"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with patch.object(sn, "save_refresh_token") as mock_save:
            token = sn.refresh_access_token("ws1", "conn-1")
        assert token == "new"
        mock_save.assert_not_called()

    @patch.object(sn, "_post_token", return_value=(400, {"error": "invalid_grant"}))
    @patch.object(sn, "get_credentials_from_db", return_value=("u", "c", "s"))
    def test_invalid_grant_marks_reauth_and_raises(self, mock_creds, mock_post, mock_kms, mock_table):
        mock_kms.decrypt.return_value = {"Plaintext": b"stale"}
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": base64.b64encode(b"c").decode()}
        }
        with pytest.raises(ValueError, match="Token expired"):
            sn.refresh_access_token("ws1", "conn-1")
        # The status flip is best-effort inside the token manager -- it uses
        # update_item; assert it happened.
        calls = [c for c in mock_table.update_item.call_args_list
                 if c.kwargs["Key"]["SK"].startswith("SERVICENOW_CONNECTION#")]
        assert len(calls) == 1
        assert calls[0].kwargs["ExpressionAttributeValues"][":s"] == "reauth_required"


class TestAuthorizeUrl:
    def test_shape(self):
        url = sn.build_authorize_url(
            instance_url="https://acme.service-now.com",
            client_id="clid",
            redirect_uri="https://ui/cb",
            state="XYZ",
        )
        assert url.startswith("https://acme.service-now.com/oauth_auth.do?")
        assert "response_type=code" in url
        assert "client_id=clid" in url
        assert "state=XYZ" in url
