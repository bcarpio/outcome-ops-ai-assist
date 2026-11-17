# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Repo-deletion completeness verifier (delete-bug 04).

Checks whether a removed repo's data is actually gone from a region's
stores. Used in two places:

- the delete-worker self-verifies its own region after cleaning (a
  loud log line if anything survived — catches delete regressions), and
- the one-shot backfill sweep (delete-bug 05) and any on-demand
  attestation reuse ``check_repo_clean`` to enumerate leaked data.

Region-scoped: pass the clients/table/bucket names for the region you
want to check (the delete-worker passes its own region's; a
cross-region attestation calls it once per region). s3vectors emptiness
is intentionally NOT checked here — verifying the vector index by prefix
is a separate, fiddlier follow-up; the high-value stores (symbol-graph
facts, code-maps pointer, KB objects) are where the leaks were.
"""
from __future__ import annotations

from typing import Any, Dict

from boto3.dynamodb.conditions import Key


def count_symbol_graph_facts(table, workspace_id: str, repo_bare: str) -> int:
    """Number of symbol-graph facts remaining for a repo (PK = ws#repo)."""
    total = 0
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(f"{workspace_id}#{repo_bare}"),
        "Select": "COUNT",
    }
    while True:
        resp = table.query(**kwargs)
        total += resp.get("Count", 0)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return total
        kwargs["ExclusiveStartKey"] = lek


def pointer_present(table, workspace_id: str, repo_bare: str) -> bool:
    """True if the code-maps SHA-truth pointer row still exists."""
    resp = table.get_item(
        Key={"PK": f"SYMBOL_GRAPH#{workspace_id}#{repo_bare}", "SK": "POINTER"}
    )
    return "Item" in resp


def count_partition_rows(table, pk: str) -> int:
    """Total rows under one DynamoDB partition (paginated COUNT)."""
    total = 0
    kwargs: Dict[str, Any] = {
        "KeyConditionExpression": Key("PK").eq(pk),
        "Select": "COUNT",
    }
    while True:
        resp = table.query(**kwargs)
        total += resp.get("Count", 0)
        lek = resp.get("LastEvaluatedKey")
        if not lek:
            return total
        kwargs["ExclusiveStartKey"] = lek


def count_s3_objects(s3_client, bucket: str, prefix: str) -> int:
    """Number of S3 objects under a prefix (paginated)."""
    total, token = 0, None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        page = s3_client.list_objects_v2(**kwargs)
        total += page.get("KeyCount", 0)
        token = page.get("NextContinuationToken")
        if not token:
            return total


def check_repo_clean(
    *,
    workspace_id: str,
    repo_bare: str,
    repo_name: str,
    dynamodb,
    s3_client,
    symbol_graph_table: str = "",
    code_maps_table: str = "",
    kb_bucket: str = "",
    pending_work_table: str = "",
) -> Dict[str, Any]:
    """Region-scoped completeness check for a removed repo.

    ``repo_name`` is the owner-prefixed name ("owner/repo"); ``repo_bare``
    is the bare name used by the symbol graph. Returns
    ``{"clean": bool, "stores": {...counts/flags...}}``. A store whose
    table/bucket name is empty is reported as 0/absent (skipped), so a
    caller can check only the stores it knows about.
    """
    sg = (
        count_symbol_graph_facts(dynamodb.Table(symbol_graph_table), workspace_id, repo_bare)
        if symbol_graph_table
        else 0
    )
    ptr = (
        pointer_present(dynamodb.Table(code_maps_table), workspace_id, repo_bare)
        if code_maps_table
        else False
    )
    state_rows = (
        count_partition_rows(dynamodb.Table(code_maps_table), f"repo#{repo_bare}")
        if code_maps_table
        else 0
    )
    kb_owner = (
        count_s3_objects(s3_client, kb_bucket, f"workspaces/{workspace_id}/github/{repo_name}/")
        if kb_bucket
        else 0
    )
    kb_bare = (
        count_s3_objects(s3_client, kb_bucket, f"workspaces/{workspace_id}/github/{repo_bare}/")
        if kb_bucket and repo_bare != repo_name
        else 0
    )
    pending = (
        count_partition_rows(
            dynamodb.Table(pending_work_table),
            f"WS#{workspace_id}#GITHUB_REPO#{repo_name}",
        )
        if pending_work_table
        else 0
    )
    stores = {
        "symbol_graph_facts": sg,
        "code_maps_pointer": ptr,
        "code_maps_state_rows": state_rows,
        "kb_owner_objects": kb_owner,
        "kb_bare_objects": kb_bare,
        "pending_work_rows": pending,
    }
    clean = (
        sg == 0
        and not ptr
        and state_rows == 0
        and kb_owner == 0
        and kb_bare == 0
        and pending == 0
    )
    return {"clean": clean, "stores": stores}
