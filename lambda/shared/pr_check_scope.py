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

    repo_workspaces = find_workspaces_for_repo(repo_name, table)

    if check_type in STANDARDS_CHECKS:
        combined: Set[str] = set(repo_workspaces)
        combined.update(list_global_workspace_ids(table))
        return sorted(combined)

    # LOCAL_ONLY_CHECKS + fallback for unknown check types.
    return sorted(set(repo_workspaces))
