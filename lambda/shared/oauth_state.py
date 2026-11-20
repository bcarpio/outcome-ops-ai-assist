"""One-time OAuth ``state`` tokens bound to the admin who started the flow.

The authorize step (which checks that the caller is a workspace admin)
issues a random token and stores the workspace, the admin's email, and any
provider-specific extras server-side. The OAuth callback, which has no
caller identity of its own, consumes that token exactly once and takes the
workspace and user from the stored row, never from the browser.

Before this, most integrations put ``{"workspace_id": ...}`` (and sometimes
``user_email``) into ``state`` as plain JSON and the callback trusted it, so
anyone could hand-build a provider authorize URL with another workspace's id
and attach their own account to it, bypassing the admin check, with an
audit row naming whatever email they typed. Salesforce and ServiceNow
already used server-side state; this module generalizes that pattern.

Rows live in the workspaces table (every integration Lambda already has
read/write access to it) under ``PK = SK = OAUTH_STATE#<token>``. Consume
is an atomic conditional delete, so a token cannot be replayed even by two
concurrent callbacks. Expiry is enforced in code (``expires_at``), not by
DynamoDB TTL, so a stale token is rejected even if the row still exists.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
from typing import Any, Dict, Optional

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# Long enough for a user to finish a provider consent screen (including an
# SSO hop), short enough that a leaked authorize URL goes dead quickly.
STATE_TTL_SECONDS = 900

_KEY_PREFIX = "OAUTH_STATE#"

_dynamodb_resource = None


def _table(table_name: Optional[str]):
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    name = table_name or os.environ.get("WORKSPACES_TABLE", "")
    if not name:
        raise RuntimeError("WORKSPACES_TABLE is not configured")
    return _dynamodb_resource.Table(name)


def _reset_clients_for_tests() -> None:
    global _dynamodb_resource
    _dynamodb_resource = None


def issue_state(
    provider: str,
    workspace_id: str,
    user_email: str,
    extra: Optional[Dict[str, Any]] = None,
    *,
    table_name: Optional[str] = None,
) -> str:
    """Store a one-time state row and return its opaque token.

    Call only after the caller has been authorized for ``workspace_id``.
    ``extra`` carries provider-specific values the callback needs (for
    example a GitLab ``instance_id``); they come back from
    ``consume_state`` unchanged.
    """

    if not provider or not workspace_id or not user_email:
        raise ValueError("provider, workspace_id and user_email are required")
    token = secrets.token_urlsafe(32)
    now = int(time.time())
    _table(table_name).put_item(
        Item={
            "PK": f"{_KEY_PREFIX}{token}",
            "SK": f"{_KEY_PREFIX}{token}",
            "entity_type": "oauth_state",
            "provider": provider,
            "workspace_id": workspace_id,
            "user_email": user_email,
            "extra": dict(extra or {}),
            "created_at": now,
            "expires_at": now + STATE_TTL_SECONDS,
        }
    )
    return token


def consume_state(
    provider: str,
    token: Optional[str],
    *,
    table_name: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Atomically delete the state row and return its payload.

    Returns ``{"workspace_id", "user_email", **extra}``, or None when the
    token is missing, unknown, already used, expired, or was issued for a
    different provider. Never raises for those cases; callers turn None
    into their ``invalid_state`` redirect.
    """

    if not token or not isinstance(token, str):
        return None
    key = {"PK": f"{_KEY_PREFIX}{token}", "SK": f"{_KEY_PREFIX}{token}"}
    try:
        resp = _table(table_name).delete_item(
            Key=key,
            ConditionExpression="attribute_exists(PK)",
            ReturnValues="ALL_OLD",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            logger.warning("[oauth_state] unknown or already-used state for %s", provider)
            return None
        raise
    item = resp.get("Attributes") or {}
    if item.get("provider") != provider:
        logger.warning(
            "[oauth_state] state issued for %s presented to %s callback",
            item.get("provider"), provider,
        )
        return None
    if int(item.get("expires_at", 0)) < int(time.time()):
        logger.warning(
            "[oauth_state] expired %s state for workspace %s",
            provider, item.get("workspace_id"),
        )
        return None
    payload: Dict[str, Any] = dict(item.get("extra") or {})
    payload["workspace_id"] = item["workspace_id"]
    payload["user_email"] = item["user_email"]
    return payload
