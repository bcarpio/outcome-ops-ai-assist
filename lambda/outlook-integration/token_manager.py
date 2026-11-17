# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Token Manager for Outlook OAuth tokens.

Thin wrapper over ``shared.entra_id_oauth`` -- the integration owns
the scope string + PK prefix; the shared module owns the HTTP,
KMS, and DDB plumbing. See ``lambda/shared/entra_id_oauth.py`` for
the contract and the deltas-reconciled history.
"""

import os
from typing import Optional, Tuple

from shared.entra_id_oauth import (
    delete_tokens as _delete_tokens,
    exchange_code_for_tokens as _exchange_code_for_tokens,
    get_refresh_token as _get_refresh_token,
    refresh_access_token as _refresh_access_token,
    save_tokens as _save_tokens,
)


WORKSPACES_TABLE = os.environ.get("WORKSPACES_TABLE")
KMS_KEY_ARN = os.environ.get("OAUTH_KMS_KEY_ARN")

OUTLOOK_SCOPES = "Mail.Read Mail.ReadBasic offline_access openid profile email"

_PK_PREFIX = "OUTLOOK_TOKEN#"


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
    client_id: str,
    client_secret: str,
) -> Optional[str]:
    return _refresh_access_token(
        workspaces_table=WORKSPACES_TABLE,
        kms_key_arn=KMS_KEY_ARN,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
        client_id=client_id,
        client_secret=client_secret,
        scopes=OUTLOOK_SCOPES,
    )


def delete_tokens(connection_id: str) -> bool:
    return _delete_tokens(
        workspaces_table=WORKSPACES_TABLE,
        pk_prefix=_PK_PREFIX,
        connection_id=connection_id,
    )


def exchange_code_for_tokens(
    code: str,
    client_id: str,
    client_secret: str,
    redirect_uri: str,
) -> Tuple[Optional[str], Optional[str], Optional[int]]:
    return _exchange_code_for_tokens(
        code=code,
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scopes=OUTLOOK_SCOPES,
    )
