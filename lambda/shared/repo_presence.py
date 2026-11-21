# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Answer "does this workspace still have this repo connected?".

Ingestion writers and the delete worker call this before (re)writing
derived data for a repo, so a repo that was disconnected mid-flight does
not get its documents, code maps or summaries resurrected after the delete
already swept them.

Connected repos are rows in the workspaces table, keyed per provider:

    github        PK=WS#<workspace_id>  SK=GITHUB_REPO#<owner>/<repo>
    gitlab        PK=WS#<workspace_id>  SK=GITLAB_REPO#<group>[/<sub>...]/<repo>
    azure_devops  PK=WS#<workspace_id>  SK=AZDO_REPO#<org>/<project>/<repo>

A wrong SK prefix silently reads nothing, so the prefixes live here once.
The lookup is a strongly consistent GetItem, so a delete that already
committed is always visible. If the lookup itself fails, this raises
``RepoPresenceError`` instead of guessing; callers fail closed.
"""

from __future__ import annotations

import logging
import os
from typing import Dict, Optional

import boto3
from botocore.exceptions import BotoCoreError, ClientError

logger = logging.getLogger(__name__)

SOURCE_SK_PREFIX = {
    "github": "GITHUB_REPO#",
    "gitlab": "GITLAB_REPO#",
    "azure_devops": "AZDO_REPO#",
}

_dynamodb_resource = None


class RepoPresenceError(Exception):
    """The presence lookup itself failed (DDB error, missing table config)."""


def _reset_clients_for_tests() -> None:
    global _dynamodb_resource
    _dynamodb_resource = None


def _get_dynamodb_resource():
    global _dynamodb_resource
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def repo_row_key(workspace_id: str, source: str, repo_slug: str) -> Dict[str, str]:
    """Return the workspaces-table key of the repo row for ``source``.

    ``repo_slug`` is the full provider slug (``owner/repo``,
    ``group/sub/repo``, ``org/project/repo``). One leading and one trailing
    ``/`` are stripped; case is preserved because slugs are stored as
    entered. Raises ``ValueError`` on an unknown source or an empty
    ``workspace_id`` / ``repo_slug``.
    """
    prefix = SOURCE_SK_PREFIX.get(source)
    if prefix is None:
        raise ValueError(f"unknown repo source: {source!r}")
    if not workspace_id:
        raise ValueError("workspace_id is required")
    slug = repo_slug or ""
    if slug.startswith("/"):
        slug = slug[1:]
    if slug.endswith("/"):
        slug = slug[:-1]
    if not slug:
        raise ValueError("repo_slug is required")
    return {"PK": f"WS#{workspace_id}", "SK": f"{prefix}{slug}"}


def is_repo_connected(
    workspace_id: str,
    source: str,
    repo_slug: str,
    *,
    table_name: Optional[str] = None,
) -> bool:
    """True iff the workspace's repo row for ``source``/``repo_slug`` exists.

    Uses a strongly consistent GetItem projecting only ``PK``. The table
    defaults to the ``WORKSPACES_TABLE`` env var, read at call time.
    Raises ``ValueError`` for bad arguments and ``RepoPresenceError`` when
    no table is configured or the DynamoDB call fails.
    """
    key = repo_row_key(workspace_id, source, repo_slug)
    name = table_name or os.environ.get("WORKSPACES_TABLE", "")
    if not name:
        raise RepoPresenceError("WORKSPACES_TABLE is not configured")
    try:
        table = _get_dynamodb_resource().Table(name)
        resp = table.get_item(
            Key=key,
            ConsistentRead=True,
            ProjectionExpression="PK",
        )
    except (ClientError, BotoCoreError) as exc:
        logger.exception(
            "[repo_presence] GetItem failed for %s %s in %s",
            key["PK"], key["SK"], name,
        )
        raise RepoPresenceError(
            f"repo presence lookup failed for {key['SK']}: {exc}"
        ) from exc
    return "Item" in resp
