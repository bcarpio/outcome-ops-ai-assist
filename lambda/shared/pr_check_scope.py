"""
Per-check-type retrieval scope resolver for PR checks.

Closes docs/tech-debt.md entry "PR-check handlers query the KB
workspace-blind". Different checks want different retrieval scopes:

- Standards checks (ADR_COMPLIANCE / TEST_COVERAGE / LICENSE_COMPLIANCE
  / BREAKING_CHANGES) walk the repo's workspace(s) + all ``is_global``
  workspaces so enterprise / security / compliance standards apply
  cross-portfolio.
- README_FRESHNESS is about *this* repo's README specifically; scoped
  to the repo's workspace(s) only.
- ARCHITECTURAL_DUPLICATION is a cross-portfolio similarity check
  by design; scans every workspace.

Design mirrors ``lambda/chat/handler.py::resolve_chat_workspace_scope``
but the trigger is check_type instead of user membership.
"""

from __future__ import annotations

import logging
import os
from typing import Iterable, List, Optional, Set

import boto3


logger = logging.getLogger(__name__)


# Standards checks whose retrieval scope should include all is_global
# workspaces alongside the repo's own workspace(s).
STANDARDS_CHECKS: Set[str] = {
    "ADR_COMPLIANCE",
    "TEST_COVERAGE",
    "LICENSE_COMPLIANCE",
    "BREAKING_CHANGES",
}

# Local-only checks: scope to the repo's workspace(s) only. README
# freshness is about this repo's README, not org standards.
LOCAL_ONLY_CHECKS: Set[str] = {
    "README_FRESHNESS",
}

# Cross-portfolio checks: scope to every workspace in the deployment.
# Similarity-across-repos is the whole point.
CROSS_PORTFOLIO_CHECKS: Set[str] = {
    "ARCHITECTURAL_DUPLICATION",
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
) -> List[str]:
    """Return every workspace_id that has a GITHUB_REPO row for this repo.

    A repo can be added to multiple workspaces (rare but supported);
    all owning workspaces contribute to the "local" retrieval scope
    for standards checks + README_FRESHNESS.

    Scans the workspaces table filtered on
    ``SK = GITHUB_REPO#{repo_name}``. Not a hot path (once per PR
    check per repo); paginated for correctness.
    """
    if not repo_name:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    workspace_ids: List[str] = []
    scan_kwargs = {
        "FilterExpression": "SK = :sk",
        "ExpressionAttributeValues": {":sk": f"GITHUB_REPO#{repo_name}"},
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
    """Every workspace in the deployment. Used only by
    ARCHITECTURAL_DUPLICATION (cross-portfolio similarity)."""
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
) -> List[str]:
    """The workspace scope a repo's cross-repo references RESOLVE against
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

    repo_workspaces = find_workspaces_for_repo(repo_name, table)
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

    repo_workspaces = find_workspaces_for_repo(repo_name, table)
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
) -> List[str]:
    """Return the workspace_ids a PR check should retrieve from.

    Per-check-type routing:
    - STANDARDS_CHECKS: repo's workspace(s) + all is_global.
    - LOCAL_ONLY_CHECKS (README_FRESHNESS): repo's workspace(s) only.
    - CROSS_PORTFOLIO_CHECKS (ARCHITECTURAL_DUPLICATION): every workspace.
    - Anything unrecognized falls back to LOCAL_ONLY behavior to be
      safe -- broadening on an unknown check_type could leak content
      across scopes.

    Returns a sorted, deduped list.
    """
    if not check_type:
        return []
    if table is None:
        table = _get_dynamodb().Table(_get_workspaces_table_name())

    if check_type in CROSS_PORTFOLIO_CHECKS:
        return sorted(list_all_workspace_ids(table))

    # BREAKING_CHANGES finds a changed producer's CONSUMERS, which live in the
    # workspaces that can read the producer (ADR-056 §5) -- the FORWARD scope:
    # repo's workspace(s) + the workspaces they granted + all workspaces if the
    # producer is global. The reverse (definition) scope would miss a consumer in
    # a workspace the producer granted, because a grant is one-directional. The
    # other standards checks keep repo + is_global (they retrieve standards TEXT,
    # not graph edges; forward grants are a producer->consumer notion, not a
    # whose-ADRs-apply one).
    if check_type == "BREAKING_CHANGES":
        return resolve_consumer_query_scope(repo_name, table)

    repo_workspaces = find_workspaces_for_repo(repo_name, table)

    if check_type in STANDARDS_CHECKS:
        combined: Set[str] = set(repo_workspaces)
        combined.update(list_global_workspace_ids(table))
        return sorted(combined)

    # LOCAL_ONLY_CHECKS + fallback for unknown check types.
    return sorted(set(repo_workspaces))
