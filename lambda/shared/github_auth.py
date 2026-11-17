# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""GitHub App installation-token cache (fable-scale chunk 06).

Prior behavior: every sync/PR-check minted a fresh JWT + called
`POST /app/installations/{id}/access_tokens` to get an installation token.
At thousands of repos + org-wide PR analysis that's an unnecessary tax on
the 5000 req/hr GitHub App ceiling AND adds ~200ms latency per call.

This module caches the token per installation_id in SSM Parameter Store
(SecureString, KMS-encrypted). Tokens are valid for 60 minutes; we cache
for 55 to avoid a race with expiry.

Cross-Lambda-instance sharing via SSM means one Lambda mint serves many
concurrent invocations org-wide. Cache miss rate falls from ~100% to
~1/hr per installation regardless of concurrency.

Env:
- GITHUB_TOKEN_CACHE_SSM_PREFIX (default: /{env}/{app}/github/installation-token)
- GITHUB_TOKEN_CACHE_TTL_S      (default: 3300 seconds == 55 minutes)
- GITHUB_TOKEN_CACHE_DISABLED   (default: "false"; set truthy to bypass cache)
"""
from __future__ import annotations

import json
import logging
import os
import time
from typing import Callable, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

def _cache_ttl_s() -> int:
    try:
        return int(os.environ.get("GITHUB_TOKEN_CACHE_TTL_S", "3300"))
    except ValueError:
        return 3300


def _cache_disabled() -> bool:
    return os.environ.get(
        "GITHUB_TOKEN_CACHE_DISABLED", "false"
    ).lower() in ("true", "1", "yes")


def _default_ssm_prefix() -> str:
    override = os.environ.get("GITHUB_TOKEN_CACHE_SSM_PREFIX")
    if override:
        return override
    env = os.environ.get("ENVIRONMENT") or os.environ.get("ENV") or "prd"
    app = os.environ.get("APP_NAME", "outcome-ops-ai-assist")
    return f"/{env}/{app}/github/installation-token"


_ssm_client = None


def _get_ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _param_name(installation_id: str) -> str:
    return f"{_default_ssm_prefix()}/{installation_id}"


def _load_cached(installation_id: str) -> Optional[dict]:
    if _cache_disabled():
        return None
    try:
        resp = _get_ssm().get_parameter(
            Name=_param_name(installation_id),
            WithDecryption=True,
        )
        payload = json.loads(resp["Parameter"]["Value"])
        # Reject stale rows client-side. 60s safety margin against clock skew.
        if float(payload.get("expires_at", 0)) > time.time() + 60:
            return payload
        return None
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code != "ParameterNotFound":
            logger.warning(
                "github_auth: SSM get_parameter failed for installation=%s: %s",
                installation_id, e,
            )
        return None
    except (ValueError, KeyError) as e:
        logger.warning(
            "github_auth: SSM cache value malformed for installation=%s: %s",
            installation_id, e,
        )
        return None


def _write_cached(installation_id: str, token: str, expires_at: float) -> None:
    if _cache_disabled():
        return
    payload = json.dumps({"token": token, "expires_at": expires_at})
    try:
        _get_ssm().put_parameter(
            Name=_param_name(installation_id),
            Value=payload,
            Type="SecureString",
            Overwrite=True,
        )
    except ClientError as e:
        logger.warning(
            "github_auth: SSM put_parameter failed for installation=%s: %s",
            installation_id, e,
        )


def get_installation_token(
    installation_id: str,
    mint_fn: Callable[[str], str],
) -> str:
    """Return a cached installation token, minting via `mint_fn` on miss.

    `mint_fn(installation_id)` should call GitHub's POST
    /app/installations/{id}/access_tokens and return the token string.
    Signature is intentionally decoupled so this module doesn't own the
    JWT + urllib plumbing (github-sync already has that).
    """
    cached = _load_cached(installation_id)
    if cached is not None:
        return cached["token"]

    logger.info("github_auth: cache miss installation=%s; minting", installation_id)
    token = mint_fn(installation_id)
    expires_at = time.time() + _cache_ttl_s()
    _write_cached(installation_id, token, expires_at)
    return token
