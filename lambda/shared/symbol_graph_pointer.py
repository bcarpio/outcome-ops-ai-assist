"""Symbol-graph atomic-swap pointer.

Owns the begin -> commit / abandon protocol that advances the
"current symbol-graph SHA" for a (workspace, repo) pair without ever
leaving the row in an ambiguous state. Two attributes co-exist on the
existing code-maps pointer item:

- ``graph_sha``          : SHA the persisted symbol-graph slice was
                           built against. Readers (chunk 04) anchor
                           every query on this value.
- ``graph_pending_sha``  : SHA in flight while a regen is mid-write.
                           Present iff a swap is currently in progress.
                           Cleared by ``commit_swap`` (success) or
                           ``abandon_swap`` (failure / Lambda timeout).

Why this is separate from ``shared.symbol_graph_writer``: the writer
owns the partition-level delete-then-write. This module owns the
pointer-level state machine. Two concurrent regens for the same
(workspace, repo) can race the writer harmlessly — the SK is a pure
upsert and the loser's items are GC'd on the next regen — but the
pointer must not flip-flop, so ``begin_swap`` uses a conditional
update to detect the concurrent-regen case and reject the loser.

This module reuses the same pointer item key the writer already uses
(``PK = "SYMBOL_GRAPH#${ws}#${repo}", SK = "POINTER"``) so both
modules see one row instead of fighting over two. The writer's
``current_commit_sha`` attribute is preserved and unrelated; chunk 03
adds ``graph_sha`` / ``graph_pending_sha`` alongside it.

Test-isolation hooks (``_set_resource_for_tests`` /
``_reset_clients_for_tests``) mirror the writer pattern in
``symbol_graph_writer.py`` to defend against the cross-suite
sys.modules contamination documented there.
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

import boto3
from botocore.exceptions import ClientError

from shared import dual_write

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pointer item key (must match shared.symbol_graph_writer)
# ---------------------------------------------------------------------------

POINTER_PK_PREFIX = "SYMBOL_GRAPH#"
POINTER_SK = "POINTER"


def _pointer_key(workspace_id: str, repo: str) -> dict:
    return {
        "PK": f"{POINTER_PK_PREFIX}{workspace_id}#{repo}",
        "SK": POINTER_SK,
    }


# ---------------------------------------------------------------------------
# Lazy clients (one per container) - same pattern as symbol_graph_writer.
# ---------------------------------------------------------------------------

_dynamodb_resource = None
_test_resource_override = None


def _resource():
    """Return a cached DynamoDB resource.

    Tests can override via ``_set_resource_for_tests`` to bypass the
    lazy ``boto3.resource`` call entirely. See
    ``symbol_graph_writer._resource`` for the contamination this guards
    against (test_check_handlers_init replaces sys.modules['boto3']
    with a MagicMock, and this module's module-level ``boto3`` import
    can resolve to that mock under cross-suite test ordering).
    """

    global _dynamodb_resource
    if _test_resource_override is not None:
        return _test_resource_override
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def _set_resource_for_tests(resource) -> None:
    """Test hook — inject a DynamoDB resource (typically moto-backed)."""

    global _test_resource_override
    _test_resource_override = resource


def _reset_clients_for_tests() -> None:
    """Test hook — clear cached resource + override."""

    global _dynamodb_resource, _test_resource_override
    _dynamodb_resource = None
    _test_resource_override = None


# ---------------------------------------------------------------------------
# Pointer state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PointerState:
    """Snapshot of the symbol-graph pointer for one (workspace, repo).

    All three fields default to ``None`` on first-time reads (no row, or
    row exists but the attributes are unset). The query layer in chunk
    04 uses ``graph_sha`` as the authoritative anchor and treats a
    non-None ``graph_pending_sha`` as "regen in flight" — neither stale
    nor wrong, fall back to ``graph_sha``.

    ``graph_sha_committed_at`` is the epoch-second timestamp of the most
    recent successful ``commit_swap`` call (i.e. when the current
    ``graph_sha`` became the committed slice). Chunk-04 readers use it
    to compute ``staleness_seconds = now - graph_sha_committed_at``.
    """

    graph_sha: Optional[str]
    graph_pending_sha: Optional[str]
    graph_sha_committed_at: Optional[int] = None


def _table_name(code_maps_table_name: Optional[str]) -> str:
    """Resolve the code-maps table name from arg or ``CODE_MAPS_TABLE`` env."""

    name = code_maps_table_name or os.environ.get("CODE_MAPS_TABLE", "")
    if not name:
        raise ValueError(
            "code_maps_table_name is required (set CODE_MAPS_TABLE env or "
            "pass explicitly)"
        )
    return name


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def read_pointer(
    workspace_id: str,
    repo: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> PointerState:
    """Return the current ``(graph_sha, graph_pending_sha)`` for the pair.

    Both attributes default to ``None`` when the pointer row doesn't
    exist or the attribute is unset — first-ever regen for a
    (workspace, repo) starts from this state.

    Args:
        workspace_id: Workspace scope.
        repo: Repository key.
        code_maps_table_name: Code-maps state-tracking table. Defaults
            to the ``CODE_MAPS_TABLE`` environment variable.

    Returns:
        ``PointerState`` snapshot. Never raises on a missing row.
    """

    if not workspace_id or not repo:
        raise ValueError("workspace_id and repo are required")

    table = _resource().Table(_table_name(code_maps_table_name))
    try:
        response = table.get_item(Key=_pointer_key(workspace_id, repo))
    except ClientError as exc:
        logger.warning(
            "[symbol_graph_pointer] read_pointer failed for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        return PointerState(graph_sha=None, graph_pending_sha=None)

    item = response.get("Item") or {}
    graph_sha = item.get("graph_sha")
    graph_pending_sha = item.get("graph_pending_sha")
    committed_at_raw = item.get("graph_sha_committed_at")
    # DynamoDB returns numerics as Decimal — coerce to int defensively so
    # downstream staleness math (now - committed_at) doesn't trip over
    # mixed types. Reject zero / non-numeric values.
    try:
        committed_at = (
            int(committed_at_raw) if committed_at_raw is not None else None
        )
    except (TypeError, ValueError):
        committed_at = None
    if committed_at is not None and committed_at <= 0:
        committed_at = None
    return PointerState(
        graph_sha=graph_sha if isinstance(graph_sha, str) and graph_sha else None,
        graph_pending_sha=(
            graph_pending_sha
            if isinstance(graph_pending_sha, str) and graph_pending_sha
            else None
        ),
        graph_sha_committed_at=committed_at,
    )


def begin_swap(
    workspace_id: str,
    repo: str,
    new_sha: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> bool:
    """Claim the pointer for a regen against ``new_sha``.

    Sets ``graph_pending_sha = new_sha`` via a conditional update. The
    condition is "no pending swap, OR pending swap is for this same
    SHA" — so idempotent retries by the same regen succeed, but a
    second regen targeting a different SHA fails (preventing two
    concurrent writers from racing the pointer).

    Args:
        workspace_id: Workspace scope.
        repo: Repository key.
        new_sha: SHA the caller is about to write Facts for.

    Returns:
        True if the swap was claimed (caller may proceed to write).
        False if a different concurrent regen owns the pointer.
    """

    if not workspace_id or not repo or not new_sha:
        raise ValueError("workspace_id, repo, and new_sha are all required")

    table = _resource().Table(_table_name(code_maps_table_name))
    try:
        table.update_item(
            Key=_pointer_key(workspace_id, repo),
            UpdateExpression=(
                "SET graph_pending_sha = :new_sha, updated_at = :now"
            ),
            ConditionExpression=(
                "attribute_not_exists(graph_pending_sha) OR "
                "graph_pending_sha = :new_sha"
            ),
            ExpressionAttributeValues={
                ":new_sha": new_sha,
                ":now": int(time.time()),
            },
        )
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            logger.warning(
                "[symbol_graph_pointer] begin_swap rejected for %s/%s "
                "(concurrent regen owns the pointer)",
                workspace_id,
                repo,
            )
            return False
        logger.error(
            "[symbol_graph_pointer] begin_swap failed for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        raise


def commit_swap(
    workspace_id: str,
    repo: str,
    new_sha: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> bool:
    """Advance the pointer from pending to committed.

    Conditional update: succeeds when ``graph_pending_sha`` on the
    row equals ``new_sha`` (this regen owns the pointer). Sets
    ``graph_sha = new_sha`` and removes ``graph_pending_sha``.

    **Idempotent under parallel batches**: ~30 process-batch-summary
    Lambdas run concurrently for one repo regen, each calling
    commit_swap on completion. The first batch's commit clears
    ``graph_pending_sha``; subsequent batches then see "pending !=
    new_sha" and would have rejected. To avoid log-noise + false-
    failure semantics for the late-arriving batches, when the
    conditional fails we check whether ``graph_sha`` is already
    ``new_sha`` — if so, the desired end-state holds and we return
    True (idempotent success). Real failures (pending sha belongs
    to a DIFFERENT regen, or the row is missing entirely) still
    return False.

    Returns:
        True on success (first writer OR idempotent late writer).
        False if the conditional failed AND ``graph_sha`` does not
        already equal ``new_sha`` (real conflict, abandoned regen,
        or another regen took over).
    """

    if not workspace_id or not repo or not new_sha:
        raise ValueError("workspace_id, repo, and new_sha are all required")

    name = _table_name(code_maps_table_name)
    table = _resource().Table(name)
    try:
        # graph_sha_committed_at is set here (not in begin_swap) because
        # "when did this slice become the committed slice" semantically
        # refers to the commit step. Chunk-04 readers use it for
        # staleness_seconds.
        resp = table.update_item(
            Key=_pointer_key(workspace_id, repo),
            UpdateExpression=(
                "SET graph_sha = :new_sha, "
                "graph_sha_committed_at = :now, "
                "updated_at = :now "
                "REMOVE graph_pending_sha"
            ),
            ConditionExpression="graph_pending_sha = :new_sha",
            ExpressionAttributeValues={
                ":new_sha": new_sha,
                ":now": int(time.time()),
            },
            ReturnValues="ALL_NEW",
        )
        # Replicate the committed pointer to the passive region. The local
        # write above is a conditional update_item that dual_write can't
        # express, so we emit the resulting item as an unconditional put;
        # without this the SHA-truth pointer never reaches the passive
        # region and goes stale/missing on the next regen (ADR-022/029).
        # Only the true first-committer reaches here (parallel batches hit
        # the idempotent fallback below), so exactly one emit per regen.
        dual_write.replicate_put_item(name, resp.get("Attributes", {}))
        return True
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ConditionalCheckFailedException":
            # Idempotent fallback: if graph_sha already equals new_sha,
            # an earlier batch (parallel regen) committed first. The
            # caller's intent ("this SHA is current") is satisfied; do
            # nothing more, return True so the caller doesn't escalate
            # to abandon_swap (which would clear a valid pointer state).
            try:
                row = table.get_item(Key=_pointer_key(workspace_id, repo)).get(
                    "Item", {}
                )
                if row.get("graph_sha") == new_sha:
                    logger.debug(
                        "[symbol_graph_pointer] commit_swap idempotent "
                        "no-op for %s/%s (graph_sha already %s)",
                        workspace_id,
                        repo,
                        new_sha,
                    )
                    return True
            except ClientError:
                pass  # fall through to the False return below
            logger.warning(
                "[symbol_graph_pointer] commit_swap rejected for %s/%s "
                "(pending sha does not match %s)",
                workspace_id,
                repo,
                new_sha,
            )
            return False
        logger.error(
            "[symbol_graph_pointer] commit_swap failed for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        raise


def abandon_swap(
    workspace_id: str,
    repo: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> None:
    """Clear ``graph_pending_sha`` without affecting ``graph_sha``.

    Idempotent — safe to call when no swap is pending (e.g. on a
    Lambda timeout cleanup path where we don't know if begin_swap
    even ran). Never raises on a missing row.
    """

    if not workspace_id or not repo:
        raise ValueError("workspace_id and repo are required")

    table = _resource().Table(_table_name(code_maps_table_name))
    try:
        table.update_item(
            Key=_pointer_key(workspace_id, repo),
            UpdateExpression="REMOVE graph_pending_sha SET updated_at = :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as exc:
        logger.warning(
            "[symbol_graph_pointer] abandon_swap non-fatal failure for "
            "%s/%s: %s",
            workspace_id,
            repo,
            exc,
        )


def get_previous_graph_sha(
    workspace_id: str,
    repo: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> Optional[str]:
    """Convenience accessor — the SHA the previous slice was built against.

    Used by the writer's delete-then-write pass to find the old
    partition. Returns ``None`` on a first-ever regen.
    """

    state = read_pointer(
        workspace_id, repo, code_maps_table_name=code_maps_table_name
    )
    return state.graph_sha


__all__ = (
    "PointerState",
    "POINTER_PK_PREFIX",
    "POINTER_SK",
    "read_pointer",
    "begin_swap",
    "commit_swap",
    "abandon_swap",
    "get_previous_graph_sha",
)


# Re-export tuple for callers that want a quick (graph_sha, pending) read.
def read_pointer_tuple(
    workspace_id: str,
    repo: str,
    *,
    code_maps_table_name: Optional[str] = None,
) -> Tuple[Optional[str], Optional[str]]:
    """Same as ``read_pointer`` but returns a plain tuple."""

    state = read_pointer(
        workspace_id, repo, code_maps_table_name=code_maps_table_name
    )
    return state.graph_sha, state.graph_pending_sha
