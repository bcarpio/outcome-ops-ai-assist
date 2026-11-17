# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Handler-local ADO token refresh helper.

The three retrieval-heavy check handlers (ADR_COMPLIANCE,
ARCHITECTURAL_DUPLICATION) plus the three introspection-heavy ones
(README_FRESHNESS, LICENSE_COMPLIANCE, TEST_COVERAGE) all need the
same "refresh a customer's ADO OAuth access token from a connection_id"
call. Rather than each handler duplicate the env-var + SSM plumbing,
this helper wraps ``azure_devops_token_manager.refresh_access_token``.

Kept in ``check_handlers/`` because:

- It reads the same ``AZURE_DEVOPS_TENANT_ID`` / ``AZURE_DEVOPS_CLIENT_ID``
  env vars + ``AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM`` that the parent
  handler.py already wires. The check
  handlers are peers of that helper, not consumers of an abstraction
  layer above.
- The ADO token manager itself is a local module bundled into the
  process-pr-check zip (not in ``lambda/shared/``). Keeping this
  helper local avoids a shared → integration-Lambda dependency
  direction.
"""

import logging
import os
from typing import Optional

import boto3


logger = logging.getLogger(__name__)


_ssm_client = None
_ado_client_secret_cache: Optional[str] = None


def _get_ssm():
    global _ssm_client
    if _ssm_client is None:
        _ssm_client = boto3.client("ssm")
    return _ssm_client


def _get_ado_client_secret() -> str:
    """One-shot SSM read of the ADO Entra ID app's client secret.

    Mirrors the same-named helper in handler.py so check_handlers can
    refresh access tokens without importing the parent handler
    (avoids a circular dep during unit-test module loading).
    """
    global _ado_client_secret_cache
    if _ado_client_secret_cache is not None:
        return _ado_client_secret_cache
    param_name = os.environ.get("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", "")
    if not param_name:
        raise RuntimeError(
            "AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM not configured"
        )
    response = _get_ssm().get_parameter(Name=param_name, WithDecryption=True)
    _ado_client_secret_cache = response["Parameter"]["Value"]
    return _ado_client_secret_cache


def refresh_ado_token(connection_id: str) -> str:
    """Refresh the ADO OAuth access token for a customer connection.

    Returns a fresh access token string. Raises ``RuntimeError`` if
    any of the pieces (env vars, SSM secret, refresh grant) is
    missing/invalid — callers surface a "reconnect_required" UI
    message on that.

    Import of ``azure_devops_token_manager`` is deferred to call time
    because the module lives adjacent to the parent handler.py, and
    check_handlers/ is imported before handler.py has adjusted
    sys.path. Deferring keeps unit tests loadable regardless of
    import order.
    """
    if not connection_id:
        raise ValueError("connection_id required for ADO token refresh")

    tenant_id = os.environ.get("AZURE_DEVOPS_TENANT_ID", "")
    client_id = os.environ.get("AZURE_DEVOPS_CLIENT_ID", "")
    if not (tenant_id and client_id):
        raise RuntimeError(
            "Azure DevOps OAuth app not configured "
            "(AZURE_DEVOPS_TENANT_ID / AZURE_DEVOPS_CLIENT_ID env vars missing)"
        )

    from shared.oauth.azure_devops import (  # noqa: E402
        refresh_access_token as _refresh,
    )

    client_secret = _get_ado_client_secret()
    access_token = _refresh(
        connection_id=connection_id,
        tenant_id=tenant_id,
        client_id=client_id,
        client_secret=client_secret,
    )
    if not access_token:
        raise RuntimeError(
            "Failed to refresh Azure DevOps access token; reconnect_required"
        )
    return access_token


def reset_client_secret_cache() -> None:
    """Test-only: clear the cached client secret so patches take effect."""
    global _ado_client_secret_cache
    _ado_client_secret_cache = None
