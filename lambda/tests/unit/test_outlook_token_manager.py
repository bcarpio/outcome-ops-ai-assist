"""Unit tests for file-ingestion/outlook_token_manager.py."""

import base64
import importlib.util
import json
import os
import sys
import urllib.error
import urllib.parse
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError


# ============================================================================
# Module loading -- handle sys.modules pollution from test_check_handlers_init
# ============================================================================

def _load_token_module():
    """Load or reload the outlook_token_manager module from file-ingestion."""
    mod_name = "fi_outlook_token_manager"

    # Clean up any prior registration
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    # If boto3 has been replaced with a MagicMock by another test file,
    # pre-register the sub-modules that the real boto3 exposes so that
    # ``from botocore.exceptions import ClientError`` works at import time.
    if isinstance(sys.modules.get("boto3"), MagicMock):
        sys.modules.setdefault("boto3.dynamodb", MagicMock())
        sys.modules.setdefault("boto3.dynamodb.conditions", MagicMock())

    token_path = os.path.join(
        os.path.dirname(__file__),
        "../../file-ingestion/outlook_token_manager.py",
    )
    spec = importlib.util.spec_from_file_location(mod_name, token_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = module
    spec.loader.exec_module(module)
    return module


token_module = _load_token_module()

# Shared Entra ID OAuth helper (ADR-035 chunk 01a):
import shared.entra_id_oauth as shared_oauth  # noqa: E402

# Convenience aliases
save_tokens = token_module.save_tokens
get_refresh_token = token_module.get_refresh_token
refresh_access_token = token_module.refresh_access_token
delete_tokens = token_module.delete_tokens
exchange_code_for_tokens = token_module.exchange_code_for_tokens
_encrypt_token = shared_oauth._encrypt_token
_decrypt_token = shared_oauth._decrypt_token


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_module_globals():
    """Reset cached boto3 clients between tests."""
    shared_oauth._dynamodb = None
    shared_oauth._kms = None
    yield


@pytest.fixture
def mock_kms():
    """Create a mock KMS client with sensible defaults."""
    mock = MagicMock()
    mock.encrypt.return_value = {
        "CiphertextBlob": b"encrypted_data",
    }
    mock.decrypt.return_value = {
        "Plaintext": b"decrypted_token_value",
    }
    return mock


@pytest.fixture
def mock_table():
    """Create a mock DynamoDB table."""
    return MagicMock()


@pytest.fixture
def mock_dynamodb(mock_table):
    """Create a mock DynamoDB resource whose .Table() returns mock_table."""
    mock = MagicMock()
    mock.Table.return_value = mock_table
    return mock


# ============================================================================
# Helper -- build a urllib context-manager response
# ============================================================================

def _make_urlopen_response(body_dict):
    """Return a MagicMock that behaves like urllib.request.urlopen().__enter__()."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


# ============================================================================
# Encrypt / Decrypt
# ============================================================================

class TestEncryptDecrypt:
    """Test token encryption and decryption via KMS."""

    @patch.object(shared_oauth, "_get_kms")
    def test_encrypt_token_returns_base64(self, mock_kms_fn, mock_kms):
        """Encrypting a token should return a valid base64-encoded string."""
        mock_kms_fn.return_value = mock_kms

        result = _encrypt_token("my_secret_token", "arn:aws:kms:us-east-1:123456:key/test-key")

        assert isinstance(result, str)
        decoded = base64.b64decode(result)
        assert decoded == b"encrypted_data"
        mock_kms.encrypt.assert_called_once()

    @patch.object(shared_oauth, "_get_kms")
    def test_encrypt_token_passes_correct_args(self, mock_kms_fn, mock_kms):
        """Encrypt should pass the KMS_KEY_ARN and utf-8 encoded plaintext."""
        mock_kms_fn.return_value = mock_kms
        token_module.KMS_KEY_ARN = "arn:aws:kms:us-east-1:123456:key/test-key"

        _encrypt_token("secret_value", "arn:aws:kms:us-east-1:123456:key/test-key")

        mock_kms.encrypt.assert_called_once_with(
            KeyId="arn:aws:kms:us-east-1:123456:key/test-key",
            Plaintext=b"secret_value",
        )

    @patch.object(shared_oauth, "_get_kms")
    def test_decrypt_token_returns_plaintext(self, mock_kms_fn, mock_kms):
        """Decrypting should return the plain-text string from KMS."""
        mock_kms_fn.return_value = mock_kms

        encrypted = base64.b64encode(b"encrypted_data").decode("utf-8")
        result = _decrypt_token(encrypted)

        assert result == "decrypted_token_value"
        mock_kms.decrypt.assert_called_once()

    @patch.object(shared_oauth, "_get_kms")
    def test_decrypt_token_passes_decoded_ciphertext(self, mock_kms_fn, mock_kms):
        """Decrypt should base64-decode the input before passing to KMS."""
        mock_kms_fn.return_value = mock_kms
        raw_bytes = b"some_cipher_blob"
        encrypted = base64.b64encode(raw_bytes).decode("utf-8")

        _decrypt_token(encrypted)

        mock_kms.decrypt.assert_called_once_with(CiphertextBlob=raw_bytes)


# ============================================================================
# Save Tokens
# ============================================================================

class TestSaveTokens:
    """Test saving OAuth tokens to DynamoDB."""

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_success(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Saving tokens should return True and store the correct item."""
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

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_includes_expires_at(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Saved item should contain an integer expires_at and an updated_at."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        save_tokens("conn_test", "access", "refresh", 7200)

        item = mock_table.put_item.call_args[1]["Item"]
        assert isinstance(item["expires_at"], int)
        assert "updated_at" in item

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_default_expires(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Omitting expires_in should default to 3600 and still succeed."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        result = save_tokens("conn_test", "access", "refresh")

        assert result is True
        mock_table.put_item.assert_called_once()

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_encrypts_refresh_not_access(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Only the refresh token should be encrypted; access token should not be stored."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted_refresh"

        save_tokens("conn_test", "access_tok", "refresh_tok", 3600)

        # _encrypt_token is called with (token, kms_key_arn). The
        # second arg is the wrapper's KMS_KEY_ARN module global; a
        # prior test (test_encrypt_token_passes_correct_args) sets
        # that global, so just check the first arg here.
        mock_encrypt.assert_called_once()
        assert mock_encrypt.call_args.args[0] == "refresh_tok"
        item = mock_table.put_item.call_args[1]["Item"]
        assert "access_tok" not in item.values()

    @patch.object(shared_oauth, "_encrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_save_tokens_dynamodb_error(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Should return False when DynamoDB raises ClientError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"
        mock_table.put_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Internal error"}}, "PutItem"
        )

        result = save_tokens("conn_test", "access", "refresh")

        assert result is False


# ============================================================================
# Get Refresh Token
# ============================================================================

class TestGetRefreshToken:
    """Test retrieving and decrypting refresh tokens from DynamoDB."""

    @patch.object(shared_oauth, "_decrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_success(self, mock_db_fn, mock_decrypt, mock_dynamodb, mock_table):
        """Should decrypt and return the stored refresh token."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "encrypted_value"}
        }
        mock_decrypt.return_value = "plain_refresh_token"

        result = get_refresh_token("conn_test")

        assert result == "plain_refresh_token"
        mock_decrypt.assert_called_once_with("encrypted_value")

    @patch.object(shared_oauth, "_decrypt_token")
    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_queries_correct_key(self, mock_db_fn, mock_decrypt, mock_dynamodb, mock_table):
        """DynamoDB query should use OUTLOOK_TOKEN# prefix."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "enc"}
        }
        mock_decrypt.return_value = "tok"

        get_refresh_token("my-connection-123")

        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "OUTLOOK_TOKEN#my-connection-123",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_not_found(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return None when no item exists in DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_refresh_token("nonexistent")

        assert result is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return None when encrypted_refresh_token field is missing."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {}}

        result = get_refresh_token("conn_test")

        assert result is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_empty_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return None when encrypted_refresh_token is an empty string."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": ""}
        }

        result = get_refresh_token("conn_test")

        assert result is None

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_get_refresh_token_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return None on DynamoDB ClientError."""
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
    """Test refreshing access tokens via the Microsoft token endpoint."""

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_success_with_new_refresh_token(self, mock_get_refresh, mock_urlopen, mock_save):
        """Successful refresh should return new access token and save new tokens."""
        mock_get_refresh.return_value = "old_refresh_token"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        })

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result == "new_access_token"
        mock_save.assert_called_once()
        # After the chunk-01a refactor, save_tokens is invoked with kwargs
        # because the shared module uses keyword arguments to keep the
        # call-site self-documenting.
        call_kwargs = mock_save.call_args.kwargs
        assert call_kwargs["connection_id"] == "conn_test"
        assert call_kwargs["access_token"] == "new_access_token"
        assert call_kwargs["refresh_token"] == "new_refresh_token"
        assert call_kwargs["expires_in"] == 3600

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_no_refresh_token_in_response(self, mock_get_refresh, mock_urlopen, mock_save):
        """When response has no refresh_token, save_tokens should not be called."""
        mock_get_refresh.return_value = "old_refresh"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_access_token",
            "expires_in": 3600,
        })

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result == "new_access_token"
        mock_save.assert_not_called()

    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_no_stored_refresh_token(self, mock_get_refresh):
        """Should return None when no refresh token is stored."""
        mock_get_refresh.return_value = None

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_http_error(self, mock_get_refresh, mock_urlopen):
        """Should return None on HTTP error from Microsoft."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_http_error_invalid_grant(self, mock_get_refresh, mock_urlopen):
        """Should handle invalid_grant error (revoked token) gracefully."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_http_error_no_body(self, mock_get_refresh, mock_urlopen):
        """Should handle HTTP error when fp is None (no response body)."""
        mock_get_refresh.return_value = "refresh_token"
        error = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=None,
        )
        mock_urlopen.side_effect = error

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_network_error(self, mock_get_refresh, mock_urlopen):
        """Should return None on network/URL error."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_no_access_token_in_response(self, mock_get_refresh, mock_urlopen):
        """Should return None when response lacks access_token."""
        mock_get_refresh.return_value = "refresh_token"
        mock_urlopen.return_value = _make_urlopen_response({"error": "something"})

        result = refresh_access_token("conn_test", "client_id", "client_secret")

        assert result is None

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_sends_correct_payload(self, mock_get_refresh, mock_urlopen, mock_save):
        """Should POST the correct grant_type and parameters to Microsoft."""
        mock_get_refresh.return_value = "my_refresh_tok"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_at",
            "refresh_token": "new_rt",
            "expires_in": 3600,
        })

        refresh_access_token("conn_test", "my_client", "my_secret")

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert request_obj.full_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        assert request_obj.method == "POST"

        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        assert posted_data["grant_type"] == ["refresh_token"]
        assert posted_data["client_id"] == ["my_client"]
        assert posted_data["client_secret"] == ["my_secret"]
        assert posted_data["refresh_token"] == ["my_refresh_tok"]

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_default_expires_in(self, mock_get_refresh, mock_urlopen, mock_save):
        """Should default to 3600 when expires_in is not in the response."""
        mock_get_refresh.return_value = "old_refresh"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "new_at",
            "refresh_token": "new_rt",
        })

        result = refresh_access_token("conn_test", "cid", "csecret")

        assert result == "new_at"
        mock_save.assert_called_once()
        _kw = mock_save.call_args.kwargs
        assert _kw["connection_id"] == "conn_test"
        assert _kw["access_token"] == "new_at"
        assert _kw["refresh_token"] == "new_rt"
        assert _kw["expires_in"] == 3600

    @patch.object(shared_oauth, "save_tokens")
    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    @patch.object(shared_oauth, "get_refresh_token")
    def test_refresh_includes_correct_scopes(self, mock_get_refresh, mock_urlopen, mock_save):
        """Refresh request should include the expected Microsoft scopes."""
        mock_get_refresh.return_value = "my_refresh"
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        })

        refresh_access_token("conn_test", "cid", "csecret")

        request_obj = mock_urlopen.call_args[0][0]
        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        scope_value = posted_data["scope"][0]
        assert "Mail.Read" in scope_value
        assert "offline_access" in scope_value


# ============================================================================
# Delete Tokens
# ============================================================================

class TestDeleteTokens:
    """Test deleting tokens from DynamoDB."""

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_tokens_success(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return True and call delete_item with the correct key."""
        mock_db_fn.return_value = mock_dynamodb

        result = delete_tokens("conn_test")

        assert result is True
        mock_table.delete_item.assert_called_once_with(
            Key={
                "PK": "OUTLOOK_TOKEN#conn_test",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_tokens_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Should return False when DynamoDB raises ClientError."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.delete_item.side_effect = ClientError(
            {"Error": {"Code": "500", "Message": "Error"}}, "DeleteItem"
        )

        result = delete_tokens("conn_test")

        assert result is False

    @patch.object(shared_oauth, "_get_dynamodb")
    def test_delete_tokens_uses_correct_key(self, mock_db_fn, mock_dynamodb, mock_table):
        """delete_item should use OUTLOOK_TOKEN# prefix in PK."""
        mock_db_fn.return_value = mock_dynamodb

        delete_tokens("my-conn-456")

        mock_table.delete_item.assert_called_once_with(
            Key={
                "PK": "OUTLOOK_TOKEN#my-conn-456",
                "SK": "CREDENTIALS",
            }
        )


# ============================================================================
# Exchange Code for Tokens
# ============================================================================

class TestExchangeCodeForTokens:
    """Test exchanging an authorization code for tokens."""

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_success(self, mock_urlopen):
        """Successful exchange should return (access, refresh, expires_in) tuple."""
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "access_tok",
            "refresh_token": "refresh_tok",
            "expires_in": 3600,
        })

        access, refresh, expires = exchange_code_for_tokens(
            "auth_code", "client_id", "client_secret", "https://example.com/callback"
        )

        assert access == "access_tok"
        assert refresh == "refresh_tok"
        assert expires == 3600

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_http_error(self, mock_urlopen):
        """Should return (None, None, None) on HTTP error."""
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
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

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_http_error_no_body(self, mock_urlopen):
        """Should return (None, None, None) on HTTP error with no response body."""
        error = urllib.error.HTTPError(
            url="https://login.microsoftonline.com/common/oauth2/v2.0/token",
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

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_network_error(self, mock_urlopen):
        """Should return (None, None, None) on network error."""
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )

        assert access is None
        assert refresh is None
        assert expires is None

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_default_expires(self, mock_urlopen):
        """Should default to 3600 when expires_in is absent from response."""
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "tok",
            "refresh_token": "ref",
        })

        _, _, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )

        assert expires == 3600

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_sends_correct_payload(self, mock_urlopen):
        """Exchange should POST the correct grant_type and parameters to Microsoft."""
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        })

        exchange_code_for_tokens(
            "my_auth_code", "my_client", "my_secret", "https://app.example.com/oauth/callback"
        )

        call_args = mock_urlopen.call_args
        request_obj = call_args[0][0]
        assert request_obj.full_url == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
        assert request_obj.method == "POST"

        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        assert posted_data["grant_type"] == ["authorization_code"]
        assert posted_data["client_id"] == ["my_client"]
        assert posted_data["client_secret"] == ["my_secret"]
        assert posted_data["code"] == ["my_auth_code"]
        assert posted_data["redirect_uri"] == ["https://app.example.com/oauth/callback"]

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_includes_correct_scopes(self, mock_urlopen):
        """Exchange request should include the expected Microsoft scopes."""
        mock_urlopen.return_value = _make_urlopen_response({
            "access_token": "at",
            "refresh_token": "rt",
            "expires_in": 3600,
        })

        exchange_code_for_tokens(
            "code", "cid", "csecret", "https://example.com/callback"
        )

        request_obj = mock_urlopen.call_args[0][0]
        posted_data = urllib.parse.parse_qs(request_obj.data.decode("utf-8"))
        scope_value = posted_data["scope"][0]
        assert "Mail.Read" in scope_value
        assert "offline_access" in scope_value

    @patch("shared.entra_id_oauth.urllib.request.urlopen")
    def test_exchange_missing_fields_in_response(self, mock_urlopen):
        """When response body is empty JSON, access and refresh should be None."""
        mock_urlopen.return_value = _make_urlopen_response({})

        access, refresh, expires = exchange_code_for_tokens(
            "code", "client_id", "client_secret", "https://example.com/callback"
        )

        assert access is None
        assert refresh is None
        # expires_in defaults to 3600
        assert expires == 3600


# ============================================================================
# Client caching (_get_dynamodb / _get_kms)
# ============================================================================

class TestClientCaching:
    """Test that boto3 clients are cached after first creation."""

    @patch.object(shared_oauth, "boto3")
    def test_get_dynamodb_caches_client(self, mock_boto3):
        """Calling _get_dynamodb twice should only call boto3.resource once."""
        mock_resource = MagicMock()
        mock_boto3.resource.return_value = mock_resource

        first = shared_oauth._get_dynamodb()
        second = shared_oauth._get_dynamodb()

        assert first is second
        mock_boto3.resource.assert_called_once_with("dynamodb")

    @patch.object(shared_oauth, "boto3")
    def test_get_kms_caches_client(self, mock_boto3):
        """Calling _get_kms twice should only call boto3.client once."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        first = shared_oauth._get_kms()
        second = shared_oauth._get_kms()

        assert first is second
        mock_boto3.client.assert_called_once_with("kms")
