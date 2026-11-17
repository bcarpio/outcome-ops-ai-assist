# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Token Manager for Azure DevOps OAuth tokens.

Thin wrapper over ``shared.entra_id_oauth`` -- the integration owns
the scope string + PK prefix + per-customer tenant_id; the shared
module owns the HTTP, KMS, and DDB plumbing. See
``lambda/shared/entra_id_oauth.py`` for the contract and the
deltas-reconciled history.

ADO-specific surface vs the 5 Microsoft-Graph wrappers
(outlook/teams/onedrive/onenote/sharepoint):

1. **tenant_id is per-customer**, not ``"common"``. ADO uses the
   single-tenant authority because the OAuth app is registered in
   the customer's Entra ID tenant (per ADR-035-style architecture
   captured in ADR). The wrapper accepts ``tenant_id``
   as an argument on every function that touches Microsoft's token
   endpoint; the caller pulls it from
   ``var.azure_devops_tenant_id`` (env: AZURE_DEVOPS_TENANT_ID).

2. **Resource ID is Azure DevOps**, not Microsoft Graph. The well-
   known resource ID ``499b84ac-1321-427f-aa17-267ca6975798`` is
   documented by Microsoft and never changes. The ``.default``
   scope grants whatever sub-permissions (vso.code, vso.code_write,
   etc.) the customer's tenant admin consented to at app-
   registration time. If they only consented to read, ingest works
   but PR-comment posting fails at the API call (handler maps the
   403 to a user-visible "needs vso.code_write" message in ).

3. **get_user_profile()** calls ADO's vssps endpoint, NOT Microsoft
   Graph's ``/me``. The shared module is OAuth-only; provider-
   specific API calls live here.
"""

import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Optional, Tuple

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

# Azure DevOps well-known Entra ID resource ID + ``.default`` scope.
# Documented by Microsoft; never changes. ``.default`` grants the
# union of whatever sub-scopes (vso.code, vso.code_write, etc.) the
# customer's tenant admin allowed at app registration time.
AZDO_RESOURCE_ID = "499b84ac-1321-427f-aa17-267ca6975798"
AZDO_SCOPES = f"{AZDO_RESOURCE_ID}/.default offline_access openid profile"

# ADO user-profile endpoint (separate host from dev.azure.com).
AZDO_PROFILE_URL = (
    "https://app.vssps.visualstudio.com/_apis/profile/profiles/me?api-version=7.0"
)

_PK_PREFIX = "AZDO_TOKEN#"


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
    """Refresh the access token for an ADO connection.

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
        scopes=AZDO_SCOPES,
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
    (not ``/common/``) because the app registration is single-tenant."""
    return _exchange_code_for_tokens(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=AZDO_SCOPES,
        tenant_id=tenant_id,
    )


def get_user_profile(access_token: str) -> Optional[dict]:
    """Fetch the connected user's ADO profile (email + display name).

    ADO-specific: calls ``app.vssps.visualstudio.com``, not Microsoft
    Graph's ``/me``. The shared module is OAuth-only; this provider-
    specific API call lives in the wrapper.

    Returns a dict with at least ``emailAddress`` and ``displayName``
    on success, or ``None`` on HTTP/network error. Callers map None
    to a UI-visible "couldn't read your profile" error.
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
        logger.error(f"ADO profile fetch failed: {e.code} - {error_body}")
        return None
    except urllib.error.URLError as e:
        logger.error(f"Network error during ADO profile fetch: {e.reason}")
        return None
