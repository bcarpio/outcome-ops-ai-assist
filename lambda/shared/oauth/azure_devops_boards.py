# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Token Manager for Azure DevOps Boards OAuth tokens.

Thin wrapper over ``shared.entra_id_oauth`` -- the integration owns
the scope string + PK prefix + per-customer tenant_id; the shared
module owns the HTTP, KMS, and DDB plumbing. See
``lambda/shared/entra_id_oauth.py`` for the contract and the
deltas-reconciled history.

Boards vs Repos (both use the same shared module):

1. **Explicit ``vso.work`` scope, NOT ``.default``.** Repos uses
   ``.default`` because it opportunistically consumes many
   sub-scopes (``vso.code``, ``vso.code_write``, PR threads,
   etc.). Boards v1 is read-only work items -- exactly one
   sub-scope. Using the explicit scope documents the exact
   permission in Microsoft's per-token-exchange audit log,
   prevents accidental write operations if a customer admin
   grants broader permissions on the shared app registration,
   and gives us an explicit v2 upgrade path (``vso.work_write``
   when work-item comment posting ships). Aligns with
   feedback_enterprise_security -- request only what you use.

2. **Distinct DDB PK prefix (``AZDO_BOARDS_TOKEN#``).** Repos
   stores under ``AZDO_TOKEN#``. Independent refresh tokens per
   integration mean Repos and Boards can be disconnected
   independently and each carries a distinct OAuth consent.

3. **Same tenant_id + client_id + client_secret plumbing as
   Repos.** ADO is single-tenant per customer (``.default`` /
   ``vso.work`` both live under ``login.microsoftonline.com/{tenant_id}/``,
   not ``/common/``). The wrapper accepts ``tenant_id`` on every
   call that touches Microsoft's token endpoint; the caller
   pulls it from ``var.azure_devops_tenant_id`` (env
   AZURE_DEVOPS_TENANT_ID) -- reused across Repos + Boards.

4. **``get_user_profile()`` calls ADO's vssps endpoint.** Same
   endpoint Repos uses; not Microsoft Graph. The shared module is
   OAuth-only; provider-specific API calls live here.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Optional, Tuple

from shared.entra_id_oauth import (
    delete_tokens as _delete_tokens,
    exchange_code_for_tokens as _exchange_code_for_tokens,
    get_refresh_token as _get_refresh_token,
    refresh_access_token as _refresh_access_token,
    save_tokens as _save_tokens,
)

logger = logging.getLogger(__name__)


WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KMS_KEY_ARN = os.environ.get("OAUTH_KMS_KEY_ARN")

# Azure DevOps well-known Entra ID resource ID (documented by
# Microsoft; never changes). Same value the Repos integration uses --
# both integrations authenticate against the same target resource.
AZDO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"

# Explicit ``vso.work`` scope for read-only work-item access. When v2
# adds work-item comment posting, upgrade to ``vso.work_write`` in a
# separate scope constant + migrate existing customers via re-consent
# (documented in the v2 plan). Do NOT flip to ``.default`` here --
# the explicit scope IS the contract with SecOps reviewers per the
# module docstring's rationale.
AZDO_BOARDS_SCOPES = (
    f"{AZDO_RESOURCE_ID}/vso.work offline_access openid profile"
)

# ADO user-profile endpoint (separate host from dev.azure.com).
# Same endpoint the Repos wrapper uses.
AZDO_PROFILE_URL = (
    "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0"
)

_PK_PREFIX = "AZDO_BOARDS_TOKEN#"


def save_tokens(
    connection_id: str,
    access_token: str,
    refresh_token: str,
    expires_in: int = 3600,
) -> bool:
    return _save_tokens(
        workspaces_table=WORKSPACES_TABLE,
        kms_key_arn=KMS_KEY_ARN,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
        access_token=access_token,
        refresh_token=refresh_token,
        expires_in=expires_in,
    )


def get_refresh_token(connection_id: str) -> Optional[str]:
    return _get_refresh_token(
        workspaces_table=WORKSPACES_TABLE,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
    )


def refresh_access_token(
    connection_id: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
) -> Optional[str]:
    """Refresh the access token for a Boards connection.

    Note the extra ``tenant_id`` arg compared to the Microsoft-Graph
    wrappers -- ADO is single-tenant per customer, so the tenant
    flows from tfvars through the handler down to here on every
    call.
    """
    return _refresh_access_token(
        workspaces_table=WORKSPACES_TABLE,
        kms_key_arn=KMS_KEY_ARN,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
        client_id=client_id,
        client_secret=client_secret,
        scopes=AZDO_BOARDS_SCOPES,
        tenant_id=tenant_id,
    )


def delete_tokens(connection_id: str) -> bool:
    return _delete_tokens(
        workspaces_table=WORKSPACES_TABLE,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
    )


def exchange_code_for_tokens(
    code: str,
    tenant_id: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Exchange an OAuth authorization code for tokens against the
    customer's Entra ID tenant. ADO requires the specific tenant
    (not ``/common/``) because the app registration is single-tenant.
    """
    return _exchange_code_for_tokens(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=AZDO_BOARDS_SCOPES,
        tenant_id=tenant_id,
    )


def get_user_profile(access_token: str) -> Optional[dict]:
    """Fetch the connected user's ADO profile (email + display name).

    Same endpoint the Repos wrapper uses -- vssps profiles is
    integration-agnostic. Returns a dict with at least ``emailAddress``
    and ``displayName`` on success, or ``None`` on HTTP/network error.
    Callers map None to a UI-visible "couldn't read your profile" error.
    """
    request = urllib.request.Request(
        AZDO_PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8") if e.fp else ""
        logger.error(f"ADO Boards profile fetch failed: {e.code} - {error_body}")
        return None
    except urllib.error.URLError as e:
        logger.error(f"Network error during ADO Boards profile fetch: {e.reason}")
        return None
