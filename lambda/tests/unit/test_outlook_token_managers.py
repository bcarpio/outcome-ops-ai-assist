# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for outlook-integration and outlook-sync token_manager.py files."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_module(mod_name, filename, directory):
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    mod_dir = os.path.join(os.path.dirname(__file__), directory)
    if mod_dir not in sys.path:
        sys.path.insert(0, mod_dir)

    mod_path = os.path.join(mod_dir, filename)
    spec = importlib.util.spec_from_file_location(mod_name, mod_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


outlook_int_tm = _load_module(
    "outlook_int_token_mgr", "token_manager.py", "../../outlook-integration"
)
outlook_sync_tm = _load_module(
    "outlook_sync_token_mgr", "token_manager.py", "../../outlook-sync"
)

# Shared Entra ID OAuth helper (ADR-035):
import shared.entra_id_oauth as shared_oauth  # noqa: E402


# ============================================================================
# Helpers
# ============================================================================

def _make_urlopen_response(body_dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================================
# Parametrized tests for both modules
# ============================================================================

@pytest.fixture(params=["integration", "sync"])
def tm(request):
    """Yield each token manager module in turn."""
    if request.param == "integration":
        mod = outlook_int_tm
    else:
        mod = outlook_sync_tm
    # Reset globals
    mod._dynamodb = None
    mod._kms = None
    yield mod


class TestOutlookTokenManagerEncryptDecrypt:

    def test_encrypt_token(self, tm):
        with patch.object(shared_oauth, "_get_kms") as mock_kms_fn:
            mock_kms = MagicMock()
            mock_kms.encrypt.return_value = {"CiphertextBlob": b"encrypted"}
            mock_kms_fn.return_value = mock_kms

            result = shared_oauth._encrypt_token("my_token", "arn:aws:kms:us-east-1:123456:key/test-key")
            assert base64.b64decode(result) == b"encrypted"

    def test_decrypt_token(self, tm):
        with patch.object(shared_oauth, "_get_kms") as mock_kms_fn:
            mock_kms = MagicMock()
            mock_kms.decrypt.return_value = {"Plaintext": b"decrypted"}
            mock_kms_fn.return_value = mock_kms

            encrypted = base64.b64encode(b"cipher").decode()
            result = shared_oauth._decrypt_token(encrypted)
            assert result == "decrypted"


class TestOutlookTokenManagerSaveTokens:

    def test_save_tokens_success(self, tm):
        with patch.object(shared_oauth, "_encrypt_token") as mock_enc, \
             patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_enc.return_value = "encrypted"

            result = tm.save_tokens("conn1", "access", "refresh", 3600)
            assert result is True
            item = mock_table.put_item.call_args[1]["Item"]
            assert item["PK"] == "OUTLOOK_TOKEN#conn1"
            assert item["SK"] == "CREDENTIALS"

    def test_save_tokens_error(self, tm):
        with patch.object(shared_oauth, "_encrypt_token") as mock_enc, \
             patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_enc.return_value = "enc"
            mock_table.put_item.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "err"}}, "PutItem"
            )
            assert tm.save_tokens("conn1", "a", "r") is False


class TestOutlookTokenManagerGetRefreshToken:

    def test_get_refresh_token_success(self, tm):
        with patch.object(shared_oauth, "_decrypt_token") as mock_dec, \
             patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.get_item.return_value = {"Item": {"encrypted_refresh_token": "enc"}}
            mock_dec.return_value = "plain"
            assert tm.get_refresh_token("conn1") == "plain"

    def test_get_refresh_token_not_found(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.get_item.return_value = {}
            assert tm.get_refresh_token("conn1") is None

    def test_get_refresh_token_no_field(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.get_item.return_value = {"Item": {}}
            assert tm.get_refresh_token("conn1") is None

    def test_get_refresh_token_empty_field(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.get_item.return_value = {"Item": {"encrypted_refresh_token": ""}}
            assert tm.get_refresh_token("conn1") is None

    def test_get_refresh_token_error(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.get_item.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "err"}}, "GetItem"
            )
            assert tm.get_refresh_token("conn1") is None


class TestOutlookTokenManagerRefreshAccessToken:

    def test_refresh_success(self, tm):
        mod_name = tm.__name__
        with patch.object(shared_oauth, "save_tokens") as mock_save, \
             patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen, \
             patch.object(shared_oauth, "get_refresh_token") as mock_get:
            mock_get.return_value = "old_refresh"
            mock_urlopen.return_value = _make_urlopen_response({
                "access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600,
            })
            result = tm.refresh_access_token("conn1", "cid", "csec")
            assert result == "new_at"
            mock_save.assert_called_once()

    def test_refresh_no_stored_token(self, tm):
        with patch.object(shared_oauth, "get_refresh_token") as mock_get:
            mock_get.return_value = None
            assert tm.refresh_access_token("conn1", "cid", "csec") is None

    def test_refresh_no_access_in_response(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen, \
             patch.object(shared_oauth, "get_refresh_token") as mock_get:
            mock_get.return_value = "refresh"
            mock_urlopen.return_value = _make_urlopen_response({"error": "bad"})
            assert tm.refresh_access_token("conn1", "cid", "csec") is None

    def test_refresh_http_error(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen, \
             patch.object(shared_oauth, "get_refresh_token") as mock_get:
            mock_get.return_value = "refresh"
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://example.com", code=400, msg="Bad",
                hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'invalid_grant')),
            )
            assert tm.refresh_access_token("conn1", "cid", "csec") is None

    def test_refresh_url_error(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen, \
             patch.object(shared_oauth, "get_refresh_token") as mock_get:
            mock_get.return_value = "refresh"
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            assert tm.refresh_access_token("conn1", "cid", "csec") is None


class TestOutlookTokenManagerDeleteTokens:

    def test_delete_success(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            assert tm.delete_tokens("conn1") is True
            mock_table.delete_item.assert_called_once_with(
                Key={"PK": "OUTLOOK_TOKEN#conn1", "SK": "CREDENTIALS"}
            )

    def test_delete_error(self, tm):
        with patch.object(shared_oauth, "_get_dynamodb") as mock_db_fn:
            mock_table = MagicMock()
            mock_db = MagicMock()
            mock_db.Table.return_value = mock_table
            mock_db_fn.return_value = mock_db
            mock_table.delete_item.side_effect = ClientError(
                {"Error": {"Code": "500", "Message": "err"}}, "DeleteItem"
            )
            assert tm.delete_tokens("conn1") is False


class TestOutlookTokenManagerExchangeCode:

    def test_exchange_success(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({
                "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
            })
            a, r, e = tm.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
            assert a == "at"
            assert r == "rt"
            assert e == 3600

    def test_exchange_http_error(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.HTTPError(
                url="https://example.com", code=400, msg="Bad",
                hdrs={}, fp=MagicMock(read=MagicMock(return_value=b'err')),
            )
            a, r, e = tm.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
            assert a is None

    def test_exchange_url_error(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.side_effect = urllib.error.URLError("Connection refused")
            a, r, e = tm.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
            assert a is None

    def test_exchange_default_expires(self, tm):
        mod_name = tm.__name__
        with patch("shared.entra_id_oauth.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value = _make_urlopen_response({
                "access_token": "at", "refresh_token": "rt",
            })
            _, _, e = tm.exchange_code_for_tokens("code", "cid", "csec", "https://cb")
            assert e == 3600


class TestOutlookTokenManagerClientCaching:

    def test_dynamodb_caches(self, tm):
        shared_oauth._dynamodb = None
        with patch.object(shared_oauth, "boto3") as mock_boto3:
            mock_boto3.resource.return_value = MagicMock()
            first = shared_oauth._get_dynamodb()
            second = shared_oauth._get_dynamodb()
            assert first is second
            mock_boto3.resource.assert_called_once()

    def test_kms_caches(self, tm):
        shared_oauth._kms = None
        with patch.object(shared_oauth, "boto3") as mock_boto3:
            mock_boto3.client.return_value = MagicMock()
            first = shared_oauth._get_kms()
            second = shared_oauth._get_kms()
            assert first is second
            mock_boto3.client.assert_called_once()
