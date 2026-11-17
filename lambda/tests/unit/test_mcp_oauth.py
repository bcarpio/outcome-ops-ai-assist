# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Tests for MCP Azure AD OAuth (JWT) auth + validate_bearer dispatch (chunk 11b).

Uses a real RS256 keypair and mocks the JWKS client so signature/iss/aud/exp
validation runs for real against a known public key.
"""

import importlib.util
import os
import sys
import time
from unittest.mock import patch

import pytest  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402

# Obtain a REAL PyJWT reference WITHOUT disturbing sys.modules['jwt'] at
# collection time -- a sibling test (test_github_app) installs a MagicMock there
# at import and relies on it, so clobbering it (either direction) breaks a test.
# Pop, do a fresh real import, then restore the prior sys.modules state. The
# autouse fixture below swaps in the real module only during OUR tests (the
# handler's lazy `import jwt` reads sys.modules at call time).
_saved_jwt = sys.modules.pop("jwt", None)
import jwt as _real_jwt  # noqa: E402
if _saved_jwt is not None:
    sys.modules["jwt"] = _saved_jwt
else:
    sys.modules.pop("jwt", None)
jwt = _real_jwt


def _load_mcp_module():
    # Unique module name -> never clobber the "mcp_server_handler" entry that
    # test_mcp_outcomeops.py captures (reloading a shared name mid-suite breaks
    # the other file). We patch.object(M, ...) on this private instance.
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../mcp-server/handler.py")
    spec = importlib.util.spec_from_file_location("mcp_handler_oauth", handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["mcp_handler_oauth"] = mod
    spec.loader.exec_module(mod)
    return mod


M = _load_mcp_module()

TENANT = "test-tenant-guid"
AUD = "api://outcomeops-mcp"
ISS = f"https://login.microsoftonline.com/{TENANT}/v2.0"
EMAIL = "alice@outcomeops.onmicrosoft.com"

_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv_pem = _priv.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())
_pub_pem = _priv.public_key().public_bytes(
    serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)

# a DIFFERENT key -> tokens signed with it fail signature verification
_priv2 = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_priv2_pem = _priv2.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())


def _make_token(overrides=None, sign_key=_priv_pem, drop=()):
    now = int(time.time())
    claims = {"iss": ISS, "aud": AUD, "exp": now + 3600, "nbf": now - 10,
              "iat": now, "preferred_username": EMAIL}
    claims.update(overrides or {})
    for k in drop:
        claims.pop(k, None)
    return jwt.encode(claims, sign_key, algorithm="RS256", headers={"kid": "test"})


class _FakeSigningKey:
    key = _pub_pem


class _FakeJwksClient:
    def get_signing_key_from_jwt(self, token):
        return _FakeSigningKey()


@pytest.fixture(autouse=True)
def _ensure_real_jwt():
    """Guarantee sys.modules['jwt'] is the real PyJWT during each test, undoing
    any MagicMock a sibling test injected. Restored afterwards."""
    saved = sys.modules.get("jwt")
    sys.modules["jwt"] = jwt
    yield
    if saved is not None:
        sys.modules["jwt"] = saved
    else:
        sys.modules.pop("jwt", None)


@pytest.fixture
def azure_env():
    with patch.object(M, "AZURE_AD_TENANT_ID", TENANT), \
         patch.object(M, "MCP_JWT_AUDIENCE", AUD), \
         patch.object(M, "_get_jwks_client", return_value=_FakeJwksClient()), \
         patch.object(M, "_emit_api_key_used"):
        yield


class TestValidateAzureAdJwt:
    def test_valid_token_returns_email(self, azure_env):
        assert M._validate_azure_ad_jwt(_make_token()) == EMAIL

    def test_wrong_audience_rejected(self, azure_env):
        assert M._validate_azure_ad_jwt(_make_token({"aud": "api://wrong"})) is None

    def test_wrong_issuer_rejected(self, azure_env):
        assert M._validate_azure_ad_jwt(
            _make_token({"iss": "https://login.microsoftonline.com/evil/v2.0"})) is None

    def test_expired_rejected(self, azure_env):
        now = int(time.time())
        assert M._validate_azure_ad_jwt(
            _make_token({"exp": now - 10, "nbf": now - 100})) is None

    def test_tampered_signature_rejected(self, azure_env):
        # signed with a different key than the JWKS returns
        assert M._validate_azure_ad_jwt(_make_token(sign_key=_priv2_pem)) is None

    def test_no_email_claim_rejected(self, azure_env):
        assert M._validate_azure_ad_jwt(
            _make_token(drop=("preferred_username",))) is None

    def test_falls_back_to_upn(self, azure_env):
        tok = _make_token(overrides={"upn": "bob@outcomeops.onmicrosoft.com"},
                          drop=("preferred_username",))
        assert M._validate_azure_ad_jwt(tok) == "bob@outcomeops.onmicrosoft.com"

    def test_disabled_when_config_missing(self):
        with patch.object(M, "AZURE_AD_TENANT_ID", ""), \
             patch.object(M, "MCP_JWT_AUDIENCE", ""):
            assert M._validate_azure_ad_jwt(_make_token()) is None


class TestValidateBearerDispatch:
    def test_jwt_shape_routes_to_azure(self):
        with patch.object(M, "_validate_azure_ad_jwt", return_value=EMAIL) as jwtv, \
             patch.object(M, "validate_api_key") as static:
            assert M.validate_bearer("eyJhbGciOiJSUzI1NiJ9.x.y") == EMAIL
            jwtv.assert_called_once()
            static.assert_not_called()

    def test_static_key_routes_to_static_when_enabled(self):
        with patch.object(M, "MCP_STATIC_KEYS_ENABLED", True), \
             patch.object(M, "validate_api_key", return_value=EMAIL) as static, \
             patch.object(M, "_validate_azure_ad_jwt") as jwtv:
            assert M.validate_bearer("ooa_abc123") == EMAIL
            static.assert_called_once()
            jwtv.assert_not_called()

    def test_static_key_rejected_when_disabled(self):
        with patch.object(M, "MCP_STATIC_KEYS_ENABLED", False), \
             patch.object(M, "validate_api_key") as static, \
             patch.object(M, "_emit_api_key_used"):
            assert M.validate_bearer("ooa_abc123") is None
            static.assert_not_called()

    def test_empty_bearer_returns_none(self):
        assert M.validate_bearer("") is None
