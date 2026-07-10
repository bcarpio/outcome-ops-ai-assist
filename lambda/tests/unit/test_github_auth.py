"""Unit tests for shared/github_auth (fable-scale chunk 06).

Covers the SSM-cached installation-token wrapper:
- Cache hit skips the mint function.
- Cache miss (ParameterNotFound) calls mint, then writes back.
- Stale cache entries (expires_at in the past) trigger a fresh mint.
- Cache disabled env var bypasses SSM entirely.
- SSM ClientError falls through to a fresh mint (fail-open).
- Malformed cache values force a mint.
"""
import importlib
import json
import os
import sys
import time
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError


def _load(**envs):
    for k, v in envs.items():
        os.environ[k] = v
    for k in list(os.environ):
        if k in ("GITHUB_TOKEN_CACHE_DISABLED", "GITHUB_TOKEN_CACHE_TTL_S",
                 "GITHUB_TOKEN_CACHE_SSM_PREFIX"):
            if k not in envs:
                os.environ.pop(k, None)
    if "outcomeops_github_auth" in sys.modules:
        del sys.modules["outcomeops_github_auth"]
    lambda_root = os.path.join(os.path.dirname(__file__), "../..")
    if lambda_root not in sys.path:
        sys.path.insert(0, lambda_root)
    return importlib.import_module("shared.github_auth")


def _param_not_found():
    return ClientError(
        {"Error": {"Code": "ParameterNotFound", "Message": ""}},
        operation_name="GetParameter",
    )


class TestCacheHit:
    def test_valid_cache_returns_stored_token(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache")
        fake_ssm = MagicMock()
        payload = json.dumps({"token": "cached-tok", "expires_at": time.time() + 600})
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": payload}}
        mod._ssm_client = fake_ssm

        mint = MagicMock()
        token = mod.get_installation_token("42", mint)
        assert token == "cached-tok"
        mint.assert_not_called()
        fake_ssm.get_parameter.assert_called_once()
        fake_ssm.put_parameter.assert_not_called()


class TestCacheMiss:
    def test_missing_param_triggers_mint_and_write(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache", GITHUB_TOKEN_CACHE_TTL_S="3300")
        fake_ssm = MagicMock()
        fake_ssm.get_parameter.side_effect = _param_not_found()
        mod._ssm_client = fake_ssm

        mint = MagicMock(return_value="fresh-tok")
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"
        mint.assert_called_once_with("42")
        fake_ssm.put_parameter.assert_called_once()
        put_kwargs = fake_ssm.put_parameter.call_args.kwargs
        assert put_kwargs["Type"] == "SecureString"
        assert put_kwargs["Name"] == "/test/cache/42"
        cached = json.loads(put_kwargs["Value"])
        assert cached["token"] == "fresh-tok"
        assert cached["expires_at"] > time.time() + 3000

    def test_stale_cache_forces_fresh_mint(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache")
        fake_ssm = MagicMock()
        payload = json.dumps({"token": "old-tok", "expires_at": time.time() - 10})
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": payload}}
        mod._ssm_client = fake_ssm

        mint = MagicMock(return_value="fresh-tok")
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"
        mint.assert_called_once()

    def test_malformed_cache_forces_fresh_mint(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache")
        fake_ssm = MagicMock()
        fake_ssm.get_parameter.return_value = {"Parameter": {"Value": "not-json"}}
        mod._ssm_client = fake_ssm

        mint = MagicMock(return_value="fresh-tok")
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"


class TestCacheDisabled:
    def test_disabled_env_bypasses_ssm(self):
        mod = _load(GITHUB_TOKEN_CACHE_DISABLED="true")
        fake_ssm = MagicMock()
        mod._ssm_client = fake_ssm

        mint = MagicMock(return_value="fresh-tok")
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"
        # No SSM ops.
        fake_ssm.get_parameter.assert_not_called()
        fake_ssm.put_parameter.assert_not_called()


class TestFailOpen:
    def test_ssm_get_error_falls_through_to_mint(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache")
        fake_ssm = MagicMock()
        fake_ssm.get_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            operation_name="GetParameter",
        )
        mod._ssm_client = fake_ssm
        mint = MagicMock(return_value="fresh-tok")
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"

    def test_ssm_put_error_does_not_break_mint_return(self):
        mod = _load(GITHUB_TOKEN_CACHE_SSM_PREFIX="/test/cache")
        fake_ssm = MagicMock()
        fake_ssm.get_parameter.side_effect = _param_not_found()
        fake_ssm.put_parameter.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
            operation_name="PutParameter",
        )
        mod._ssm_client = fake_ssm
        mint = MagicMock(return_value="fresh-tok")
        # Token should still return even if the cache write failed.
        token = mod.get_installation_token("42", mint)
        assert token == "fresh-tok"
