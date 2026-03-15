"""Unit tests for security-scanner Lambda handler."""

import base64
import importlib.util
import json
import os
import sys
import time
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode

import bcrypt
import pytest


# ============================================================================
# Module loading
# ============================================================================

mock_ssm_client = MagicMock()
mock_dynamodb = MagicMock()

SECRET_PATH = "abc123def456"
SCANNER_EMAIL = "security-scanner@outcomeops.local"
SCANNER_PASSWORD = "test-password-12345"
JWT_SECRET = "test-jwt-secret-64chars-long-enough-for-testing-purposes-ok"
REDIRECT_URL = "https://example.com/workspaces"
WORKSPACES_TABLE = "test-workspaces"

# Pre-compute bcrypt hash for tests
PASSWORD_HASH = bcrypt.hashpw(SCANNER_PASSWORD.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def _load_handler_module():
    """Load or reload the security-scanner handler module."""
    if 'security_scanner_handler' in sys.modules:
        del sys.modules['security_scanner_handler']

    # Temporarily restore real jwt if mocked by other tests
    saved_jwt = sys.modules.get('jwt')
    jwt_was_mocked = saved_jwt is not None and not hasattr(saved_jwt, '__file__')
    if jwt_was_mocked:
        del sys.modules['jwt']
        for key in list(sys.modules.keys()):
            if key.startswith('jwt.'):
                del sys.modules[key]

    # Set env vars before loading
    os.environ["SCANNER_PATH"] = SECRET_PATH
    os.environ["SCANNER_EMAIL"] = SCANNER_EMAIL
    os.environ["REDIRECT_URL"] = REDIRECT_URL
    os.environ["SSM_PASSWORD"] = "/test/security-scanner/password"
    os.environ["SSM_JWT_SECRET"] = "/test/security-scanner/jwt-secret"
    os.environ["WORKSPACES_TABLE"] = WORKSPACES_TABLE

    handler_dir = os.path.join(os.path.dirname(__file__), '../../security-scanner')
    if handler_dir not in sys.path:
        sys.path.insert(0, handler_dir)

    handler_path = os.path.join(handler_dir, 'handler.py')
    spec = importlib.util.spec_from_file_location("security_scanner_handler", handler_path)
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules['security_scanner_handler'] = handler_module
    spec.loader.exec_module(handler_module)

    # Replace boto3 clients with mocks
    handler_module.ssm = mock_ssm_client
    handler_module.dynamodb = mock_dynamodb

    # Restore the mocked jwt for other tests that depend on it
    if jwt_was_mocked:
        sys.modules['jwt'] = saved_jwt

    return handler_module


handler_module = _load_handler_module()
lambda_handler = handler_module.lambda_handler


# ============================================================================
# Fixtures
# ============================================================================

SCANNER_USER_ITEM = {
    "PK": f"USER#{SCANNER_EMAIL}",
    "SK": "PROFILE",
    "user_email": SCANNER_EMAIL,
    "name": "Security Scanner",
    "auth_type": "password",
    "password_hash": PASSWORD_HASH,
    "is_service_account": True,
}


@pytest.fixture(autouse=True)
def reset_mocks():
    """Reset mocks and caches between tests."""
    mock_ssm_client.reset_mock()
    mock_dynamodb.reset_mock()
    # Reset cached values
    handler_module._jwt_secret = None

    def ssm_side_effect(Name, WithDecryption=True):
        values = {
            "/test/security-scanner/password": SCANNER_PASSWORD,
            "/test/security-scanner/jwt-secret": JWT_SECRET,
        }
        if Name in values:
            return {"Parameter": {"Value": values[Name]}}
        raise Exception(f"Parameter not found: {Name}")

    mock_ssm_client.get_parameter.side_effect = ssm_side_effect

    # Mock DynamoDB table
    mock_table = MagicMock()
    mock_dynamodb.Table.return_value = mock_table
    mock_table.get_item.return_value = {"Item": SCANNER_USER_ITEM.copy()}

    yield


def _alb_get_event(path):
    return {
        "httpMethod": "GET",
        "path": path,
        "headers": {},
        "body": None,
        "isBase64Encoded": False,
    }


def _alb_post_event(path, form_data):
    return {
        "httpMethod": "POST",
        "path": path,
        "headers": {"content-type": "application/x-www-form-urlencoded"},
        "body": urlencode(form_data),
        "isBase64Encoded": False,
    }


# ============================================================================
# Tests
# ============================================================================

class TestGetLoginForm:

    def test_valid_path_returns_login_form(self):
        """GET with correct secret path returns HTML login form."""
        event = _alb_get_event(f"/auth/{SECRET_PATH}")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["headers"]["Content-Type"] == "text/html"
        assert "<form" in result["body"]
        assert 'name="email"' in result["body"]
        assert 'name="password"' in result["body"]
        assert "Sign In" in result["body"]

    def test_invalid_path_returns_404(self):
        """GET with wrong path returns 404."""
        event = _alb_get_event("/auth/wrong-path")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 404

    def test_no_path_returns_404(self):
        """GET to /auth/ without secret returns 404."""
        event = _alb_get_event("/auth/")
        result = lambda_handler(event, None)

        assert result["statusCode"] == 404


class TestPostLogin:

    def test_valid_credentials_returns_redirect_with_cookie(self):
        """POST with correct email/password returns 302 + Set-Cookie."""
        event = _alb_post_event(
            f"/auth/{SECRET_PATH}",
            {"email": SCANNER_EMAIL, "password": SCANNER_PASSWORD},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 302
        assert result["headers"]["Location"] == REDIRECT_URL
        cookie = result["headers"]["Set-Cookie"]
        assert "oo_scanner_session=" in cookie
        assert "HttpOnly" in cookie
        assert "Secure" in cookie
        assert "SameSite=Lax" in cookie

        # Decode JWT payload (base64) to verify claims
        token = cookie.split("oo_scanner_session=")[1].split(";")[0]
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        decoded = json.loads(base64.b64decode(payload_b64))
        assert decoded["sub"] == SCANNER_EMAIL
        assert decoded["email"] == SCANNER_EMAIL
        assert decoded["auth_type"] == "scanner"
        assert decoded["name"] == "Security Scanner"
        assert "exp" in decoded

    def test_first_login_hashes_password(self):
        """First login with empty hash reads SSM, hashes, and stores in DynamoDB."""
        mock_table = mock_dynamodb.Table.return_value
        user_no_hash = SCANNER_USER_ITEM.copy()
        user_no_hash["password_hash"] = ""
        mock_table.get_item.return_value = {"Item": user_no_hash}

        event = _alb_post_event(
            f"/auth/{SECRET_PATH}",
            {"email": SCANNER_EMAIL, "password": SCANNER_PASSWORD},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 302
        # Verify DynamoDB was updated with the hash
        mock_table.update_item.assert_called_once()
        call_kwargs = mock_table.update_item.call_args[1]
        assert call_kwargs["UpdateExpression"] == "SET password_hash = :h"
        stored_hash = call_kwargs["ExpressionAttributeValues"][":h"]
        assert bcrypt.checkpw(SCANNER_PASSWORD.encode("utf-8"), stored_hash.encode("utf-8"))

    def test_wrong_password_returns_form_with_error(self):
        """POST with wrong password returns login form with error."""
        event = _alb_post_event(
            f"/auth/{SECRET_PATH}",
            {"email": SCANNER_EMAIL, "password": "wrong-password"},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "Invalid credentials" in result["body"]
        assert "<form" in result["body"]

    def test_wrong_email_returns_form_with_error(self):
        """POST with wrong email returns login form with error."""
        mock_table = mock_dynamodb.Table.return_value
        mock_table.get_item.return_value = {}  # User not found

        event = _alb_post_event(
            f"/auth/{SECRET_PATH}",
            {"email": "wrong@example.com", "password": SCANNER_PASSWORD},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "Invalid credentials" in result["body"]

    def test_non_password_user_returns_error(self):
        """POST for a user without auth_type=password returns error."""
        mock_table = mock_dynamodb.Table.return_value
        oidc_user = SCANNER_USER_ITEM.copy()
        oidc_user["auth_type"] = "oidc"
        mock_table.get_item.return_value = {"Item": oidc_user}

        event = _alb_post_event(
            f"/auth/{SECRET_PATH}",
            {"email": SCANNER_EMAIL, "password": SCANNER_PASSWORD},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "Invalid credentials" in result["body"]

    def test_missing_fields_returns_form_with_error(self):
        """POST with missing fields returns login form with error."""
        event = _alb_post_event(f"/auth/{SECRET_PATH}", {"email": ""})
        result = lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert "required" in result["body"]

    def test_invalid_path_returns_404(self):
        """POST to wrong path returns 404."""
        event = _alb_post_event(
            "/auth/wrong-path",
            {"email": SCANNER_EMAIL, "password": SCANNER_PASSWORD},
        )
        result = lambda_handler(event, None)

        assert result["statusCode"] == 404


class TestUnsupportedMethod:

    def test_put_returns_405(self):
        """PUT returns 405."""
        event = {"httpMethod": "PUT", "path": f"/auth/{SECRET_PATH}"}
        result = lambda_handler(event, None)

        assert result["statusCode"] == 405
