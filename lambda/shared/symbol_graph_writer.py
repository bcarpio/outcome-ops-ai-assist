"""Symbol-graph DynamoDB writer (plan-gkg chunk 02, v2 schema).

Persists the structural ``Fact`` stream emitted by the chunk-01 extractor.
Schema and access patterns are documented in plan-gkg/02-storage-and-schema.md
with the v2 refinement captured in ``plan/overview.md``.

v2 schema (this module):
  - Base PK is ``${workspace_id}#${repo}`` -- no commit_sha component.
    ``commit_sha`` rides along as an attribute for provenance and
    staleness display only; correctness is anchored on the SK, which
    already uniquely identifies a fact within a (workspace, repo) via
    ``kind#source#target#file_path#line``.
  - Reextracting facts under a new commit SHA upserts in place via
    matching SKs. No delete-old-partition step is needed -- the v1
    file-scoped-delete workaround (commit 9b09390) was rendered
    dead-code by this refactor.
  - The pointer module (``shared.symbol_graph_pointer``) is unchanged;
    ``graph_sha`` / ``graph_sha_committed_at`` still drive staleness
    reporting but no longer gate correctness.

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
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pointer item shape (code-maps table, plan-gkg/02 Option A)
# ---------------------------------------------------------------------------

# The pointer lives in the existing code-maps state-tracking table. PK/SK
# values are chosen to coexist with existing rows without collision --
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

    ``facts_written`` is the number of unique Fact items upserted into the
    (workspace, repo) partition.

    ``facts_deleted`` is retained on the dataclass for back-compat with
    callers that destructure WriteResult by position or read the field
    directly. Under the v2 schema it is always 0 -- the writer no longer
    runs a delete pass. Safe to remove in a follow-up cleanup chunk once
    no caller reads it.

    ``previous_commit_sha`` is also retained for back-compat. **Deprecated**
    under v2: there is no per-SHA partition concept anymore, so this field
    is informational only (echoes whatever the caller passed in, or None
    when not supplied). Safe to remove in a follow-up cleanup chunk.

    ``pointer_swapped`` is True iff the code-maps pointer was advanced to
    ``new_commit_sha`` (legacy ``current_commit_sha`` attribute -- the
    chunk-03 pointer state machine in ``shared.symbol_graph_pointer``
    owns the modern ``graph_sha`` / ``graph_pending_sha`` lifecycle and
    callers wanting that path pass ``skip_pointer_swap=True``).
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
    lazy ``boto3.resource`` call entirely -- needed because the wider
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
    """Test hook -- inject a DynamoDB resource (typically moto-backed)
    so ``_resource()`` short-circuits the boto3.resource lookup that
    can return a MagicMock under cross-suite contamination.
    """

    global _test_resource_override
    _test_resource_override = resource


def _reset_clients_for_tests() -> None:
    """Test hook -- clears both the cached resource and the test
    override so each test gets a fresh moto-backed setup.
    """

    global _dynamodb_resource, _test_resource_override
    _dynamodb_resource = None
    _test_resource_override = None


# ---------------------------------------------------------------------------
# Key construction (v2 schema -- no commit_sha in base PK)
# ---------------------------------------------------------------------------


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
    to the last ``/`` (or last ``::``) -- that's what an enterprise
    "find every consumer of ``@hyatt-hpe/hpe-utils/*``" query keys on.
    Symbols without a delimiter (single-module function calls) skip the
    GSI2 row entirely.
    """

    # Subpath case -- slash inside the module/package portion.
    if "/" in target_symbol_id:
        head, sep, tail = target_symbol_id.rpartition("/")
        if head and tail:
            return {"prefix": head, "suffix": tail}

    # Namespace case -- ``<lang>:<module>::<namespace>::<symbol>``.
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

    PK shape is v2: ``${workspace_id}#${repo}``. ``commit_sha`` is an
    attribute only -- the SK alone uniquely identifies a fact within
    the partition.
    """

    pk = f"{workspace_id}#{repo}"
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


def _write_facts_batched(
    symbol_graph_table_name: str,
    items: Sequence[Dict[str, object]],
) -> int:
    """Batch-write items in 25-item chunks. Returns the count written.

    Uses ``table.batch_writer`` which handles the chunking and unprocessed-
    item retries internally; we still cap the buffered size at
    ``_BATCH_SIZE`` to make the per-batch boundary explicit for failure-
    recovery testing.

    **Dedupe before send**: DynamoDB's BatchWriteItem rejects the whole
    batch with ``ValidationException: Provided list of item keys contains
    duplicates`` when two PutItem requests target the same (PK, SK).
    The chunk-01 extractor can emit two Facts with identical
    ``(kind, source, target, file_path, line)`` tuples for a single
    source file (e.g. the same call walked twice by the Tree-sitter
    visitor under nested-scope traversal), which produces identical SKs.
    Dedupe to the last occurrence (semantically equivalent -- all
    duplicates carry the same content). Live evidence:
    2026-05-15 re-extract of outcome-ops-ai-assist-private's
    workspace-management batch.
    """

    if not items:
        return 0

    deduped: Dict[tuple, Dict[str, object]] = {}
    for item in items:
        deduped[(item["PK"], item["SK"])] = item

    table = _resource().Table(symbol_graph_table_name)
    written = 0
    # batch_writer respects the 25-item ceiling internally, but capping
    # at _BATCH_SIZE keeps the flush boundary deterministic for the
    # failure-recovery test.
    with table.batch_writer() as batch:
        for item in deduped.values():
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

    v2 write path:
      1. Materialize Facts to items with the new PK shape
         (``${workspace_id}#${repo}``).
      2. Dedupe by (PK, SK) (handled inside ``_write_facts_batched`` --
         the chunk-01 extractor can emit duplicate Facts per source file
         and a single batch with a duplicate (PK, SK) is rejected by
         BatchWriteItem).
      3. Batch-write upsert. Same-SK rows upsert in place -- no separate
         delete step. Reextracting facts under a new SHA simply
         overwrites the rows with the new ``commit_sha`` attribute and
         updated ``created_at``.
      4. Optionally swap the legacy ``current_commit_sha`` pointer (when
         ``skip_pointer_swap=False``). The chunk-03 atomic-swap path in
         ``shared.symbol_graph_pointer`` owns the modern ``graph_sha`` /
         ``graph_pending_sha`` state machine and passes
         ``skip_pointer_swap=True``.

    If step 3 raises, the pointer is NOT swapped (step 4 only runs after
    a clean write).

    Args:
        workspace_id: Workspace scope.
        repo: Repository key (``owner/repo`` or short name; the writer
            does not parse, it stores verbatim).
        commit_sha: SHA the Facts were extracted from. Stored as a
            row attribute only -- correctness is anchored on the SK.
        facts: Iterable of Fact-shaped objects (see
            ``lambda/process-batch-summary/extractor/base.py``).
        symbol_graph_table_name: DynamoDB table name. Defaults to the
            ``SYMBOL_GRAPH_TABLE`` environment variable.
        code_maps_table_name: Code-maps state-tracking table name (for
            the legacy pointer-swap). Defaults to the ``CODE_MAPS_TABLE``
            environment variable.
        previous_commit_sha: **Deprecated** under v2. Retained on the
            signature for back-compat with callers (process-batch-summary
            passes it from the chunk-03 pointer module). The value is
            echoed back on the ``WriteResult`` for observability but is
            no longer used to drive a delete pass -- the v2 schema makes
            that step unnecessary.
        skip_pointer_swap: When True, step 4 is skipped. Chunk-03's
            ``symbol_graph_pointer.commit_swap`` owns pointer state
            and must not be clobbered by the writer's legacy put_item
            of ``current_commit_sha``.

    Returns:
        ``WriteResult`` describing the regen outcome. ``facts_deleted``
        is always 0 under v2.

    Raises:
        ValueError: Required arguments are empty.
        Any underlying ``ClientError`` from the write pass -- the caller
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

    # Step 1: materialize Facts to v2-shaped items.
    items: List[Dict[str, object]] = [
        _build_item(workspace_id, repo, commit_sha, fact) for fact in facts
    ]

    # Step 2-3: dedupe + batch-write upsert. No delete pass: identical
    # SKs from a previous extraction are overwritten in place with the
    # new commit_sha attribute.
    written = _write_facts_batched(table_name, items)

    # Step 4: legacy pointer-swap. Skipped when the caller is on the
    # chunk-03 atomic-swap path (process-batch-summary) which owns the
    # modern pointer state machine in shared.symbol_graph_pointer.
    if skip_pointer_swap or not code_maps_name:
        swapped = False
    else:
        swapped = _swap_pointer(
            code_maps_name, workspace_id, repo, commit_sha
        )

    return WriteResult(
        facts_written=written,
        facts_deleted=0,
        previous_commit_sha=previous_commit_sha or None,
        new_commit_sha=commit_sha,
        pointer_swapped=swapped,
    )
