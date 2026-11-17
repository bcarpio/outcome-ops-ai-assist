# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Audit GSI2 partition sharding (fable-scale chunk 04).

Every Bedrock-related audit row org-wide was writing `GSI2PK = "CAT#bedrock"`
(single string). One DDB partition = ~1000 WCU physical ceiling, absorbing
every chat turn, rerank, and MCP call. Throttles at a few hundred writes/sec
under a 2000-engineer chat + MCP fleet. Same class of problem holds for
`CAT#admin` under org-scale admin activity.

Fix: append a hash-derived shard suffix to `GSI2PK` on write, and fan out
across every shard on read. `AUDIT_GSI2_SHARDS` (default 16) sets the shard
count. Reads also cover the legacy unsharded key so pre-cutover rows stay
visible until the 365-day TTL window elapses.

Env kill switch (`AUDIT_SHARD_ENABLED=false`) returns the writer to the
pre-shard shape for rollback; reader always fans out over the legacy key
regardless, so this is safe to toggle.
"""
from __future__ import annotations

import hashlib
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

AUDIT_GSI2_SHARDS = int(os.environ.get("AUDIT_GSI2_SHARDS", "16"))
AUDIT_SHARD_ENABLED = os.environ.get("AUDIT_SHARD_ENABLED", "true").lower() == "true"


def gsi2_shard_suffix(row_id: str) -> str:
    """Deterministic shard suffix (e.g. "#07") for the given row identity.

    Same `row_id` -> same shard, so an idempotent retry lands on the same
    partition. `row_id` is expected to be the audit row's `request_id`
    (already random 16-hex from `secrets.token_hex(8)`); hashing gives us
    a stable mapping in case the identity source changes.

    When sharding is disabled, returns "" so `gsi2pk_for_category` yields
    the pre-shard legacy shape.
    """
    if not AUDIT_SHARD_ENABLED or AUDIT_GSI2_SHARDS <= 1:
        return ""
    digest = hashlib.blake2b(row_id.encode(), digest_size=1).digest()[0]
    return f"#{digest % AUDIT_GSI2_SHARDS:02d}"


def gsi2pk_for_category(category: str, row_id: str) -> str:
    """Build the `GSI2PK` value for a fresh audit write.

    Sharded shape: `CAT#<category>#NN`. Legacy: `CAT#<category>`.
    """
    return f"CAT#{category}{gsi2_shard_suffix(row_id)}"


def gsi2pk_shards_for_read(category: str, include_legacy: bool = True) -> List[str]:
    """Enumerate every GSI2PK partition value the reader must Query for the
    given category.

    Post-cutover this returns the sharded set (`CAT#<cat>#00`..`#15`) plus
    the legacy unsharded key so rows written before the cutover stay
    visible for the 365-day TTL window.
    """
    shards = [f"CAT#{category}#{i:02d}" for i in range(AUDIT_GSI2_SHARDS)]
    if include_legacy:
        shards.append(f"CAT#{category}")
    return shards


def read_by_category(
    table,
    category: str,
    sk_start: str,
    sk_end: str,
    limit: int,
    filter_expression: Optional[str] = None,
    expression_attribute_values: Optional[Dict[str, Any]] = None,
    expression_attribute_names: Optional[Dict[str, str]] = None,
    cursor_sk: Optional[str] = None,
    filter_page_limit: int = 500,
    filter_max_pages: int = 5,
) -> Tuple[List[Dict], Optional[str]]:
    """Fan-out Query across every GSI2 shard for `category`, merged desc by SK.

    Runs `len(shards)` parallel Queries (16 shards + legacy = 17 by default).
    Merges + sorts descending by `GSI2SK`, returns the top `limit`.

    Cursor: "high water mark" — `cursor_sk` is the smallest `GSI2SK` returned
    on the prior page. Next-page Queries use `sk_end = cursor_sk` and the
    boundary row is filtered client-side to avoid duplication.

    Sparse-filter pagination: when `filter_expression` is set, each shard
    walks up to `filter_max_pages` pages of `filter_page_limit` rows,
    following LastEvaluatedKey, until it accumulates `limit` matches or hits
    the safety cap. This mirrors the pre-shard reader's behavior (DDB
    applies Limit BEFORE FilterExpression, so a sparse filter like
    status=refusal against a chat-dominated partition returns 0 unless the
    reader keeps walking). Without a filter, each shard runs one Query with
    `Limit=max(limit, 100)`.
    """
    shards = gsi2pk_shards_for_read(category)
    effective_sk_end = cursor_sk if cursor_sk else sk_end

    def _query_one(gsi2pk: str) -> List[Dict]:
        base_expr_vals = {
            ":pk": gsi2pk,
            ":s": sk_start,
            ":e": effective_sk_end,
        }
        if expression_attribute_values:
            base_expr_vals.update(expression_attribute_values)
        base_kwargs: Dict[str, Any] = {
            "IndexName": "GSI2",
            "KeyConditionExpression": "GSI2PK = :pk AND GSI2SK BETWEEN :s AND :e",
            "ExpressionAttributeValues": base_expr_vals,
            "ScanIndexForward": False,
        }
        if filter_expression:
            base_kwargs["FilterExpression"] = filter_expression
        if expression_attribute_names:
            base_kwargs["ExpressionAttributeNames"] = dict(expression_attribute_names)

        try:
            if filter_expression:
                # Sparse-filter pagination: walk pages until we have `limit`
                # matches or hit the safety cap. Each page pre-filter is
                # filter_page_limit rows deep; matches come back post-filter.
                collected: List[Dict] = []
                last_key = None
                for _ in range(filter_max_pages):
                    page_kwargs = dict(base_kwargs)
                    page_kwargs["Limit"] = filter_page_limit
                    if last_key:
                        page_kwargs["ExclusiveStartKey"] = last_key
                    resp = table.query(**page_kwargs)
                    collected.extend(resp.get("Items", []) or [])
                    last_key = resp.get("LastEvaluatedKey")
                    if len(collected) >= limit or not last_key:
                        break
                return collected[:limit]
            else:
                base_kwargs["Limit"] = max(limit, 100)
                resp = table.query(**base_kwargs)
                return resp.get("Items", []) or []
        except Exception as e:
            logger.warning("audit shard Query failed on %s: %s", gsi2pk, e)
            return []

    # 17 shards is small; ThreadPoolExecutor overhead is minimal, IO-bound.
    with ThreadPoolExecutor(max_workers=len(shards)) as ex:
        results = list(ex.map(_query_one, shards))

    items: List[Dict] = [x for sub in results for x in sub]
    if cursor_sk:
        # Drop the boundary row from the prior page.
        items = [x for x in items if x.get("GSI2SK", "") != cursor_sk]
    items.sort(key=lambda x: x.get("GSI2SK", ""), reverse=True)
    trimmed = items[:limit]

    next_cursor_sk: Optional[str] = None
    # More items exist below the last one we returned — pass a cursor.
    if len(items) > limit and trimmed:
        next_cursor_sk = trimmed[-1].get("GSI2SK", "") or None

    return trimmed, next_cursor_sk
