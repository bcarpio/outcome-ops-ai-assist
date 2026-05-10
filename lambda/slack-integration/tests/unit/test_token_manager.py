"""Tests for slack-integration token_manager.py.

Slack bot tokens don't expire under our v1 install model — there's no
refresh-token dance. Tests verify KMS round-trip, save/delete, and that
refresh_access_token returns the stored token without an HTTP call.
"""

import base64
import importlib
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

FAKE_BOT_TOKEN = "xoxb-test-bot-token-value"
FAKE_CIPHERTEXT_BLOB = b"encrypted-bytes"
FAKE_ENCRYPTED_B64 = base64.b64encode(FAKE_CIPHERTEXT_BLOB).decode("utf-8")

_ENV_VARS = {
    "ENV": "test",
    "APP_NAME": "app.retrieveit.ai",
    "TENANTS_TABLE": "test-tenants",
    "SLACK_KMS_KEY_ARN": "arn:aws:kms:us-west-2:123:key/slack-key",
    "SLACK_CLIENT_ID": "test-client-id",
}


def _reload_module():
    for k, v in _ENV_VARS.items():
        os.environ[k] = v
    if "token_manager" in sys.modules:
        del sys.modules["token_manager"]
    return importlib.import_module("token_manager")


def _reset_globals(mod):
    for name in ["_kms_client", "_ssm_client", "_dynamodb", "_slack_client_secret"]:
        if hasattr(mod, name):
            setattr(mod, name, None)


def _build_mock_kms():
    m = MagicMock()
    m.encrypt.return_value = {"CiphertextBlob": FAKE_CIPHERTEXT_BLOB}
    m.decrypt.return_value = {"Plaintext": FAKE_BOT_TOKEN.encode("utf-8")}
    return m


def _build_mock_ssm():
    m = MagicMock()
    m.get_parameter.return_value = {"Parameter": {"Value": "mock-secret"}}
    return m


def _build_mock_table(get_item_return=None):
    m = MagicMock()
    m.get_item.return_value = get_item_return if get_item_return is not None else {}
    return m


def _build_mock_dynamodb(table):
    m = MagicMock()
    m.Table.return_value = table
    return m


class TestEncryptDecrypt:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_encrypt_token_returns_base64(self):
        with patch.object(self.mod, "_get_kms_client", return_value=_build_mock_kms()):
            assert self.mod.encrypt_token("plaintext-token") == FAKE_ENCRYPTED_B64

    def test_encrypt_passes_correct_key_arn(self):
        kms = _build_mock_kms()
        with patch.object(self.mod, "_get_kms_client", return_value=kms):
            self.mod.encrypt_token("token")
        assert kms.encrypt.call_args.kwargs["KeyId"] == _ENV_VARS["SLACK_KMS_KEY_ARN"]

    def test_decrypt_token_round_trip(self):
        with patch.object(self.mod, "_get_kms_client", return_value=_build_mock_kms()):
            assert self.mod.decrypt_token(FAKE_ENCRYPTED_B64) == FAKE_BOT_TOKEN

    def test_decrypt_passes_correct_key_arn(self):
        kms = _build_mock_kms()
        with patch.object(self.mod, "_get_kms_client", return_value=kms):
            self.mod.decrypt_token(FAKE_ENCRYPTED_B64)
        assert kms.decrypt.call_args.kwargs["KeyId"] == _ENV_VARS["SLACK_KMS_KEY_ARN"]


class TestSaveAndDeleteTokens:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_save_tokens_writes_dynamodb_row(self):
        table = _build_mock_table()
        with patch.object(self.mod, "_get_dynamodb", return_value=_build_mock_dynamodb(table)), \
             patch.object(self.mod, "_get_kms_client", return_value=_build_mock_kms()):
            self.mod.save_tokens("conn1", "xoxb-plaintext")
        item = table.put_item.call_args.kwargs["Item"]
        assert item["PK"] == "SLACK_TOKEN#conn1"
        assert item["SK"] == "CREDENTIALS"
        assert item["entity_type"] == "slack_token"
        assert item["encrypted_bot_token"] == FAKE_ENCRYPTED_B64

    def test_delete_tokens_removes_dynamodb_row(self):
        table = _build_mock_table()
        with patch.object(self.mod, "_get_dynamodb", return_value=_build_mock_dynamodb(table)):
            self.mod.delete_tokens("conn1")
        table.delete_item.assert_called_once_with(
            Key={"PK": "SLACK_TOKEN#conn1", "SK": "CREDENTIALS"}
        )


class TestRefreshAccessToken:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_returns_decrypted_stored_token_no_api_call(self):
        """Slack bot tokens don't expire — refresh_access_token must NOT
        make an HTTP call. This is the "Slack tokens don't refresh" guard."""
        table = _build_mock_table({"Item": {"encrypted_bot_token": FAKE_ENCRYPTED_B64}})
        with patch.object(self.mod, "_get_dynamodb", return_value=_build_mock_dynamodb(table)), \
             patch.object(self.mod, "_get_kms_client", return_value=_build_mock_kms()):
            token = self.mod.refresh_access_token("conn1")
        assert token == FAKE_BOT_TOKEN

    def test_requests_module_not_imported(self):
        """Static guard: token_manager.py must not import requests, since
        Slack bot tokens don't refresh. If someone adds an HTTP call here
        in the future, this test fails and forces a deliberate decision."""
        assert not hasattr(self.mod, "requests"), (
            "token_manager imported requests — Slack bot tokens don't expire, "
            "no HTTP call should happen during refresh"
        )

    def test_no_token_row_raises(self):
        table = _build_mock_table({})
        with patch.object(self.mod, "_get_dynamodb", return_value=_build_mock_dynamodb(table)), \
             pytest.raises(ValueError, match="No token found"):
            self.mod.refresh_access_token("missing")

    def test_empty_encrypted_field_raises(self):
        table = _build_mock_table({"Item": {"some_other_field": "x"}})
        with patch.object(self.mod, "_get_dynamodb", return_value=_build_mock_dynamodb(table)), \
             pytest.raises(ValueError, match="No bot token"):
            self.mod.refresh_access_token("conn1")


class TestClientCredentials:

    @pytest.fixture(autouse=True)
    def _setup(self):
        self.mod = _reload_module()
        _reset_globals(self.mod)

    def test_client_id_from_env(self):
        assert self.mod._get_client_id() == "test-client-id"

    def test_client_secret_loaded_from_ssm_path(self):
        ssm = _build_mock_ssm()
        with patch.object(self.mod, "_get_ssm_client", return_value=ssm):
            assert self.mod._get_client_secret() == "mock-secret"
        call_kwargs = ssm.get_parameter.call_args.kwargs
        assert call_kwargs["Name"] == "/test/app.retrieveit.ai/secrets/slack/client_secret"
        assert call_kwargs["WithDecryption"] is True

    def test_client_secret_cached_across_calls(self):
        ssm = _build_mock_ssm()
        with patch.object(self.mod, "_get_ssm_client", return_value=ssm):
            self.mod._get_client_secret()
            self.mod._get_client_secret()
        assert ssm.get_parameter.call_count == 1
