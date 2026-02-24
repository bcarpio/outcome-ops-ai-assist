"""Unit tests for Outlook token manager (token_manager.py)."""

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

def _load_token_module():
    """Load or reload the token_manager module."""
    if 'outlook_token_manager' in sys.modules:
        del sys.modules['outlook_token_manager']

    token_path = os.path.join(os.path.dirname(__file__), '../../outlook-integration/token_manager.py')
    spec = importlib.util.spec_from_file_location("outlook_token_manager", token_path)
    token_module = importlib.util.module_from_spec(spec)
    sys.modules['outlook_token_manager'] = token_module
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
    def test_encrypt_token(self, mock_kms_fn, mock_kms):
        """Test encrypting a token returns base64-encoded string."""
        mock_kms_fn.return_value = mock_kms

        result = _encrypt_token("my_secret_token")
        assert isinstance(result, str)
        # Should be valid base64
        base64.b64decode(result)
        mock_kms.encrypt.assert_called_once()

    @patch.object(token_module, '_get_kms')
    def test_decrypt_token(self, mock_kms_fn, mock_kms):
        """Test decrypting a token returns plain text."""
        mock_kms_fn.return_value = mock_kms

        encrypted = base64.b64encode(b"encrypted_data").decode("utf-8")
        result = _decrypt_token(encrypted)
        assert result == "decrypted_token_value"
        mock_kms.decrypt.assert_called_once()


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
        assert item["PK"] == "OUTLOOK_TOKEN#conn_test"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"
        assert item["connection_id"] == "conn_test"

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_dynamodb_error(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test returns False on DynamoDB error."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "PutItem"
        )

        result = save_tokens("conn_test", "access", "refresh")
        assert result is False


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

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_not_found(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when token not found."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_refresh_token("nonexistent")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when encrypted field is missing."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {}}

        result = get_refresh_token("conn_test")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None on DynamoDB error."""
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
    """Test refreshing access tokens via Microsoft token endpoint."""

    @patch.object(token_module, 'save_tokens')
    @patch('outlook_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_success(self, mock_get_refresh, mock_urlopen, mock_save):
        """Test successful token refresh."""
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
        mock_save.assert_called_once_with("conn_test", "new_access_token", "new_refresh_token", 3600)

    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_refresh_token(self, mock_get_refresh):
        """Test returns None when no refresh token stored."""
        mock_get_refresh.return_value = None

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('outlook_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on HTTP error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/...",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('outlook_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_network_error(self, mock_get_refresh, mock_urlopen):
        """Test returns None on network error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = refresh_access_token("conn_test", "client_id", "client_secret")
        assert result is None

    @patch('outlook_token_manager.urllib.request.urlopen')
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
                "PK": "OUTLOOK_TOKEN#conn_test",
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


# ============================================================================
# Exchange Code for Tokens
# ============================================================================

class TestExchangeCodeForTokens:
    """Test exchanging authorization code for tokens."""

    @patch('outlook_token_manager.urllib.request.urlopen')
    def test_exchange_success(self, mock_urlopen):
        """Test successful code exchange."""
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

    @patch('outlook_token_manager.urllib.request.urlopen')
    def test_exchange_http_error(self, mock_urlopen):
        """Test returns None tuple on HTTP error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/...",
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

    @patch('outlook_token_manager.urllib.request.urlopen')
    def test_exchange_network_error(self, mock_urlopen):
        """Test returns None tuple on network error."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('outlook_token_manager.urllib.request.urlopen')
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
