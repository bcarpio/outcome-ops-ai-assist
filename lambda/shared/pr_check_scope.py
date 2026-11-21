# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Per-check-type retrieval scope resolver for PR checks.

Fixes PR checks that queried the KB workspace-blind (ADR-037).
Different checks want different retrieval scopes:

- Read-scope checks (ADR_COMPLIANCE / TEST_COVERAGE / LICENSE_COMPLIANCE
  / ARCHITECTURAL_DUPLICATION) retrieve from every workspace the repo's
  workspace may READ: the repo's own workspace(s) + all ``is_global``
  workspaces + every workspace whose ``METADATA.search_allowed_workspaces``
  lists a repo workspace (the Sharing tab grant). Standards may live in any
  connector's content (Confluence etc.) in a global OR a sharing workspace.
- BREAKING_CHANGES finds a changed producer's consumers, so it uses the
  FORWARD consumer scope (ADR-056 section 5), not the read scope.
- README_FRESHNESS is about *this* repo's README specifically; scoped
  to the repo's workspace(s) only. Unknown check types get the same
  local-only fallback.

The repo's own workspace(s) are found per provider (ADR-052): the repo row
SK prefix comes from ``shared.repo_presence.SOURCE_SK_PREFIX``.

Design mirrors ``lambda/chat/handler.py::resolve_chat_workspace_scope``
but the trigger is check_type instead of user membership.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional, Set

import boto3

from shared.repo_presence import SOURCE_SK_PREFIX

logger = logging.getLogger(__name__)


# Read-scope checks: retrieve from every workspace the repo's workspace may
# read (own + is_global + workspaces that shared with it).
READ_SCOPE_CHECKS: Set[str] = {
    "ADR_COMPLIANCE",
    "TEST_COVERAGE",
    "LICENSE_COMPLIANCE",
    "ARCHITECTURAL_DUPLICATION",
}

# Local-only checks: scope to the repo's workspace(s) only. README
# freshness is about this repo's README, not org standards.
LOCAL_ONLY_CHECKS: Set[str] = {
    "README_FRESHNESS",
}


_dynamodb = None


def _get_dynamodb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb")
    return _dynamodb


def _get_workspaces_table_name() -> str:
    return os.environ.get(
        "WORKSPACES_TABLE",
        f"{os.environ.get('ENV', 'dev')}-outcome-ops-ai-assist-workspaces",
    )


def find_workspaces_for_repo(
    repo_name: str,
    table=None,
    *,
    provider: str = "github",
) -> List[str]:
    """Return every workspace_id that has a repo row for this repo.

    A repo can be added to multiple workspaces (rare but supported);
    all owning workspaces contribute to the "local" retrieval scope.

    Scans the workspaces table filtered on
    ``SK = {SOURCE_SK_PREFIX[provider]}{repo_name}`` (``GITHUB_REPO#``,
    ``GITLAB_REPO#`` or ``AZDO_REPO#``; ADR-052 provider parity).
    ``repo_name`` is the provider slug that keys the repo row (GitHub
    ``owner/repo``, GitLab full project path, ADO ``org/project/repo``).
    An unknown provider returns [] and logs a warning; it never falls back
    to the GitHub prefix. Not a hot path (once per PR check per repo);
    paginated for correctness.
    """
    if not repo_name:
        return []
    prefix = SOURCE_SK_PREFIX.get(provider)
    if prefix is None:
        logger.warning(
            "[pr_check_scope] unknown provider %r for repo %s; no repo workspace",
            provider,
            repo_name,
        )
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    workspace_ids: List[str] = []
    scan_kwargs = {
        "FilterExpression": "SK = :sk",
        "ExpressionAttributeValues": {":sk": f"{prefix}{repo_name}"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            ws_id = item.get("workspace_id") or (
                item.get("PK", "").replace("WS#", "") or None
            )
            if ws_id and ws_id not in workspace_ids:
                workspace_ids.append(ws_id)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return workspace_ids


def _list_workspace_ids_by_filter(
    filter_expression: str,
    expr_values: dict,
    table=None,
) -> List[str]:
    """Small scan helper reused for the two "all workspaces" branches."""
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())
    ids: List[str] = []
    scan_kwargs = {
        "FilterExpression": filter_expression,
        "ExpressionAttributeValues": expr_values,
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            ws_id = item.get("workspace_id")
            if ws_id and ws_id not in ids:
                ids.append(ws_id)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return ids


def list_global_workspace_ids(table=None) -> List[str]:
    """All ``is_global=true`` workspaces in the deployment."""
    return _list_workspace_ids_by_filter(
        "SK = :sk AND is_global = :ig",
        {":sk": "METADATA", ":ig": True},
        table=table,
    )


def list_all_workspace_ids(table=None) -> List[str]:
    """Every workspace in the deployment. Used by process-batch-summary and
    by the consumer scope when the producer's workspace is ``is_global``."""
    return _list_workspace_ids_by_filter(
        "SK = :sk AND begins_with(PK, :pk_prefix)",
        {":sk": "METADATA", ":pk_prefix": "WS#"},
        table=table,
    )


def list_workspaces_granting_access(
    workspace_ids: Iterable[str],
    table=None,
) -> List[str]:
    """Workspaces that have granted read access to any of ``workspace_ids``.

    ADR-056 §1.3 cross-workspace grant: a workspace B's
    ``METADATA.search_allowed_workspaces`` lists the workspaces allowed to read
    B. This returns every B where some A in ``workspace_ids`` appears in B's
    grant list -- i.e. the workspaces whose repos are in-scope for a repo that
    lives in ``workspace_ids``. Input workspaces are excluded from the result.

    Reverse of the forward read in ``chat.handler._validate_workspace_access``;
    there is no index for list-membership, so this scans the METADATA rows and
    filters in Python. Cold path (once per repo resolution), paginated.
    """
    wanted = {w for w in workspace_ids if w}
    if not wanted:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    granting: List[str] = []
    scan_kwargs = {
        "FilterExpression": (
            "SK = :sk AND attribute_exists(search_allowed_workspaces)"
        ),
        "ExpressionAttributeValues": {":sk": "METADATA"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            allowed = item.get("search_allowed_workspaces") or []
            if not any(a in wanted for a in allowed):
                continue
            ws_id = item.get("workspace_id") or (
                item.get("PK", "").replace("WS#", "") or None
            )
            if ws_id and ws_id not in wanted and ws_id not in granting:
                granting.append(ws_id)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return granting


def resolve_repo_definition_scope(
    repo_name: str,
    table=None,
    *,
    provider: str = "github",
) -> List[str]:
    """The workspace scope a repo may READ: what its cross-repo references
    RESOLVE against and what its read-scope PR checks retrieve from
    (the REVERSE / consumer-side direction).

    ADR-056 §1: the repo's own workspace(s) + every ``is_global`` workspace +
    every workspace that granted the repo's workspace(s) access ("who can this
    repo read"). This is the scope cross-repo RESOLUTION uses: a consumer binds
    to producers it is allowed to read. It is NOT the scope for finding a
    producer's consumers -- that is the FORWARD direction in
    ``resolve_consumer_query_scope``. A grant is one-directional, so the two
    scopes differ. Sorted, deduped.
    """
    if not repo_name:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    repo_workspaces = find_workspaces_for_repo(repo_name, table, provider=provider)
    combined: Set[str] = set(repo_workspaces)
    combined.update(list_global_workspace_ids(table))
    combined.update(list_workspaces_granting_access(repo_workspaces, table))
    return sorted(combined)


def _forward_grants(
    workspace_ids: Iterable[str],
    table=None,
) -> List[str]:
    """The workspaces that the given workspaces have GRANTED (their own
    ``search_allowed_workspaces``) -- i.e. who may read them.

    The forward direction: for a producer in workspace W, the workspaces W
    granted are where W's consumers may live. Scans METADATA (same cold path as
    the other scope primitives) so it needs no GetItem grant.
    """
    wanted = {w for w in workspace_ids if w}
    if not wanted:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    out: List[str] = []
    scan_kwargs = {
        "FilterExpression": "SK = :sk",
        "ExpressionAttributeValues": {":sk": "METADATA"},
    }
    while True:
        response = table.scan(**scan_kwargs)
        for item in response.get("Items", []):
            if item.get("workspace_id") not in wanted:
                continue
            for granted in item.get("search_allowed_workspaces") or []:
                if granted and granted not in out:
                    out.append(granted)
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key
    return out


def resolve_consumer_query_scope(
    repo_name: str,
    table=None,
    *,
    provider: str = "github",
) -> List[str]:
    """The workspaces where a producer repo's CONSUMERS may live
    (the FORWARD / producer-side direction).

    ADR-056 §5: a consumer in workspace A can import a producer in the repo's
    workspace W iff A can read W -- A is in W's ``search_allowed_workspaces`` or
    W is ``is_global`` (readable by all). So a producer's consumers live in: its
    own workspace(s) + the workspaces those workspace(s) granted (forward) +
    every workspace if any owning workspace is ``is_global`` + (for continuity)
    the ``is_global`` workspaces. BREAKING_CHANGES uses THIS to find
    cross-workspace consumers; the reverse (definition) scope would miss them
    because a grant is one-directional. Sorted, deduped.
    """
    if not repo_name:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    repo_workspaces = find_workspaces_for_repo(repo_name, table, provider=provider)
    combined: Set[str] = set(repo_workspaces)
    global_ids = set(list_global_workspace_ids(table))
    combined.update(global_ids)
    # A global producer is readable by everyone -> consumers may be anywhere.
    if any(w in global_ids for w in repo_workspaces):
        combined.update(list_all_workspace_ids(table))
    # Forward grants: the workspaces this producer's workspace(s) granted.
    combined.update(_forward_grants(repo_workspaces, table))
    return sorted(combined)


def resolve_pr_check_workspace_scope(
    check_type: str,
    repo_name: str,
    table=None,
    *,
    provider: str = "github",
) -> List[str]:
    """Return the workspace_ids a PR check should retrieve from.

    Per-check-type routing:
    - READ_SCOPE_CHECKS (ADR_COMPLIANCE, TEST_COVERAGE, LICENSE_COMPLIANCE,
      ARCHITECTURAL_DUPLICATION): ``resolve_repo_definition_scope``, i.e.
      repo's workspace(s) + all is_global + workspaces that shared with them.
    - BREAKING_CHANGES: ``resolve_consumer_query_scope`` (forward scope).
    - LOCAL_ONLY_CHECKS (README_FRESHNESS): repo's workspace(s) only.
    - Anything unrecognized falls back to LOCAL_ONLY behavior to be
      safe -- broadening on an unknown check_type could leak content
      across scopes.

    Logs the resolved scope once at INFO. Returns a sorted, deduped list.
    """
    if not check_type:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    if check_type in READ_SCOPE_CHECKS:
        # Standards text and duplicate candidates come from whatever the repo's
        # workspace may read: own + is_global + workspaces that granted it
        # search access. Never every workspace in the deployment.
        scope = resolve_repo_definition_scope(repo_name, table, provider=provider)
    elif check_type == "BREAKING_CHANGES":
        # BREAKING_CHANGES finds a changed producer's CONSUMERS, which live in
        # the workspaces that can read the producer (ADR-056 section 5) -- the
        # FORWARD scope: repo's workspace(s) + the workspaces they granted + all
        # workspaces if the producer is global. The read (definition) scope
        # would miss a consumer in a workspace the producer granted, because a
        # grant is one-directional.
        scope = resolve_consumer_query_scope(repo_name, table, provider=provider)
    else:
        # LOCAL_ONLY_CHECKS + fallback for unknown check types.
        scope = sorted(
            set(find_workspaces_for_repo(repo_name, table, provider=provider))
        )

    logger.info(
        "[pr_check_scope] %s scope for %s:%s: %s",
        check_type,
        provider,
        repo_name,
        scope,
    )
    return scope
