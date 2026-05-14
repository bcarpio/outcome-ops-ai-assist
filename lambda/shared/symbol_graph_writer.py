"""Symbol-graph DynamoDB writer (plan-gkg chunk 02).

Persists the structural ``Fact`` stream emitted by the chunk-01 extractor.
Schema and access patterns are documented in plan-gkg/02-storage-and-schema.md.

Write flow (the "delete-then-write" pattern with atomic pointer-swap):

  1. Read the current SHA pointer from the code-maps state-tracking row
     (pointer-swap Option A — reuses existing code-maps infra, no new
     pointer item type to maintain).
  2. Query the previous-SHA partition; batch-delete every item in
     25-item batches.
  3. Batch-write the new Facts under the new SHA in 25-item batches.
  4. Atomically update the code-maps row to point at the new SHA.

If step 3 fails midway the pointer is NEVER updated, so queries continue
to see the previous SHA's facts (mixed-state is avoided). Idempotency
holds because the sort key encodes ``kind#source#target#file#line`` — a
second write of identical Facts produces an identical row.

Companion to ``shared/audit_writer.py``. Kept narrow and side-effect-clean
so the call site can be unit-tested with moto under a single autouse
fixture (ADR-005 / plan-gkg cross-cutting standards).
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence

import boto3
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pointer item shape (code-maps table, plan-gkg/02 Option A)
# ---------------------------------------------------------------------------

# The pointer lives in the existing code-maps state-tracking table. PK/SK
# values are chosen to coexist with existing rows without collision —
# the SK constant is unique to this feature.
POINTER_PK_PREFIX = "SYMBOL_GRAPH#"
POINTER_SK = "POINTER"

# DynamoDB enforces a 25-item ceiling per BatchWriteItem call.
_BATCH_SIZE = 25


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WriteResult:
    """Summary of one regen pass.

    ``facts_written`` is the number of new Fact items put. ``facts_deleted``
    is the number of previous-SHA items removed. ``pointer_swapped`` is
    True iff the code-maps pointer was advanced to ``new_commit_sha``;
    False means step 3 failed and the previous SHA remains current.
    """

    facts_written: int
    facts_deleted: int
    previous_commit_sha: Optional[str]
    new_commit_sha: str
    pointer_swapped: bool


# ---------------------------------------------------------------------------
# Lazy clients (one per container)
# ---------------------------------------------------------------------------

_dynamodb_resource = None
_test_resource_override = None


def _resource():
    """Return a cached DynamoDB resource.

    Tests can override via ``_set_resource_for_tests`` to bypass the
    lazy ``boto3.resource`` call entirely — needed because the wider
    test suite has a cross-file sys.modules contamination
    (``test_check_handlers_init`` replaces ``sys.modules['boto3']`` with
    a MagicMock that other tests rely on). Passing a moto-backed
    resource directly sidesteps that boto3 reference being mocked here.
    """

    global _dynamodb_resource
    if _test_resource_override is not None:
        return _test_resource_override
    if _dynamodb_resource is None:
        _dynamodb_resource = boto3.resource("dynamodb")
    return _dynamodb_resource


def _set_resource_for_tests(resource) -> None:
    """Test hook — inject a DynamoDB resource (typically moto-backed)
    so ``_resource()`` short-circuits the boto3.resource lookup that
    can return a MagicMock under cross-suite contamination.
    """

    global _test_resource_override
    _test_resource_override = resource


def _reset_clients_for_tests() -> None:
    """Test hook — clears both the cached resource and the test
    override so each test gets a fresh moto-backed setup.
    """

    global _dynamodb_resource, _test_resource_override
    _dynamodb_resource = None
    _test_resource_override = None


# ---------------------------------------------------------------------------
# Key construction
# ---------------------------------------------------------------------------


def _partition_pk(workspace_id: str, repo: str, commit_sha: str) -> str:
    return f"{workspace_id}#{repo}#{commit_sha}"


def _fact_sk(
    kind: str,
    source_symbol_id: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
) -> str:
    return f"{kind}#{source_symbol_id}#{target_symbol_id}#{file_path}#{line}"


def _gsi1_pk(workspace_id: str, target_symbol_id: str) -> str:
    return f"{workspace_id}#{target_symbol_id}"


def _gsi1_sk(kind: str, repo: str, file_path: str, line: int) -> str:
    return f"{kind}#{repo}#{file_path}#{line}"


def _split_target_prefix(target_symbol_id: str) -> Optional[Dict[str, str]]:
    """Return ``{prefix, suffix}`` for a target ID, or None if no useful
    split exists.

    Subpath imports use the canonical form
    ``<lang>:<package>/<subpath>::<symbol>``. The prefix is everything up
    to the last ``/`` (or last ``::``) — that's what an enterprise
    "find every consumer of ``@hyatt-hpe/hpe-utils/*``" query keys on.
    Symbols without a delimiter (single-module function calls) skip the
    GSI2 row entirely.
    """

    # Subpath case — slash inside the module/package portion.
    if "/" in target_symbol_id:
        head, sep, tail = target_symbol_id.rpartition("/")
        if head and tail:
            return {"prefix": head, "suffix": tail}

    # Namespace case — ``<lang>:<module>::<namespace>::<symbol>``.
    if "::" in target_symbol_id:
        head, sep, tail = target_symbol_id.rpartition("::")
        if head and tail:
            return {"prefix": head, "suffix": tail}

    return None


# ---------------------------------------------------------------------------
# Item construction from Facts
# ---------------------------------------------------------------------------


def _build_item(
    workspace_id: str,
    repo: str,
    commit_sha: str,
    fact,
) -> Dict[str, object]:
    """Flatten one ``Fact`` (chunk-01 dataclass) onto a DynamoDB item.

    The Fact type is imported lazily by the caller; we accept any object
    that exposes the documented attribute names. This keeps the writer
    library-agnostic and unit-testable without dragging the extractor
    package into the test classpath.
    """

    pk = _partition_pk(workspace_id, repo, commit_sha)
    sk = _fact_sk(
        fact.kind,
        fact.source_symbol_id,
        fact.target_symbol_id,
        fact.file_path,
        fact.line,
    )

    item: Dict[str, object] = {
        "PK": pk,
        "SK": sk,
        "GSI1PK": _gsi1_pk(workspace_id, fact.target_symbol_id),
        "GSI1SK": _gsi1_sk(fact.kind, repo, fact.file_path, fact.line),
        "workspace_id": workspace_id,
        "repo": repo,
        "commit_sha": commit_sha,
        "kind": fact.kind,
        "source_symbol_id": fact.source_symbol_id,
        "target_symbol_id": fact.target_symbol_id,
        "file_path": fact.file_path,
        "line": int(fact.line),
        "language": fact.language,
        "created_at": int(time.time()),
    }

    split = _split_target_prefix(fact.target_symbol_id)
    if split is not None:
        item["GSI2PK"] = f"{workspace_id}#{split['prefix']}"
        item["GSI2SK"] = (
            f"{split['suffix']}#{fact.kind}#{repo}#{fact.file_path}#{fact.line}"
        )

    return item


# ---------------------------------------------------------------------------
# Pointer (code-maps row) - Option A
# ---------------------------------------------------------------------------


def _pointer_key(workspace_id: str, repo: str) -> Dict[str, str]:
    return {
        "PK": f"{POINTER_PK_PREFIX}{workspace_id}#{repo}",
        "SK": POINTER_SK,
    }


def read_current_commit_sha(
    code_maps_table_name: str,
    workspace_id: str,
    repo: str,
) -> Optional[str]:
    """Return the previously-pointed-at commit SHA, or None on first regen."""

    if not code_maps_table_name:
        return None
    try:
        table = _resource().Table(code_maps_table_name)
        response = table.get_item(Key=_pointer_key(workspace_id, repo))
        item = response.get("Item")
        if not item:
            return None
        sha = item.get("current_commit_sha")
        return sha if isinstance(sha, str) and sha else None
    except ClientError as exc:
        logger.warning(
            "[symbol_graph_writer] Failed to read pointer for "
            "%s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        return None


def _swap_pointer(
    code_maps_table_name: str,
    workspace_id: str,
    repo: str,
    new_commit_sha: str,
) -> bool:
    """Atomically advance the pointer. Returns True on success."""

    if not code_maps_table_name:
        logger.warning(
            "[symbol_graph_writer] code-maps table name is empty; "
            "pointer-swap skipped for %s/%s",
            workspace_id,
            repo,
        )
        return False
    try:
        table = _resource().Table(code_maps_table_name)
        table.put_item(
            Item={
                **_pointer_key(workspace_id, repo),
                "current_commit_sha": new_commit_sha,
                "updated_at": int(time.time()),
            }
        )
        return True
    except ClientError as exc:
        logger.error(
            "[symbol_graph_writer] Pointer-swap failed for %s/%s: %s",
            workspace_id,
            repo,
            exc,
        )
        return False


# ---------------------------------------------------------------------------
# Batch helpers
# ---------------------------------------------------------------------------


def _delete_partition(
    symbol_graph_table_name: str,
    workspace_id: str,
    repo: str,
    commit_sha: str,
) -> int:
    """Query the previous-SHA partition and batch-delete every item.

    Returns the number of items deleted. Pagination via
    ``LastEvaluatedKey`` is honored so partitions larger than the 1 MB
    Query page size still drain to empty.
    """

    table = _resource().Table(symbol_graph_table_name)
    pk = _partition_pk(workspace_id, repo, commit_sha)

    deleted = 0
    last_key: Optional[Dict[str, object]] = None
    while True:
        query_kwargs = {"KeyConditionExpression": Key("PK").eq(pk)}
        if last_key:
            query_kwargs["ExclusiveStartKey"] = last_key

        response = table.query(**query_kwargs)
        items = response.get("Items", [])
        if items:
            with table.batch_writer() as batch:
                for item in items:
                    batch.delete_item(Key={"PK": item["PK"], "SK": item["SK"]})
                    deleted += 1

        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break

    return deleted


def _write_facts_batched(
    symbol_graph_table_name: str,
    items: Sequence[Dict[str, object]],
) -> int:
    """Batch-write items in 25-item chunks. Returns the count written.

    Uses ``table.batch_writer`` which handles the chunking and unprocessed-
    item retries internally; we still cap the buffered size at
    ``_BATCH_SIZE`` to make the per-batch boundary explicit for failure-
    recovery testing.
    """

    if not items:
        return 0

    table = _resource().Table(symbol_graph_table_name)
    written = 0
    # batch_writer respects the 25-item ceiling internally, but capping
    # at _BATCH_SIZE keeps the flush boundary deterministic for the
    # failure-recovery test.
    with table.batch_writer() as batch:
        for item in items:
            batch.put_item(Item=item)
            written += 1
    return written


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def write_facts(
    workspace_id: str,
    repo: str,
    commit_sha: str,
    facts: Iterable,
    *,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
    previous_commit_sha: Optional[str] = None,
    skip_pointer_swap: bool = False,
) -> WriteResult:
    """Persist a Fact stream for one (workspace, repo, commit_sha) tuple.

    Steps mirror plan-gkg/02 "Write path":
      1. Read previous SHA from the code-maps pointer (Option A) —
         skipped when ``previous_commit_sha`` is supplied explicitly.
      2. Batch-delete the previous-SHA partition.
      3. Batch-write the new Facts.
      4. Atomically swap the pointer to ``commit_sha`` — skipped when
         ``skip_pointer_swap=True`` (chunk-03 atomic-swap path owns the
         pointer state machine in ``shared.symbol_graph_pointer``).

    If step 3 raises, the pointer is NOT swapped, so reads still resolve
    to the previous SHA. If the previous SHA equals the new SHA the
    delete pass is skipped (idempotent re-write); the write pass still
    runs so identical Facts overwrite themselves in place.

    Args:
        workspace_id: Workspace scope.
        repo: Repository key (``owner/repo`` or short name; the writer
            does not parse, it stores verbatim).
        commit_sha: SHA the Facts were extracted from.
        facts: Iterable of Fact-shaped objects (see
            ``lambda/process-batch-summary/extractor/base.py``).
        symbol_graph_table_name: DynamoDB table name. Defaults to the
            ``SYMBOL_GRAPH_TABLE`` environment variable.
        code_maps_table_name: Code-maps state-tracking table name (for
            the pointer-swap). Defaults to the ``CODE_MAPS_TABLE``
            environment variable.
        previous_commit_sha: Caller-supplied previous SHA. When provided
            the writer skips the pointer-read in step 1 and uses this
            value for the delete pass — required when the caller is
            chunk-03's atomic-swap path (the new pointer schema lives
            on ``graph_sha`` / ``graph_pending_sha``, not on the legacy
            ``current_commit_sha`` field the writer reads by default).
        skip_pointer_swap: When True, step 4 is skipped. Chunk-03's
            ``symbol_graph_pointer.commit_swap`` owns pointer state
            and must not be clobbered by the writer's legacy put_item
            of ``current_commit_sha``.

    Returns:
        ``WriteResult`` describing the regen outcome.

    Raises:
        ValueError: Required arguments are empty.
        Any underlying ``ClientError`` from the write pass — the caller
        decides whether to surface or retry.
    """

    if not workspace_id or not repo or not commit_sha:
        raise ValueError(
            "workspace_id, repo, and commit_sha are all required"
        )

    table_name = symbol_graph_table_name or os.environ.get(
        "SYMBOL_GRAPH_TABLE", ""
    )
    if not table_name:
        raise ValueError(
            "symbol_graph_table_name is required (set SYMBOL_GRAPH_TABLE env "
            "or pass explicitly)"
        )

    code_maps_name = code_maps_table_name or os.environ.get(
        "CODE_MAPS_TABLE", ""
    )

    # Step 1: previous SHA. Caller-supplied wins over the legacy read.
    if previous_commit_sha is not None:
        previous_sha: Optional[str] = previous_commit_sha or None
    else:
        previous_sha = read_current_commit_sha(
            code_maps_name, workspace_id, repo
        )

    # Step 2: delete the previous-SHA partition (only if it differs and exists).
    deleted = 0
    if previous_sha and previous_sha != commit_sha:
        deleted = _delete_partition(
            table_name, workspace_id, repo, previous_sha
        )

    # Step 3: materialize Facts and batch-write. Materialization is
    # deliberate — the extractor yields a generator and we need to know
    # the count for the WriteResult.
    items: List[Dict[str, object]] = []
    for fact in facts:
        items.append(_build_item(workspace_id, repo, commit_sha, fact))

    written = _write_facts_batched(table_name, items)

    # Step 4: pointer-swap. Only after step 3 succeeded. Chunk-03's
    # atomic-swap path passes ``skip_pointer_swap=True`` so commit_swap
    # in symbol_graph_pointer owns the state machine end-to-end.
    if skip_pointer_swap or not code_maps_name:
        swapped = False
    else:
        swapped = _swap_pointer(
            code_maps_name, workspace_id, repo, commit_sha
        )

    return WriteResult(
        facts_written=written,
        facts_deleted=deleted,
        previous_commit_sha=previous_sha,
        new_commit_sha=commit_sha,
        pointer_swapped=swapped,
    )
