# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the Azure DevOps Boards token_manager wrapper.

The wrapper itself owns very little -- ADO constants, per-customer
tenant_id plumbing, and the vssps user-profile helper. The bulk of
logic (HTTP, KMS encrypt/decrypt, DDB writes, refresh-token
rotation) lives in ``shared.entra_id_oauth`` and is exercised by its
consolidated test suite.

These tests therefore focus on:
1. Each delegation passes the right AZDO_BOARDS_SCOPES + AZDO_BOARDS_TOKEN# prefix
2. tenant_id flows through (not hardcoded to "common")
3. Explicit ``vso.work`` scope (NOT ``.default``) -- Boards is
   read-only in v1 and the explicit scope IS the contract
4. Distinct from the Repos wrapper (independent tokens, distinct DDB
   partition, independent consent)
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest


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


# consolidated to lambda/shared/oauth/azure_devops_boards.py.
# Import via the ordinary package path so all consumers share the same
# module object.
_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
if _LAMBDA_ROOT not in sys.path:
    sys.path.insert(0, _LAMBDA_ROOT)
import shared.oauth.azure_devops_boards as token_module  # noqa: E402
# Legacy alias so pre-consolidation @patch targets still resolve.
sys.modules["azdo_boards_int_token_manager"] = token_module

_VENDORED_COPY_DIRS = [
    "../../shared/oauth",
]


@pytest.fixture(
    scope="module",
    autouse=True,
    params=_VENDORED_COPY_DIRS,
    ids=[d.rsplit("/", 1)[-1] for d in _VENDORED_COPY_DIRS],
)
def _vendored_copy(request):
    # only one copy now (the shared module). No-op fixture.
    yield

# Shared Entra ID OAuth helper. Tests assert wrapper delegations pass
# the right Boards-specific params (scopes, tenant_id, pk_prefix) into
# the shared module.
import shared.entra_id_oauth as shared_oauth  # noqa: E402, F401


# ============================================================================
# Constants
# ============================================================================


class TestAzdoBoardsConstants:
    """The Boards-specific constants are load-bearing for the OAuth
    flow + admin-consent shape. Pin them so a future contributor
    can't quietly drift them."""

    def test_resource_id_matches_ado_well_known(self):
        # Same well-known resource ID as Repos -- both integrations
        # authenticate against the ADO API surface. Microsoft owns
        # this value.
        assert token_module.AZDO_RESOURCE_ID == "499b84ac-1321-427f-aa17-267ca6975798"

    def test_scopes_use_explicit_vso_work_not_dot_default(self):
        # Boards v1 is READ-ONLY. Using ``vso.work`` (not
        # ``.default``) makes the exact permission auditable in
        # Microsoft's token-exchange log and gives us an explicit
        # v2 upgrade path (``vso.work_write`` when comment posting
        # ships). Regression guard: if this switches to ``.default``,
        # revert -- feedback_enterprise_security says request only
        # what you use.
        assert "/vso.work " in token_module.AZDO_BOARDS_SCOPES
        assert "/.default" not in token_module.AZDO_BOARDS_SCOPES
        assert "vso.work_write" not in token_module.AZDO_BOARDS_SCOPES
        assert "offline_access" in token_module.AZDO_BOARDS_SCOPES
        assert token_module.AZDO_RESOURCE_ID in token_module.AZDO_BOARDS_SCOPES

    def test_pk_prefix_matches_license_layer_registration(self):
        # registered AZDO_BOARDS_PROJECT# in the license
        # layer for source counting; this is the OAuth-token-row
        # counterpart. Both must use the AZDO_BOARDS_ family
        # convention so operators can grep for a Boards row and
        # find every related record.
        assert token_module._PK_PREFIX == "AZDO_BOARDS_TOKEN#"

    def test_pk_prefix_distinct_from_repos(self):
        # Regression guard: a Boards refresh token in the SAME row
        # as a Repos token would collide on save/rotate. Independent
        # PK prefixes are how the two integrations stay decoupled.
        assert token_module._PK_PREFIX != "AZDO_TOKEN#"


# ============================================================================
# Delegation contract: wrapper functions pass the right Boards-specific
# params into the shared module.
# ============================================================================


class TestSaveTokensDelegation:

    @patch("azdo_boards_int_token_manager._save_tokens")
    def test_passes_boards_pk_prefix_and_env_args(self, mock_shared):
        mock_shared.return_value = True
        result = token_module.save_tokens("conn1", "access_x", "refresh_x", 1800)
        assert result is True
        kw = mock_shared.call_args.kwargs
        assert kw["pk_prefix"] == "AZDO_BOARDS_TOKEN#"
        assert kw["connection_id"] == "conn1"
        assert kw["access_token"] == "access_x"
        assert kw["refresh_token"] == "refresh_x"
        assert kw["expires_in"] == 1800

    @patch("azdo_boards_int_token_manager._save_tokens")
    def test_default_expires_in_is_3600(self, mock_shared):
        mock_shared.return_value = True
        token_module.save_tokens("conn1", "a", "r")
        assert mock_shared.call_args.kwargs["expires_in"] == 3600


class TestGetRefreshTokenDelegation:

    @patch("azdo_boards_int_token_manager._get_refresh_token")
    def test_passes_boards_pk_prefix(self, mock_shared):
        mock_shared.return_value = "decrypted-refresh"
        result = token_module.get_refresh_token("conn1")
        assert result == "decrypted-refresh"
        kw = mock_shared.call_args.kwargs
        assert kw["pk_prefix"] == "AZDO_BOARDS_TOKEN#"
        assert kw["connection_id"] == "conn1"


class TestRefreshAccessTokenDelegation:

    @patch("azdo_boards_int_token_manager._refresh_access_token")
    def test_passes_boards_scopes_and_tenant_id(self, mock_shared):
        mock_shared.return_value = "new-access-token"
        result = token_module.refresh_access_token(
            "conn1",
            tenant_id="example-tenant-uuid",
            client_id="cid",
            client_secret="csecret",
        )
        assert result == "new-access-token"
        kw = mock_shared.call_args.kwargs
        assert kw["pk_prefix"] == "AZDO_BOARDS_TOKEN#"
        assert kw["scopes"] == token_module.AZDO_BOARDS_SCOPES
        # Critical: ADO is single-tenant per customer; tenant flows
        # through. NOT "common".
        assert kw["tenant_id"] == "example-tenant-uuid"
        assert kw["client_id"] == "cid"
        assert kw["client_secret"] == "csecret"

    @patch("azdo_boards_int_token_manager._refresh_access_token")
    def test_does_not_hardcode_common(self, mock_shared):
        """Regression guard: if a future contributor adds a default
        tenant_id="common" to the wrapper, ADO connections break
        because Entra ID rejects ``/common/`` for single-tenant apps.
        """
        mock_shared.return_value = None
        token_module.refresh_access_token(
            "conn1",
            tenant_id="another-tenant",
            client_id="cid",
            client_secret="csecret",
        )
        assert mock_shared.call_args.kwargs["tenant_id"] == "another-tenant"
        assert mock_shared.call_args.kwargs["tenant_id"] != "common"


class TestDeleteTokensDelegation:

    @patch("azdo_boards_int_token_manager._delete_tokens")
    def test_passes_boards_pk_prefix(self, mock_shared):
        mock_shared.return_value = True
        result = token_module.delete_tokens("conn1")
        assert result is True
        kw = mock_shared.call_args.kwargs
        assert kw["pk_prefix"] == "AZDO_BOARDS_TOKEN#"
        assert kw["connection_id"] == "conn1"


class TestExchangeCodeForTokensDelegation:

    @patch("azdo_boards_int_token_manager._exchange_code_for_tokens")
    def test_passes_boards_scopes_and_tenant_id(self, mock_shared):
        mock_shared.return_value = ("access_x", "refresh_x", 3600)
        result = token_module.exchange_code_for_tokens(
            code="auth-code",
            tenant_id="example-tenant-uuid",
            client_id="cid",
            client_secret="csecret",
            redirect_uri="https://example.com/api/azure-devops-boards/callback",
        )
        assert result == ("access_x", "refresh_x", 3600)
        kw = mock_shared.call_args.kwargs
        assert kw["scopes"] == token_module.AZDO_BOARDS_SCOPES
        assert kw["tenant_id"] == "example-tenant-uuid"
        assert kw["code"] == "auth-code"
        assert kw["redirect_uri"] == "https://example.com/api/azure-devops-boards/callback"


# ============================================================================
# ADO-specific helper: get_user_profile (same endpoint as Repos)
# ============================================================================


def _make_urlopen_response(body_dict):
    resp = MagicMock()
    resp.read.return_value = json.dumps(body_dict).encode("utf-8")
    resp.__enter__ = MagicMock(return_value=resp)
    resp.__exit__ = MagicMock(return_value=False)
    return resp


class TestGetUserProfile:

    @patch("azdo_boards_int_token_manager.urllib.request.urlopen")
    def test_hits_vssps_endpoint_with_bearer_token(self, mock_urlopen):
        mock_urlopen.return_value = _make_urlopen_response({
            "emailAddress": "user@example.onmicrosoft.com",
            "displayName": "Jane Doe",
            "id": "uuid-here",
        })
        result = token_module.get_user_profile("test-access-token")
        assert result == {
            "emailAddress": "user@example.onmicrosoft.com",
            "displayName": "Jane Doe",
            "id": "uuid-here",
        }
        request_obj = mock_urlopen.call_args.args[0]
        assert request_obj.full_url == token_module.AZDO_PROFILE_URL
        assert "vssps.visualstudio.com" in request_obj.full_url
        assert "api-version=7.0" in request_obj.full_url
        assert request_obj.headers["Authorization"] == "Bearer test-access-token"
        assert request_obj.headers["Accept"] == "application/json"
        assert request_obj.get_method() == "GET"

    @patch("azdo_boards_int_token_manager.urllib.request.urlopen")
    def test_returns_none_on_http_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url=token_module.AZDO_PROFILE_URL,
            code=401,
            msg="Unauthorized",
            hdrs={},
            fp=None,
        )
        assert token_module.get_user_profile("expired") is None

    @patch("azdo_boards_int_token_manager.urllib.request.urlopen")
    def test_returns_none_on_network_error(self, mock_urlopen):
        import urllib.error
        mock_urlopen.side_effect = urllib.error.URLError("connection refused")
        assert token_module.get_user_profile("a") is None
