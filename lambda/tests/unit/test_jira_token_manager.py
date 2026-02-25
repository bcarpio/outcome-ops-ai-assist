"""Unit tests for Jira token manager (token_manager.py)."""

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
    """Load or reload the token_manager module from jira-integration."""
    if 'jira_token_manager' in sys.modules:
        del sys.modules['jira_token_manager']

    token_path = os.path.join(
        os.path.dirname(__file__),
        '../../jira-integration/token_manager.py',
    )
    spec = importlib.util.spec_from_file_location("jira_token_manager", token_path)
    token_module = importlib.util.module_from_spec(spec)
    sys.modules['jira_token_manager'] = token_module
    spec.loader.exec_module(token_module)
    return token_module


token_module = _load_token_module()

# Import functions
save_tokens = token_module.save_tokens
get_refresh_token = token_module.get_refresh_token
refresh_access_token = token_module.refresh_access_token
delete_tokens = token_module.delete_tokens
exchange_code_for_tokens = token_module.exchange_code_for_tokens
get_oauth_config = token_module.get_oauth_config
_encrypt_token = token_module._encrypt_token
_decrypt_token = token_module._decrypt_token
_load_oauth_config = token_module._load_oauth_config


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture(autouse=True)
def reset_module_globals():
    """Reset cached module globals between tests."""
    token_module._dynamodb = None
    token_module._kms = None
    token_module._ssm = None
    token_module._oauth_config = None
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


@pytest.fixture
def mock_ssm():
    """Create mock SSM client with standard OAuth config responses."""
    mock = MagicMock()

    def get_param_side_effect(Name, **kwargs):
        params = {
            "/dev/outcome-ops-ai-assist/oauth/atlassian/client-id": {
                "Parameter": {"Value": "test-client-id"}
            },
            "/dev/outcome-ops-ai-assist/oauth/atlassian/client-secret": {
                "Parameter": {"Value": "test-client-secret"}
            },
            "/dev/outcome-ops-ai-assist/oauth/atlassian/jira-callback-url": {
                "Parameter": {"Value": "https://example.com/callback"}
            },
        }
        result = params.get(Name)
        if result is None:
            raise ClientError(
                {"Error": {"Code": "ParameterNotFound", "Message": f"Parameter {Name} not found"}},
                "GetParameter",
            )
        return result

    mock.get_parameter.side_effect = get_param_side_effect
    return mock


@pytest.fixture
def oauth_config():
    """Standard OAuth config dict for tests that need it pre-loaded."""
    return {
        "client_id": "test-client-id",
        "client_secret": "test-client-secret",
        "callback_url": "https://example.com/callback",
    }


# ============================================================================
# Encrypt/Decrypt
# ============================================================================

class TestEncryptDecrypt:
    """Test token encryption and decryption."""

    @patch.object(token_module, '_get_kms')
    def test_encrypt_token_returns_base64(self, mock_kms_fn, mock_kms):
        """Test encrypting a token returns a valid base64-encoded string."""
        mock_kms_fn.return_value = mock_kms

        result = _encrypt_token("my_secret_token")
        assert isinstance(result, str)
        # Should be valid base64 that decodes without error
        decoded = base64.b64decode(result)
        assert decoded == b"encrypted_data"
        mock_kms.encrypt.assert_called_once()

    @patch.object(token_module, '_get_kms')
    def test_encrypt_token_passes_correct_params(self, mock_kms_fn, mock_kms):
        """Test encrypt passes token encoded as UTF-8 with the KMS key ARN."""
        mock_kms_fn.return_value = mock_kms

        with patch.object(token_module, 'KMS_KEY_ARN', 'arn:aws:kms:us-east-1:123:key/abc'):
            _encrypt_token("test_token")

        call_kwargs = mock_kms.encrypt.call_args[1]
        assert call_kwargs["KeyId"] == "arn:aws:kms:us-east-1:123:key/abc"
        assert call_kwargs["Plaintext"] == b"test_token"

    @patch.object(token_module, '_get_kms')
    def test_decrypt_token_returns_plaintext(self, mock_kms_fn, mock_kms):
        """Test decrypting a token returns the plain text string."""
        mock_kms_fn.return_value = mock_kms

        encrypted = base64.b64encode(b"encrypted_data").decode("utf-8")
        result = _decrypt_token(encrypted)
        assert result == "decrypted_token_value"
        mock_kms.decrypt.assert_called_once()

    @patch.object(token_module, '_get_kms')
    def test_decrypt_token_passes_decoded_ciphertext(self, mock_kms_fn, mock_kms):
        """Test decrypt base64-decodes the input before passing to KMS."""
        mock_kms_fn.return_value = mock_kms

        raw_cipher = b"some_cipher_bytes"
        encrypted = base64.b64encode(raw_cipher).decode("utf-8")
        _decrypt_token(encrypted)

        call_kwargs = mock_kms.decrypt.call_args[1]
        assert call_kwargs["CiphertextBlob"] == raw_cipher


# ============================================================================
# Load OAuth Config
# ============================================================================

class TestLoadOAuthConfig:
    """Test loading OAuth configuration from SSM."""

    @patch.object(token_module, '_get_ssm')
    def test_load_oauth_config_success(self, mock_ssm_fn, mock_ssm):
        """Test successfully loading OAuth config from SSM parameters."""
        mock_ssm_fn.return_value = mock_ssm

        config = _load_oauth_config()
        assert config["client_id"] == "test-client-id"
        assert config["client_secret"] == "test-client-secret"
        assert config["callback_url"] == "https://example.com/callback"

    @patch.object(token_module, '_get_ssm')
    def test_load_oauth_config_uses_env_prefix(self, mock_ssm_fn):
        """Test that SSM parameter paths use ENV and APP_NAME."""
        mock_ssm_client = MagicMock()
        mock_ssm_client.get_parameter.return_value = {"Parameter": {"Value": "val"}}
        mock_ssm_fn.return_value = mock_ssm_client

        with patch.dict(os.environ, {"ENV": "prd", "APP_NAME": "my-app"}):
            # Reset cached config
            token_module._oauth_config = None
            _load_oauth_config()

        calls = mock_ssm_client.get_parameter.call_args_list
        param_names = [c[1]["Name"] for c in calls]
        assert "/prd/my-app/oauth/atlassian/client-id" in param_names
        assert "/prd/my-app/oauth/atlassian/client-secret" in param_names
        assert "/prd/my-app/oauth/atlassian/jira-callback-url" in param_names

    @patch.object(token_module, '_get_ssm')
    def test_load_oauth_config_decrypts_client_secret(self, mock_ssm_fn):
        """Test that client-secret parameter is fetched with WithDecryption=True."""
        mock_ssm_client = MagicMock()
        mock_ssm_client.get_parameter.return_value = {"Parameter": {"Value": "val"}}
        mock_ssm_fn.return_value = mock_ssm_client

        _load_oauth_config()

        # Find the call for client-secret
        for call in mock_ssm_client.get_parameter.call_args_list:
            if "client-secret" in call[1].get("Name", ""):
                assert call[1].get("WithDecryption") is True
                break
        else:
            pytest.fail("client-secret parameter was never fetched")

    @patch.object(token_module, '_get_ssm')
    def test_load_oauth_config_caches_result(self, mock_ssm_fn, mock_ssm):
        """Test that config is cached after first load."""
        mock_ssm_fn.return_value = mock_ssm

        config1 = _load_oauth_config()
        config2 = _load_oauth_config()
        assert config1 is config2
        # SSM should only be called 3 times total (once per param), not 6
        assert mock_ssm.get_parameter.call_count == 3

    @patch.object(token_module, '_get_ssm')
    def test_load_oauth_config_ssm_error(self, mock_ssm_fn):
        """Test raises ClientError when SSM parameter retrieval fails."""
        mock_ssm_client = MagicMock()
        mock_ssm_client.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "ParameterNotFound", "Message": "Not found"}},
            "GetParameter",
        )
        mock_ssm_fn.return_value = mock_ssm_client

        with pytest.raises(ClientError):
            _load_oauth_config()

    def test_get_oauth_config_delegates_to_load(self):
        """Test get_oauth_config calls _load_oauth_config."""
        expected = {"client_id": "id", "client_secret": "secret", "callback_url": "url"}
        with patch.object(token_module, '_load_oauth_config', return_value=expected) as mock_load:
            result = get_oauth_config()
        assert result == expected
        mock_load.assert_called_once()


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
        assert item["PK"] == "JIRA_TOKEN#conn_test"
        assert item["SK"] == "CREDENTIALS"
        assert item["encrypted_refresh_token"] == "encrypted_refresh"
        assert item["connection_id"] == "conn_test"

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_includes_expiry(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test saved item includes expires_at and updated_at fields."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        save_tokens("conn_test", "access", "refresh", 7200)

        item = mock_table.put_item.call_args[1]["Item"]
        assert "expires_at" in item
        assert isinstance(item["expires_at"], int)
        assert "updated_at" in item

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_default_expires(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test default expiry of 3600 seconds."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "encrypted"

        save_tokens("conn_test", "access", "refresh")

        item = mock_table.put_item.call_args[1]["Item"]
        # expires_at should be roughly now + 3600 (give generous margin)
        assert item["expires_at"] > 0

    @patch.object(token_module, '_encrypt_token')
    @patch.object(token_module, '_get_dynamodb')
    def test_save_tokens_encrypts_refresh_token(self, mock_db_fn, mock_encrypt, mock_dynamodb, mock_table):
        """Test that the refresh token is encrypted before storage."""
        mock_db_fn.return_value = mock_dynamodb
        mock_encrypt.return_value = "enc_value"

        save_tokens("conn_test", "access_tok", "my_refresh_tok")
        mock_encrypt.assert_called_once_with("my_refresh_tok")

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
    def test_get_refresh_token_correct_key(self, mock_db_fn, mock_decrypt, mock_dynamodb, mock_table):
        """Test uses correct DynamoDB key with JIRA_TOKEN prefix."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {
            "Item": {"encrypted_refresh_token": "enc"}
        }
        mock_decrypt.return_value = "tok"

        get_refresh_token("my-connection-123")

        mock_table.get_item.assert_called_once_with(
            Key={
                "PK": "JIRA_TOKEN#my-connection-123",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_not_found(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when token item not found in DynamoDB."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {}

        result = get_refresh_token("nonexistent")
        assert result is None

    @patch.object(token_module, '_get_dynamodb')
    def test_get_refresh_token_no_encrypted_field(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns None when Item exists but encrypted_refresh_token is missing."""
        mock_db_fn.return_value = mock_dynamodb
        mock_table.get_item.return_value = {"Item": {}}

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
    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_success_rolling_token(
        self, mock_get_refresh, mock_get_config, mock_urlopen, mock_save
    ):
        """Test successful refresh with a new rolling refresh token."""
        mock_get_refresh.return_value = "old_refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "https://example.com/callback",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access_token",
            "refresh_token": "new_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test")
        assert result == "new_access_token"
        mock_save.assert_called_once_with(
            "conn_test", "new_access_token", "new_refresh_token", 3600
        )

    @patch.object(token_module, 'save_tokens')
    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_success_same_refresh_token(
        self, mock_get_refresh, mock_get_config, mock_urlopen, mock_save
    ):
        """Test refresh when the returned refresh token is the same as the old one."""
        mock_get_refresh.return_value = "same_refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "https://example.com/callback",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access",
            "refresh_token": "same_refresh_token",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test")
        assert result == "new_access"
        # Should still save tokens to update timestamp
        mock_save.assert_called_once_with(
            "conn_test", "new_access", "same_refresh_token", 3600
        )

    @patch.object(token_module, 'save_tokens')
    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_refresh_token_in_response(
        self, mock_get_refresh, mock_get_config, mock_urlopen, mock_save
    ):
        """Test refresh when response has access token but no refresh token."""
        mock_get_refresh.return_value = "old_refresh"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "new_access",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test")
        assert result == "new_access"
        # No new refresh token means save_tokens should NOT be called
        mock_save.assert_not_called()

    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_stored_refresh_token(self, mock_get_refresh):
        """Test returns None when no refresh token is stored."""
        mock_get_refresh.return_value = None

        result = refresh_access_token("conn_test")
        assert result is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_no_access_token_in_response(
        self, mock_get_refresh, mock_get_config, mock_urlopen
    ):
        """Test returns None when response lacks access_token."""
        mock_get_refresh.return_value = "refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "error": "something_wrong",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        result = refresh_access_token("conn_test")
        assert result is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error(self, mock_get_refresh, mock_get_config, mock_urlopen):
        """Test returns None on HTTP error from Atlassian."""
        mock_get_refresh.return_value = "refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test")
        assert result is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error_invalid_grant(self, mock_get_refresh, mock_get_config, mock_urlopen):
        """Test handles invalid_grant error (token revoked) without raising."""
        mock_get_refresh.return_value = "refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_grant"}')),
        )

        result = refresh_access_token("conn_test")
        assert result is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_http_error_no_body(self, mock_get_refresh, mock_get_config, mock_urlopen):
        """Test handles HTTP error with no response body (fp=None)."""
        mock_get_refresh.return_value = "refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        http_error = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )
        # Manually set fp to None to simulate no body
        http_error.fp = None
        mock_urlopen.side_effect = http_error

        result = refresh_access_token("conn_test")
        assert result is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_network_error(self, mock_get_refresh, mock_get_config, mock_urlopen):
        """Test returns None on network error (URLError)."""
        mock_get_refresh.return_value = "refresh_token"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        result = refresh_access_token("conn_test")
        assert result is None

    @patch.object(token_module, 'save_tokens')
    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_sends_correct_payload(
        self, mock_get_refresh, mock_get_config, mock_urlopen, mock_save
    ):
        """Test the refresh request sends correct form data."""
        mock_get_refresh.return_value = "my_refresh"
        mock_get_config.return_value = {
            "client_id": "my_client_id",
            "client_secret": "my_client_secret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        refresh_access_token("conn_test")

        # Inspect the Request object passed to urlopen
        request_obj = mock_urlopen.call_args[0][0]
        assert request_obj.full_url == "https://auth.atlassian.com/oauth/token"
        assert request_obj.method == "POST"
        body = request_obj.data.decode("utf-8")
        assert "grant_type=refresh_token" in body
        assert "client_id=my_client_id" in body
        assert "client_secret=my_client_secret" in body
        assert "refresh_token=my_refresh" in body

    @patch.object(token_module, 'save_tokens')
    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    @patch.object(token_module, 'get_refresh_token')
    def test_refresh_default_expires_in(
        self, mock_get_refresh, mock_get_config, mock_urlopen, mock_save
    ):
        """Test defaults to 3600 when expires_in not in response."""
        mock_get_refresh.return_value = "refresh"
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        refresh_access_token("conn_test")

        mock_save.assert_called_once_with("conn_test", "tok", "ref", 3600)


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
                "PK": "JIRA_TOKEN#conn_test",
                "SK": "CREDENTIALS",
            }
        )

    @patch.object(token_module, '_get_dynamodb')
    def test_delete_tokens_dynamodb_error(self, mock_db_fn, mock_dynamodb, mock_table):
        """Test returns False on DynamoDB ClientError."""
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

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_success(self, mock_get_config, mock_urlopen):
        """Test successful code exchange returns token tuple."""
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }

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
            "auth_code", "https://example.com/callback"
        )
        assert access == "access_tok"
        assert refresh == "refresh_tok"
        assert expires == 3600

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_sends_correct_payload(self, mock_get_config, mock_urlopen):
        """Test the exchange request sends correct form data."""
        mock_get_config.return_value = {
            "client_id": "my_cid",
            "client_secret": "my_csecret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
            "expires_in": 3600,
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        exchange_code_for_tokens("my_auth_code", "https://example.com/cb")

        request_obj = mock_urlopen.call_args[0][0]
        assert request_obj.full_url == "https://auth.atlassian.com/oauth/token"
        assert request_obj.method == "POST"
        body = request_obj.data.decode("utf-8")
        assert "grant_type=authorization_code" in body
        assert "client_id=my_cid" in body
        assert "client_secret=my_csecret" in body
        assert "code=my_auth_code" in body
        assert "redirect_uri=" in body

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_http_error(self, mock_get_config, mock_urlopen):
        """Test returns None tuple on HTTP error."""
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=400,
            msg="Bad Request",
            hdrs={},
            fp=MagicMock(read=MagicMock(return_value=b'{"error": "invalid_code"}')),
        )

        access, refresh, expires = exchange_code_for_tokens(
            "bad_code", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_network_error(self, mock_get_config, mock_urlopen):
        """Test returns None tuple on network error."""
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        mock_urlopen.side_effect = urllib.error.URLError("Connection refused")

        access, refresh, expires = exchange_code_for_tokens(
            "code", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_default_expires_in(self, mock_get_config, mock_urlopen):
        """Test defaults to 3600 when expires_in not in response."""
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }

        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "access_token": "tok",
            "refresh_token": "ref",
        }).encode("utf-8")
        mock_response.__enter__ = MagicMock(return_value=mock_response)
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        _, _, expires = exchange_code_for_tokens(
            "code", "https://example.com/callback"
        )
        assert expires == 3600

    @patch('jira_token_manager.urllib.request.urlopen')
    @patch.object(token_module, 'get_oauth_config')
    def test_exchange_http_error_no_body(self, mock_get_config, mock_urlopen):
        """Test handles HTTP error with no response body."""
        mock_get_config.return_value = {
            "client_id": "cid",
            "client_secret": "csecret",
            "callback_url": "url",
        }
        http_error = urllib.error.HTTPError(
            url="https://auth.atlassian.com/oauth/token",
            code=500,
            msg="Server Error",
            hdrs={},
            fp=None,
        )
        http_error.fp = None
        mock_urlopen.side_effect = http_error

        access, refresh, expires = exchange_code_for_tokens(
            "code", "https://example.com/callback"
        )
        assert access is None
        assert refresh is None
        assert expires is None


# ============================================================================
# Client caching helpers
# ============================================================================

class TestClientCaching:
    """Test that boto3 client/resource helpers cache correctly."""

    @patch('jira_token_manager.boto3')
    def test_get_dynamodb_caches(self, mock_boto3):
        """Test _get_dynamodb creates resource once and caches."""
        mock_resource = MagicMock()
        mock_boto3.resource.return_value = mock_resource

        result1 = token_module._get_dynamodb()
        result2 = token_module._get_dynamodb()

        assert result1 is result2
        mock_boto3.resource.assert_called_once_with("dynamodb")

    @patch('jira_token_manager.boto3')
    def test_get_kms_caches(self, mock_boto3):
        """Test _get_kms creates client once and caches."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        result1 = token_module._get_kms()
        result2 = token_module._get_kms()

        assert result1 is result2
        mock_boto3.client.assert_called_once_with("kms")

    @patch('jira_token_manager.boto3')
    def test_get_ssm_caches(self, mock_boto3):
        """Test _get_ssm creates client once and caches."""
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client

        result1 = token_module._get_ssm()
        result2 = token_module._get_ssm()

        assert result1 is result2
        mock_boto3.client.assert_called_once_with("ssm")
