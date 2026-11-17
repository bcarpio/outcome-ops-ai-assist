# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for Confluence token manager (token_manager.py)."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading
# ============================================================================

def _load_token_module():
    """Load or reload the token_manager module from confluence-integration."""
    if 'confluence_token_manager' in sys.modules:
        del sys.modules['confluence_token_manager']

    token_path = os.path.join(
        os.path.dirname(__file__),
        '../../confluence-integration/token_manager.py',
    )
    spec = importlib.util.spec_from_file_location("confluence_token_manager", token_path)
    token_module = importlib.util.module_from_spec(spec)
    sys.modules['confluence_token_manager'] = token_module
    spec.loader.exec_module(token_module)
    return token_module


token_module = _load_token_module()

# Import functions
save_tokens = token_module.save_tokens
get_refresh_token = token_module.get_refresh_token
refresh_access_token = token_module.refresh_access_token
delete_tokens = token_module.delete_tokens
exchange_code_for_tokens = token_module.exchange_code_for_tokens
_encrypt_token = token_module._encrypt_token
_decrypt_token = token_module._decrypt_token


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_module_globals():
    """Reset cached module globals between tests."""
    token_module._dynamodb = None
    token_module._kms = None
    yield


@pytest.fixture
def mock_kms():
    """Create mock KMS client."""
    mock = MagicMock()
    mock.encrypt.return_value = {
        "CiphertextBlob": b"encrypted_data"
    }
    mock.decrypt.return_value = {
        "Plaintext": b"decrypted_token_value"
    }
    return mock


@pytest.fixture
def mock_table():
    """Create mock DynamoDB table."""
    return MagicMock()


@pytest.fixture
def mock_dynamodb(mock_table):
    """Create mock DynamoDB resource."""
    mock = MagicMock()
    mock.Table.return_value = mock_table
    return mock


# ============================================================================
# Encrypt/Decrypt
# ============================================================================

class TestEncryptDecrypt:
    """Test token encryption and decryption."""

    @patch.object(token_module, '_get_kms')
    def test_encrypt_token_returns_base64(self, mock_kms_fn, mock_kms):
        """Test encrypting a token returns base64-encoded string."""
        mock_kms_fn.return_value = mock_kms

        result = _encrypt_token("my_secret_token")
        assert isinstance(result, str)
        # Should be valid base64
        decoded = base64.b64decode(result)
        assert decoded == b"encrypted_data"
        mock_kms.encrypt.assert_called_once()

    @patch.object(token_module, '_get_kms')
    def test_encrypt_token_calls_kms_with_correct_args(self, mock_kms_fn, mock_kms):
        """Test encrypt passes correct KeyId and Plaintext to KMS."""
        mock_kms_fn.return_value = mock_kms
        token_module.KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456:key/test-key"

        _encrypt_token("secret_value")

        mock_kms.encrypt.assert_called_once_with(
            KeyId="arn:aws:kms:us-east-1:123456:key/test-key",
            Plaintext=b"secret_value",
        )

    @patch.object(token_module, '_get_kms')
    def test_decrypt_token_returns_plaintext(self, mock_kms_fn, mock_kms):
        """Test decrypting a token returns plain text."""
        mock_kms_fn.return_value = mock_kms

        encrypted = base64.b64encode(b"encrypted_data").decode("utf-8")
        result = _decrypt_token(encrypted)
        assert result == "decrypted_token_value"
        mock_kms.decrypt.assert_called_once()

    @patch.object(token_module, '_get_kms')
    def test_decrypt_token_passes_decoded_ciphertext(self, mock_kms_fn, mock_kms):
        """Test decrypt passes base64-decoded ciphertext blob to KMS."""
        mock_kms_fn.return_value = mock_kms
        raw_bytes = b"some_cipher_blob"
        encrypted = base64.b64encode(raw_bytes).decode("utf-8")

        _decrypt_token(encrypted)

        mock_kms.decrypt.assert_called_once_with(
            CiphertextBlob=raw_bytes,
        )


# ============================================================================
# Save Tokens
# ============================================================================

class TestSaveTokens:
    """Test saving OAuth tokens."""

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test saving tokens stores encrypted refresh token in DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted_refresh"

        result = save_tokens("conn_test", "access_tok", "refresh_tok", 3600)
        assert result is True
        mock_table.put_item.assert_called_once()

        item = mock_table.put_item.call_args[1]["Item"]
        assert item["PK"] == "CONFLUENCE_TOKEN#conn_test"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"
        assert item["connection_id"] == "conn_test"

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_includes_expires_at(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test saved item includes expires_at as integer timestamp."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        save_tokens("conn_test", "access", "refresh", 7200)

        item = mock_table.put_item.call_args[1]["Item"]
        assert isinstance(item["expires_at"], int)
        assert "updated_at" in item

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_default_expires(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test save_tokens uses default expiry of 3600 seconds."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        result = save_tokens("conn_test", "access", "refresh")
        assert result is True
        mock_table.put_item.assert_called_once()

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_dynamodb_error(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}}, "PutItem"
        )

        result = save_tokens("conn_test", "access", "refresh")
        assert result is False

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_encrypts_refresh_not_access(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test only the refresh token is encrypted and saved."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted_refresh"

        save_tokens("conn_test", "access_tok", "refresh_tok", 3600)

        # _encrypt_token should be called with the refresh token
        mock_encrypt.assert_called_once_with("refresh_tok")
        # The access token should not appear in the saved item
        item = mock_table.put_item.call_args[1]["Item"]
        assert "access_tok" not in item.values()


# ============================================================================
# Get Refresh Token
# ============================================================================

class TestGetRefreshToken:
    """Test retrieving refresh tokens."""

    @patch.object(token_module, '_decrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt, mock_dynamodb, mock_table):
        """Test getting and decrypting refresh token."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "encrypted_value"}
        }
        mock_decrypt.return_value = "plain_refresh_token"

        result = get_refresh_token("conn_test")
        assert result == "plain_refresh_token"
        mock_decrypt.assert_called_once_with("encrypted_value")

    @patch.object(token_module, '_decrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_queries_correct_key(self, mock_db_fn, mock_decrypt, mock_dynamodb, mock_table):
        """Test the DynamoDB query uses CONFLUENCE_TOKEN# prefix."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "enc"}
        }
        mock_decrypt.return_value = "tok"

        get_refresh_token("my-connection-123")

        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "CONFLUENCE_TOKEN#my-connection-123",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_not_found(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when no item found in DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_refresh_token("nonexistent")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when encrypted_refresh_token field is missing."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {}}

        result = get_refresh_token("conn_test")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_empty_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when encrypted_refresh_token is empty string."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": ""}
        }

        result = get_refresh_token("conn_test")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None on DynamoDB ClientError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "GetItem"
        )

        result = get_refresh_token("conn_test")
        assert result is None


# ============================================================================
# Refresh Access Token
# ============================================================================

class TestRefreshAccessToken:
    """Test refreshing access tokens via Atlassian token endpoint."""

    @patch.object(token_module, 'save_tokens')
    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_success_rolling_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test successful token refresh with new rolling refresh token."""
        mock_get_refresh.return_value = "old_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result == "new_access_token"
        mock_save.assert_called_once_with(
            "conn_test", "new_access_token", "new_refresh_token", 3600
        )

    @patch.object(token_module, 'save_tokens')
    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_success_same_refresh_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test successful refresh when refresh token is unchanged (same token)."""
        mock_get_refresh.return_value = "same_refresh_token"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "same_refresh_token",
            "expires_in": 7200,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result == "new_access_token"
        # Even with same token, save_tokens is called to update timestamp
        mock_save.assert_called_once_with(
            "conn_test", "new_access_token", "same_refresh_token", 7200
        )

    @patch.object(token_module, 'save_tokens')
    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_refresh_token_in_response(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test refresh returns access token but does not save when no refresh token in response."""
        mock_get_refresh.return_value = "old_refresh"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result == "new_access_token"
        # No refresh token in response means save_tokens should NOT be called
        mock_save.assert_not_called()

    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_stored_refresh_token(self, mock_get_refresh):
        """Test returns None when no refresh token is stored."""
        mock_get_refresh.return_value = None

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on HTTP error from Atlassian."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error_invalid_grant(self, mock_get_refresh, mock_urlopen):
        """Test handles invalid_grant error (revoked token) gracefully."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error_no_body(self, mock_get_refresh, mock_urlopen):
        """Test handles HTTP error when fp is None (no body)."""
        mock_get_refresh.return_value = "refresh_token"
        error = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = error

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_network_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on network/URL error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_access_token_in_response(self, mock_get_refresh, mock_urlopen):
        """Test returns None when response lacks access_token."""
        mock_get_refresh.return_value = "refresh_token"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({"error": "something"}).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch.object(token_module, 'save_tokens')
    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_sends_correct_payload(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test refresh sends correct grant_type and parameters to Atlassian."""
        mock_get_refresh.return_value = "my_refresh_tok"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_at",
            "refresh_token": "new_rt",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        refresh_access_token("conn_test", "my_client", "my_secret")

        # Verify the request was created with correct URL
        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert request_obj.full_url == "https://auth.atlassian.com/oauth/token"
        assert request_obj.method == "POST"

        # Verify the posted data contains correct fields
        import urllib.parse
        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        assert posted_data["grant_type"] == ["refresh_token"]
        assert posted_data["client_id"] == ["my_client"]
        assert posted_data["client_secret"] == ["my_secret"]
        assert posted_data["refresh_token"] == ["my_refresh_tok"]

    @patch.object(token_module, 'save_tokens')
    @patch('confluence_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_default_expires_in(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test defaults to 3600 when expires_in not in response."""
        mock_get_refresh.return_value = "old_refresh"

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_at",
            "refresh_token": "new_rt",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test", "cid", "csecret")
        assert result == "new_at"
        mock_save.assert_called_once_with("conn_test", "new_at", "new_rt", 3600)


# ============================================================================
# Delete Tokens
# ============================================================================

class TestDeleteTokens:
    """Test deleting tokens."""

    @patch.object(token_module, '_get_dynamodb')
    def test_delete_tokens_success(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test deleting tokens from DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb

        result = delete_tokens("conn_test")
        assert result is True
        mock_table.delete_item.assert_called_once_with(
            Key={
                "PK": "CONFLUENCE_TOKEN#conn_test",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(token_module, '_get_dynamodb')
    def test_delete_tokens_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "DeleteItem"
        )

        result = delete_tokens("conn_test")
        assert result is False

    @patch.object(token_module, '_get_dynamodb')
    def test_delete_tokens_uses_correct_key(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test delete uses CONFLUENCE_TOKEN# prefix in PK."""
        mock_db_fn.return_value = mock_dynamodb

        delete_tokens("my-conn-456")

        mock_table.delete_item.assert_called_once_with(
            Key={
                "PK": "CONFLUENCE_TOKEN#my-conn-456",
                "SK": "CREDENTIALS",
            }
        )


# ============================================================================
# Exchange Code for Tokens
# ============================================================================

class TestExchangeCodeForTokens:
    """Test exchanging authorization code for tokens."""

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_success(self, mock_urlopen):
        """Test successful code exchange returns token tuple."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "access_tok",
            "refresh_token": "refresh_tok",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        access, refresh, expires = exchange_code_for_tokens(
            "auth_code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access == "access_tok"
        assert refresh == "refresh_tok"
        assert expires == 3600

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_http_error(self, mock_urlopen):
        """Test returns None tuple on HTTP error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_code"}')),
        )

        access, refresh, expires = exchange_code_for_tokens(
            "bad_code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_http_error_no_body(self, mock_urlopen):
        """Test returns None tuple on HTTP error with no response body."""
        error = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = error

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_network_error(self, mock_urlopen):
        """Test returns None tuple on network error."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_default_expires(self, mock_urlopen):
        """Test defaults to 3600 when expires_in not in response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _, _, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert expires == 3600

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_sends_correct_payload(self, mock_urlopen):
        """Test exchange sends correct grant_type and parameters to Atlassian."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        exchange_code_for_tokens(
            "my_auth_code", "my_client", "my_secret", "https://app.example.com/oauth/callback"
        )

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert request_obj.full_url == "https://auth.atlassian.com/oauth/token"
        assert request_obj.method == "POST"

        import urllib.parse
        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        assert posted_data["grant_type"] == ["authorization_code"]
        assert posted_data["client_id"] == ["my_client"]
        assert posted_data["client_secret"] == ["my_secret"]
        assert posted_data["code"] == ["my_auth_code"]
        assert posted_data["redirect_uri"] == ["https://app.example.com/oauth/callback"]

    @patch('confluence_token_manager.urllib.request.urlopen')
    def test_exchange_missing_fields_in_response(self, mock_urlopen):
        """Test returns None for missing fields in response."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({}).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        # expires_in defaults to 3600
        assert expires == 3600


# ============================================================================
# Confluence-Sync copy (identical file, needs separate coverage)
# ============================================================================

def _load_sync_token_module():
    """Load the token_manager module from confluence-sync."""
    mod_name = "confluence_sync_token_manager"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    token_path = os.path.join(
        os.path.dirname(__file__),
        '../../confluence-sync/token_manager.py',
    )
    spec = importlib.util.spec_from_file_location(mod_name, token_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


sync_module = _load_sync_token_module()


class TestSyncEncryptDecrypt:
    """Smoke tests for confluence-sync/token_manager.py encrypt/decrypt."""

    @patch.object(sync_module, '_get_kms')
    def test_encrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.encrypt.return_value = {"CiphertextBlob": b"encrypted"}
        mock_kms_fn.return_value = mock_kms
        result = sync_module._encrypt_token("secret")
        assert isinstance(result, str)

    @patch.object(sync_module, '_get_kms')
    def test_decrypt_token(self, mock_kms_fn):
        mock_kms = MagicMock()
        mock_kms.decrypt.return_value = {"Plaintext": b"decrypted"}
        mock_kms_fn.return_value = mock_kms
        encrypted = base64.b64encode(b"data").decode("utf-8")
        result = sync_module._decrypt_token(encrypted)
        assert result == "decrypted"


class TestSyncSaveTokens:
    """Smoke tests for confluence-sync/token_manager.py save_tokens."""

    def setup_method(self):
        sync_module._dynamodb = None
        sync_module._kms = None

    @patch.object(sync_module, '_encrypt_token')
    @patch.object(sync_module, '_get_dynamodb')
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted"
        result = sync_module.save_tokens("conn", "access", "refresh", 3600)
        assert result is True

    @patch.object(sync_module, '_encrypt_token')
    @patch.object(sync_module, '_get_dynamodb')
    def test_save_tokens_error(self, mock_db_fn, mock_encrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_encrypt.return_value = "encrypted"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "PutItem"
        )
        result = sync_module.save_tokens("conn", "access", "refresh")
        assert result is False


class TestSyncGetRefreshToken:
    """Smoke tests for confluence-sync/token_manager.py get_refresh_token."""

    def setup_method(self):
        sync_module._dynamodb = None
        sync_module._kms = None

    @patch.object(sync_module, '_decrypt_token')
    @patch.object(sync_module, '_get_dynamodb')
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {"Item": {"encrypted_refresh_token": "enc"}}
        mock_decrypt.return_value = "plain"
        result = sync_module.get_refresh_token("conn")
        assert result == "plain"

    @patch.object(sync_module, '_get_dynamodb')
    def test_get_refresh_token_not_found(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.get_item.return_value = {}
        result = sync_module.get_refresh_token("nonexistent")
        assert result is None


class TestSyncRefreshAccessToken:
    """Smoke tests for confluence-sync/token_manager.py refresh_access_token."""

    def setup_method(self):
        sync_module._dynamodb = None
        sync_module._kms = None

    @patch.object(sync_module, 'save_tokens')
    @patch('confluence_sync_token_manager.urllib.request.urlopen')
    @patch.object(sync_module, 'get_refresh_token')
    def test_refresh_success(self, mock_get_refresh, mock_urlopen, mock_save):
        mock_get_refresh.return_value = "old_refresh"
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_at", "refresh_token": "new_rt", "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        result = sync_module.refresh_access_token("conn", "cid", "csecret")
        assert result == "new_at"

    @patch.object(sync_module, 'get_refresh_token')
    def test_refresh_no_stored_token(self, mock_get_refresh):
        mock_get_refresh.return_value = None
        result = sync_module.refresh_access_token("conn", "cid", "csecret")
        assert result is None


class TestSyncDeleteTokens:
    """Smoke tests for confluence-sync/token_manager.py delete_tokens."""

    def setup_method(self):
        sync_module._dynamodb = None
        sync_module._kms = None

    @patch.object(sync_module, '_get_dynamodb')
    def test_delete_success(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        result = sync_module.delete_tokens("conn")
        assert result is True

    @patch.object(sync_module, '_get_dynamodb')
    def test_delete_error(self, mock_db_fn):
        mock_table = MagicMock()
        mock_db = MagicMock()
        mock_db.Table.return_value = mock_table
        mock_db_fn.return_value = mock_db
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "err"}}, "DeleteItem"
        )
        result = sync_module.delete_tokens("conn")
        assert result is False


class TestSyncExchangeCode:
    """Smoke tests for confluence-sync/token_manager.py exchange_code_for_tokens."""

    @patch('confluence_sync_token_manager.urllib.request.urlopen')
    def test_exchange_success(self, mock_urlopen):
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "at", "refresh_token": "rt", "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response
        access, refresh, expires = sync_module.exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/cb"
        )
        assert access == "at"
        assert refresh == "rt"

    @patch('confluence_sync_token_manager.urllib.request.urlopen')
    def test_exchange_error(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400, msg="Bad", hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "bad"}')),
        )
        access, refresh, expires = sync_module.exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/cb"
        )
        assert access is None
