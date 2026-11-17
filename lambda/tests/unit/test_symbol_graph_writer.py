# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.symbol_graph_writer`` (v2 schema).

Covers the v2 write path (SK-keyed upsert into a single (workspace, repo)
partition), the legacy pointer-swap atomicity guarantee, idempotency on
re-writes, and the round-trip from DynamoDB-style query patterns
(the reader leans on the same access patterns).

Per ADR-005 the moto patches are wrapped in an ``autouse=True`` fixture
so no test can accidentally fall through to a real AWS endpoint. The
fixture also resets the writer's lazy cached DynamoDB resource between
tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from unittest.mock import patch

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.symbol_graph_writer import ...`` works
# without invoking the rest of the lambda/process-batch-summary handler.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL_GRAPH_TABLE = "test-symbol-graph"
CODE_MAPS_TABLE = "test-code-maps"
WORKSPACE_ID = "ws-test"
REPO = "owner/repo"
OLD_SHA = "abc123"
NEW_SHA = "def456"


# ---------------------------------------------------------------------------
# Lightweight Fact shim — the writer takes any object exposing the
# documented attributes. Avoiding the extractor package import keeps the
# test classpath narrow and parser-independent.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FactShim:
    kind: str
    source_symbol_id: str
    target_symbol_id: str
    file_path: str
    line: int
    language: str


# ---------------------------------------------------------------------------
# Autouse fixtures — ADR-005 cross-cutting standard.
#
# ``mock_aws`` patches every AWS client/resource; ``_reset_writer_clients``
# clears the writer's lazy cache so each test sees a fresh moto resource.
# ``_isolate_env`` keeps environment-variable leaks out of the module
# under test.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    """Autouse AWS mock — no test may touch a real AWS endpoint.

    Resets boto3's default session before AND after each test so that
    any cross-file test contamination (other tests creating a default
    session against real AWS endpoints before mock_aws is established)
    doesn't carry into this file. Defends against the test-isolation
    pattern documented in memory/feedback_*.md.
    """
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_writer_clients():
    """Inject this file's (real) ``boto3.resource`` into the writer
    via ``_set_resource_for_tests``. This bypasses the writer's
    module-level ``boto3`` reference, which can be a MagicMock under
    the wider suite's cross-file sys.modules contamination (see
    ``test_check_handlers_init.py:16``). Within this test file ``boto3``
    is imported cleanly above and resolves to the real package, so
    constructing the resource here and handing it to the writer
    sidesteps the contamination entirely.
    """
    from shared import symbol_graph_writer  # type: ignore

    symbol_graph_writer._reset_clients_for_tests()
    # ``boto3.resource`` here is the one imported at the top of THIS
    # file — guaranteed real.
    symbol_graph_writer._set_resource_for_tests(
        boto3.resource("dynamodb", region_name="us-west-2")
    )
    yield
    symbol_graph_writer._reset_clients_for_tests()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    """Strip env vars that the writer would otherwise pick up."""
    monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
    monkeypatch.delenv("CODE_MAPS_TABLE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


# ---------------------------------------------------------------------------
# Helpers: create the two DynamoDB tables under moto.
# ---------------------------------------------------------------------------


def _create_symbol_graph_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=SYMBOL_GRAPH_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
            {"AttributeName": "GSI2PK", "AttributeType": "S"},
            {"AttributeName": "GSI2SK", "AttributeType": "S"},
            {"AttributeName": "GSI3PK", "AttributeType": "S"},
            {"AttributeName": "GSI3SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "target-kind-index",
                "KeySchema": [
                    {"AttributeName": "GSI1PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI1SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "target-prefix-index",
                "KeySchema": [
                    {"AttributeName": "GSI2PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI2SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
            {
                "IndexName": "target-name-index",
                "KeySchema": [
                    {"AttributeName": "GSI3PK", "KeyType": "HASH"},
                    {"AttributeName": "GSI3SK", "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            },
        ],
        BillingMode="PAY_PER_REQUEST",
    )


def _create_code_maps_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=CODE_MAPS_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def tables():
    """Create both DynamoDB tables and return a resource handle."""
    _create_symbol_graph_table()
    _create_code_maps_table()
    return boto3.resource("dynamodb", region_name="us-west-2")


# ---------------------------------------------------------------------------
# Fact factories — keep tests legible.
# ---------------------------------------------------------------------------


def _calls_fact(i: int, target: str = "python:lambda.shared.audit::log_audit_entry") -> FactShim:
    return FactShim(
        kind="calls",
        source_symbol_id=f"python:lambda.caller{i}.handler::lambda_handler",
        target_symbol_id=target,
        file_path=f"lambda/caller{i}/handler.py",
        line=10 + i,
        language="python",
    )


def _imports_fact(i: int, target: str) -> FactShim:
    return FactShim(
        kind="imports",
        source_symbol_id=f"typescript:apps/app{i}/src/index::default",
        target_symbol_id=target,
        file_path=f"apps/app{i}/src/index.ts",
        line=1,
        language="typescript",
    )


def _implements_fact(i: int) -> FactShim:
    return FactShim(
        kind="implements",
        source_symbol_id=f"java:com.example.svc{i}::ServiceImpl",
        target_symbol_id="java:com.example.api::Service",
        file_path=f"src/main/java/com/example/svc{i}/ServiceImpl.java",
        line=42,
        language="java",
    )


# ===========================================================================
# Round-trip — write Facts, query via each access pattern.
# ===========================================================================


def test_write_facts_round_trip_table_query(tables):
    """v2 schema: facts land in the ``(workspace, repo)`` partition and
    are queryable end-to-end. ``commit_sha`` rides along as a row
    attribute for provenance.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts = [_calls_fact(i) for i in range(5)]

    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    assert result.facts_written == 5
    assert result.facts_deleted == 0
    assert result.previous_commit_sha is None
    assert result.pointer_swapped is True

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 5
    # Every row carries the commit_sha attribute (v2 provenance).
    assert all(item.get("commit_sha") == NEW_SHA for item in response["Items"])


def test_derive_bare_name_strips_language_prefix_and_namespace():
    """``_derive_bare_name`` is the source of truth for the bare-name
    attribute the chat-side fuzzy resolver Queries on. Each row in
    this table is a real shape the extractor emits today; if any
    fails, the chat resolver will silently miss those canonicals.
    """
    from shared.symbol_graph_writer import _derive_bare_name  # type: ignore

    cases = [
        ("python:lambda.auth.handler::require_auth", "require_auth"),
        ("typescript:src.lib.api.requestMagicLink", "requestMagicLink"),
        ("java:com.acme.Booking::confirm", "confirm"),
        ("abap:Z_CL_BOOKING::CONFIRM", "CONFIRM"),
        ("@acme-corp/shared-utils/http2", "http2"),
        ("bare_name_only", "bare_name_only"),
        ("", ""),
    ]
    for symbol_id, expected in cases:
        assert _derive_bare_name(symbol_id) == expected, symbol_id


def test_build_item_emits_bare_name_and_gsi3_keys(tables):
    """Every fact must carry ``bare_name`` plus the GSI3 keys so the
    target-name-index Query path on the chat handler can resolve a
    user-typed bare name back to its canonicals.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    target = "python:lambda.auth.handler::require_auth"
    facts = [_calls_fact(i, target=target) for i in range(3)]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 3
    for item in response["Items"]:
        assert item.get("bare_name") == "require_auth"
        assert item.get("GSI3PK") == f"{WORKSPACE_ID}#require_auth"
        assert item.get("GSI3SK", "").startswith(f"{target}#calls#")


def test_target_name_index_query_returns_canonical_for_bare_name(tables):
    """End-to-end on the new GSI: write facts for two distinct
    canonicals that share a bare name, Query target-name-index by
    ``(workspace_id, bare_name)``, expect both canonicals back.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    auth_canonical = "python:lambda.auth.handler::handle_workspace_invite"
    org_canonical = "python:lambda.org-management.handler::handle_workspace_invite"

    facts = [
        _calls_fact(0, target=auth_canonical),
        _calls_fact(1, target=org_canonical),
        _calls_fact(2, target=org_canonical),
    ]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        IndexName="target-name-index",
        KeyConditionExpression=Key("GSI3PK").eq(
            f"{WORKSPACE_ID}#handle_workspace_invite"
        ),
    )
    canonicals = {item["target_symbol_id"] for item in response["Items"]}
    assert canonicals == {auth_canonical, org_canonical}


def test_callers_query_via_gsi1(tables):
    """target-kind-index resolves callers(X) by (workspace, target)."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    target = "python:lambda.shared.audit::log_audit_entry"
    facts = [_calls_fact(i, target=target) for i in range(9)]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        IndexName="target-kind-index",
        KeyConditionExpression=(
            Key("GSI1PK").eq(f"{WORKSPACE_ID}#{target}")
            & Key("GSI1SK").begins_with("calls#")
        ),
    )
    assert response["Count"] == 9


def test_subpath_importers_query_via_gsi2(tables):
    """target-prefix-index resolves subpath wildcards by package prefix."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    targets = [
        "typescript:@acme-corp/shared-utils/http2::*",
        "typescript:@acme-corp/shared-utils/logger::*",
        "typescript:@acme-corp/shared-utils/auth::*",
    ]
    facts = [_imports_fact(i, target=t) for i, t in enumerate(targets)]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        IndexName="target-prefix-index",
        KeyConditionExpression=Key("GSI2PK").eq(
            f"{WORKSPACE_ID}#typescript:@acme-corp/shared-utils"
        ),
    )
    assert response["Count"] == 3


def test_implementers_query_via_gsi1(tables):
    """implements edges are queryable on the same GSI."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts = [_implements_fact(i) for i in range(4)]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        IndexName="target-kind-index",
        KeyConditionExpression=(
            Key("GSI1PK").eq(f"{WORKSPACE_ID}#java:com.example.api::Service")
            & Key("GSI1SK").begins_with("implements#")
        ),
    )
    assert response["Count"] == 4


# ===========================================================================
# Large round-trip across all three access patterns (1000 Facts).
# ===========================================================================


def test_write_facts_large_round_trip(tables):
    """Write 60 Facts (3x larger than one 25-item batch) and verify
    each access pattern resolves counts correctly. Validates that
    the writer's batching boundaries (25 items / page) do not lose
    items. Counts deliberately small enough to land in one moto
    Query response page so the test stays a single-call, no-loop
    assertion (avoids leaking-mock failure modes around pagination
    accumulators).
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts: list = []
    target_callee = "python:lambda.shared.audit::log_audit_entry"
    facts.extend(_calls_fact(i, target=target_callee) for i in range(30))

    import_target = "typescript:@acme-corp/shared-utils/http2::*"
    facts.extend(_imports_fact(i, target=import_target) for i in range(20))

    facts.extend(_implements_fact(i) for i in range(10))

    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.facts_written == 60

    table = tables.Table(SYMBOL_GRAPH_TABLE)

    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 60

    callers = table.query(
        IndexName="target-kind-index",
        KeyConditionExpression=(
            Key("GSI1PK").eq(f"{WORKSPACE_ID}#{target_callee}")
            & Key("GSI1SK").begins_with("calls#")
        ),
    )
    assert callers["Count"] == 30

    importers = table.query(
        IndexName="target-prefix-index",
        KeyConditionExpression=Key("GSI2PK").eq(
            f"{WORKSPACE_ID}#typescript:@acme-corp/shared-utils"
        ),
    )
    assert importers["Count"] == 20


# ===========================================================================
# Idempotency: writing identical Facts twice produces a single copy.
# ===========================================================================


def test_idempotent_rewrite_same_sha_no_duplicates(tables):
    """Per-file replace (ADR-046), write-then-prune ordering: rewriting the
    same Facts upserts them in place (same SKs), so the prune finds NOTHING
    stale to remove -- item count unchanged, ``facts_deleted == 0``.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts = [_calls_fact(i) for i in range(100)]

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    # Write-then-prune: the rewrite reuses the same SKs, so there is nothing
    # stale to prune; end count is unchanged.
    assert result.facts_deleted == 0

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 100


# ===========================================================================
# Dedupe: identical (PK, SK) within one write_facts call must not
# crash DynamoDB's BatchWriteItem with "Provided list of item keys
# contains duplicates". Surfaced 2026-05-15 in live re-extract.
# ===========================================================================


def test_write_facts_dedupes_identical_facts_within_one_batch(tables):
    """The chunk-01 extractor can emit two Facts with identical
    (kind, source, target, file_path, line) tuples for a single
    file (Tree-sitter walker visiting the same call twice under
    nested-scope traversal). Identical tuples produce identical SKs.
    Without dedupe, DynamoDB's BatchWriteItem rejects the entire
    25-item batch with ValidationException and the writer loses
    every fact in the batch. The dedupe step collapses identical
    items to a single put before send.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    base = _calls_fact(0)
    # Three identical Facts + two distinct ones. Same SK -> one row.
    facts = [base, base, base, _calls_fact(1), _calls_fact(2)]

    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    # All five Facts accepted; dedupe collapses to 3 unique items.
    assert result.facts_written == 3

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 3


# ===========================================================================
# Regen: a second write under NEW_SHA replaces the prior generation for
# the files it covers, within the one (workspace, repo) partition.
# ===========================================================================


def test_regen_under_new_sha_replaces_prior_generation(tables):
    """Per-file replace (ADR-046): a second write under NEW_SHA after an
    OLD_SHA write upserts the overlapping rows in place (identical SKs --
    same file/line) and adds the new ones. Because no line shifted, the
    prune finds nothing stale (``facts_deleted == 0``); the shifted-line
    case is covered by test_reextract_shifted_line_evicts_stale_generation.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    # First extraction at OLD_SHA: 50 facts (caller0..caller49).
    facts_v1 = [_calls_fact(i) for i in range(50)]
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=facts_v1,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    # Second extraction at NEW_SHA: 75 facts (caller0..caller74). The
    # first 50 SKs overlap with the OLD_SHA write and upsert; the new
    # 25 are inserted.
    facts_v2 = [_calls_fact(i) for i in range(75)]
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts_v2,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    # Write-then-prune: the 50 overlapping rows are upserted in place (same
    # SKs), so nothing is stale to prune. facts_written counts the
    # dedupe-unique PutItem operations the writer issued.
    assert result.facts_deleted == 0
    assert result.facts_written == 75
    assert result.pointer_swapped is True

    table = tables.Table(SYMBOL_GRAPH_TABLE)

    # End state: 75 facts in the one (workspace, repo) partition. All
    # rows -- both upserted and newly-inserted -- carry NEW_SHA on
    # their commit_sha attribute because the second write rewrote
    # them with the new provenance.
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert partition["Count"] == 75
    assert all(item.get("commit_sha") == NEW_SHA for item in partition["Items"])


# ===========================================================================
# The accumulation bug (root-caused 2026-07-10) + its fix (ADR-046):
# a fact SK embeds ``line``, so a line-shifting edit would orphan the old
# row under a plain upsert. Per-file replace evicts the prior generation.
# Parametrized across languages -- the replace keys on ``file_path`` and is
# language-agnostic (all extractors funnel through this one writer).
# ===========================================================================


@pytest.mark.parametrize(
    "language,file_path,caller,target",
    [
        (
            "python",
            "lambda/auth/handler.py",
            "python:lambda.auth.handler::handler",
            "python:lambda.auth.handler::require_auth",
        ),
        (
            "typescript",
            "src/lib/api.ts",
            "typescript:src/lib/api::request",
            "typescript:src/lib/auth::requireAuth",
        ),
    ],
)
def test_reextract_shifted_line_evicts_stale_generation(
    tables, language, file_path, caller, target
):
    from shared.symbol_graph_writer import write_facts  # type: ignore

    def call_at(line: int) -> FactShim:
        return FactShim(
            kind="calls",
            source_symbol_id=caller,
            target_symbol_id=target,
            file_path=file_path,
            line=line,
            language=language,
        )

    # First extraction: the call is at line 10.
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=[call_at(10)],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    # Second extraction after inserting code above: SAME call, now line 20.
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[call_at(20)],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.facts_deleted == 1  # the stale line-10 row

    # Exactly one caller row survives, at line 20 -- not two generations.
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    call_rows = [
        i
        for i in partition["Items"]
        if i["kind"] == "calls" and i["target_symbol_id"] == target
    ]
    assert len(call_rows) == 1
    assert call_rows[0]["line"] == 20


def test_replace_scoped_to_batch_files_leaves_others_intact(tables):
    """Per-file replace only touches the files in the current batch. A
    re-extract of file A must not evict file B's facts -- the invariant
    that lets parallel same-repo batches (disjoint file sets) run without
    racing on each other's deletes.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    file_a = "lambda/a/handler.py"
    file_b = "lambda/b/handler.py"

    def fact(file_path: str, line: int, src: str, tgt: str) -> FactShim:
        return FactShim(
            kind="calls",
            source_symbol_id=src,
            target_symbol_id=tgt,
            file_path=file_path,
            line=line,
            language="python",
        )

    # Seed both files.
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=[
            fact(file_a, 10, "python:a::h", "python:x::t"),
            fact(file_b, 10, "python:b::h", "python:y::t"),
        ],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    # Re-extract ONLY file A (line shifted).
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[fact(file_a, 25, "python:a::h", "python:x::t")],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.facts_deleted == 1  # only file A's old row

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    by_file = {}
    for i in partition["Items"]:
        by_file.setdefault(i["file_path"], []).append(i)
    # File B untouched (still line 10); file A replaced (single row, line 25).
    assert len(by_file[file_b]) == 1 and by_file[file_b][0]["line"] == 10
    assert len(by_file[file_a]) == 1 and by_file[file_a][0]["line"] == 25


def test_extracted_file_paths_clears_emptied_file(tables):
    """A file re-extracted but emitting zero facts (all its symbols were
    removed) still has its prior generation cleared when the caller names
    it in ``extracted_file_paths`` -- the fact-derived set alone misses it.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    emptied = "lambda/gone/handler.py"
    kept = "lambda/keep/handler.py"

    def fact(file_path: str, line: int, src: str, tgt: str) -> FactShim:
        return FactShim(
            kind="calls",
            source_symbol_id=src,
            target_symbol_id=tgt,
            file_path=file_path,
            line=line,
            language="python",
        )

    # Seed a fact in each file.
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=[
            fact(emptied, 10, "python:gone::h", "python:x::t"),
            fact(kept, 10, "python:keep::h", "python:y::t"),
        ],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    # Re-extract: ``kept`` still produces a fact; ``emptied`` produced none
    # but is named as having been re-extracted this batch.
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[fact(kept, 12, "python:keep::h", "python:y::t")],
        extracted_file_paths=[emptied, kept],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.facts_deleted == 2  # emptied's row + kept's old row

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    files = {i["file_path"] for i in partition["Items"]}
    assert emptied not in files  # cleared despite emitting zero facts
    assert kept in files


def test_write_failure_leaves_prior_generation_intact(tables, monkeypatch):
    """Throttle-safety regression (write-then-prune): if the batch write
    raises -- e.g. a DynamoDB ThrottlingException during a full-repo
    re-extract burst -- the file's PRIOR generation must survive. The prune
    runs only after a successful write, so a failed write deletes nothing and
    the file is left stale, never empty. Delete-first (the pre-fix ordering)
    would have wiped the prior rows before the failing write -> data loss.
    """
    from botocore.exceptions import ClientError

    from shared import symbol_graph_writer as w  # type: ignore

    target = "python:lambda.auth.handler::require_auth"
    same_file = "lambda/auth/handler.py"

    def call_at(line: int) -> FactShim:
        return FactShim(
            kind="calls",
            source_symbol_id="python:lambda.auth.handler::handler",
            target_symbol_id=target,
            file_path=same_file,
            line=line,
            language="python",
        )

    # Seed generation 1 (call at line 10) via a normal, successful write.
    w.write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=[call_at(10)],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    # The next write (same call, shifted to line 20) throttles mid-flight.
    def boom(*args, **kwargs):
        raise ClientError(
            {"Error": {"Code": "ThrottlingException", "Message": "slow down"}},
            "BatchWriteItem",
        )

    monkeypatch.setattr(w, "_write_facts_batched", boom)

    with pytest.raises(ClientError):
        w.write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[call_at(20)],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

    # Generation 1 (line 10) MUST still be present -- the failed write never
    # deleted it. (Delete-first would have left zero rows here.)
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    call_rows = [
        i
        for i in partition["Items"]
        if i["kind"] == "calls" and i["target_symbol_id"] == target
    ]
    assert len(call_rows) == 1
    assert call_rows[0]["line"] == 10  # prior generation intact, not gone


def test_facts_from_two_extractions_coexist_in_one_partition(tables):
    """v2 schema: facts extracted under different SHAs for different
    files coexist in the single ``(workspace, repo)`` partition with
    their respective ``commit_sha`` attributes preserved per row.

    The v1 model's "old partition" / "new partition" framing is gone --
    there is only one partition per (workspace, repo) and the row-level
    ``commit_sha`` attribute carries provenance.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore
    from dataclasses import dataclass

    @dataclass(frozen=True)
    class FactWithPath:
        kind: str
        source_symbol_id: str
        target_symbol_id: str
        file_path: str
        line: int
        language: str

    def _fact(path: str, i: int = 0):
        return FactWithPath(
            kind="calls",
            source_symbol_id=f"python:caller_{path}::lambda_handler",
            target_symbol_id=f"python:target_{path}::log_audit_entry",
            file_path=path,
            line=10 + i,
            language="python",
        )

    # First extraction at SHA_A: 3 integration handlers.
    extraction_a = [
        _fact("lambda/database-integration/handler.py"),
        _fact("lambda/jira-integration/handler.py"),
        _fact("lambda/github-integration/handler.py"),
    ]
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=extraction_a,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    # Second extraction at SHA_B: 2 DIFFERENT files (incremental regen
    # touching only what changed).
    extraction_b = [
        _fact("lambda/chat/handler.py"),
        _fact("lambda/process-batch-summary/handler.py"),
    ]
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=extraction_b,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    partition = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    # All 5 facts (3 + 2) live in the one (workspace, repo) partition.
    assert partition["Count"] == 5

    # Provenance: each row's commit_sha matches the extraction that
    # wrote it. The SHA_A rows were never touched by the SHA_B write
    # because their SKs differ.
    by_path = {item["file_path"]: item["commit_sha"] for item in partition["Items"]}
    assert by_path == {
        "lambda/database-integration/handler.py": OLD_SHA,
        "lambda/jira-integration/handler.py": OLD_SHA,
        "lambda/github-integration/handler.py": OLD_SHA,
        "lambda/chat/handler.py": NEW_SHA,
        "lambda/process-batch-summary/handler.py": NEW_SHA,
    }


# ===========================================================================
# Failure recovery: write fails => pointer never advances, old SHA queryable.
# ===========================================================================


def test_failure_recovery_pointer_never_swaps_on_write_failure(tables):
    """If the write pass raises mid-batch the legacy pointer must stay on
    the previous SHA. Under v2 the (workspace, repo) partition itself is
    upsert-only, but the legacy ``current_commit_sha`` pointer-swap
    still runs as step 4 and must not advance when step 3 raised.
    """
    from shared import symbol_graph_writer  # type: ignore

    def _read_legacy_pointer() -> str | None:
        table = tables.Table(CODE_MAPS_TABLE)
        item = table.get_item(
            Key={
                "PK": f"{symbol_graph_writer.POINTER_PK_PREFIX}{WORKSPACE_ID}#{REPO}",
                "SK": symbol_graph_writer.POINTER_SK,
            }
        ).get("Item")
        if not item:
            return None
        return item.get("current_commit_sha")

    facts_v1 = [_calls_fact(i) for i in range(20)]
    symbol_graph_writer.write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=OLD_SHA,
        facts=facts_v1,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    pointer_before = _read_legacy_pointer()
    assert pointer_before == OLD_SHA

    # Simulate the write pass blowing up by patching the batch helper.
    facts_v2 = [_calls_fact(i) for i in range(30)]

    def _boom(*_a, **_kw):
        raise RuntimeError("simulated DynamoDB throughput failure")

    with patch.object(
        symbol_graph_writer, "_write_facts_batched", side_effect=_boom
    ):
        with pytest.raises(RuntimeError, match="simulated"):
            symbol_graph_writer.write_facts(
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=NEW_SHA,
                facts=facts_v2,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                code_maps_table_name=CODE_MAPS_TABLE,
            )

    pointer_after = _read_legacy_pointer()
    assert pointer_after == OLD_SHA, (
        "Legacy pointer must NOT advance when the write pass failed."
    )


def test_write_facts_uses_env_vars_when_table_names_omitted(tables, monkeypatch):
    """SYMBOL_GRAPH_TABLE + CODE_MAPS_TABLE env vars are honored when the
    caller does not pass explicit names.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)

    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[_calls_fact(0)],
    )
    assert result.facts_written == 1
    assert result.pointer_swapped is True


# ===========================================================================
# Negative cases.
# ===========================================================================


def test_write_facts_rejects_empty_required_args():
    from shared.symbol_graph_writer import write_facts  # type: ignore

    with pytest.raises(ValueError):
        write_facts(
            workspace_id="",
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

    with pytest.raises(ValueError):
        write_facts(
            workspace_id=WORKSPACE_ID,
            repo="",
            commit_sha=NEW_SHA,
            facts=[],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )

    with pytest.raises(ValueError):
        write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha="",
            facts=[],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )


def test_write_facts_rejects_missing_table_name(monkeypatch):
    """Without a table name and no env var, the writer refuses to run."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
    with pytest.raises(ValueError, match="symbol_graph_table_name"):
        write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[],
        )


def test_first_write_no_previous_pointer(tables):
    """First-ever write for a (workspace, repo) returns previous_sha=None,
    facts_deleted=0 (always under v2), and pointer_swapped=True.
    """
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts = [_calls_fact(0)]
    result = write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.previous_commit_sha is None
    assert result.facts_deleted == 0
    assert result.facts_written == 1
    assert result.pointer_swapped is True


# ---------------------------------------------------------------------------
# Signature persistence (plan-signature-extraction chunk 01).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _ParamShim:
    name: str
    type: Optional[str] = None


@dataclass(frozen=True)
class _SigShim:
    params: tuple
    returns: Optional[str] = None


def _defines_fact_with_sig(target: str, signature) -> object:
    """A FactShim variant that carries an optional signature attribute."""

    @dataclass(frozen=True)
    class _DefinesShim:
        kind: str
        source_symbol_id: str
        target_symbol_id: str
        file_path: str
        line: int
        language: str
        signature: object = None

    return _DefinesShim(
        kind="defines",
        source_symbol_id="python:lambda.x.module::*",
        target_symbol_id=target,
        file_path="lambda/x/module.py",
        line=10,
        language="python",
        signature=signature,
    )


def test_build_item_persists_signature_when_present(tables):
    """A defines fact with a signature lands on the DDB item as a Map
    with params + returns, ready for the reader to surface back."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    sig = _SigShim(
        params=(
            _ParamShim("a", "int"),
            _ParamShim("b", "str"),
            _ParamShim("c"),  # bare, no type
        ),
        returns="bool",
    )
    target = "python:lambda.x.module::greet"
    fact = _defines_fact_with_sig(target, sig)

    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[fact],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert response["Count"] == 1
    item = response["Items"][0]
    assert "signature" in item
    persisted = item["signature"]
    assert persisted["params"] == [
        {"name": "a", "type": "int"},
        {"name": "b", "type": "str"},
        {"name": "c"},
    ]
    assert persisted["returns"] == "bool"


def test_build_item_omits_signature_attr_when_fact_has_none(tables):
    """A defines fact whose signature is None never produces a signature
    attribute on the DDB item (back-compat with extractors and stored
    rows that predate the field)."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    fact = _defines_fact_with_sig(
        "python:lambda.x.module::SomeClass", signature=None
    )
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[fact],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    response = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )
    assert "signature" not in response["Items"][0]


def test_build_item_omits_signature_returns_when_unannotated(tables):
    """A signature without a return annotation persists params only --
    no `returns: None` is written, keeping the item minimal."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    sig = _SigShim(params=(_ParamShim("x"),), returns=None)
    fact = _defines_fact_with_sig("python:lambda.x.module::noreturn", sig)
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=NEW_SHA,
        facts=[fact],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    persisted = table.query(
        KeyConditionExpression=Key("PK").eq(f"{WORKSPACE_ID}#{REPO}")
    )["Items"][0]["signature"]
    assert persisted == {"params": [{"name": "x"}]}


# ===========================================================================
# ADR-055 resolution storage primitives: scan_repo_facts + rewrite_resolved_targets
# ===========================================================================


class TestResolutionStoragePrimitives:
    """``scan_repo_facts`` reads a repo's facts back so the resolution pass
    can rebuild them; ``rewrite_resolved_targets`` applies the resolved
    targets by replacing the changed edges' rows (PUT new, DELETE old).
    """

    def test_scan_repo_facts_returns_all_rows_with_projected_fields(self, tables):
        from shared.symbol_graph_writer import (  # type: ignore
            scan_repo_facts,
            write_facts,
        )

        write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[_calls_fact(i) for i in range(4)],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            skip_pointer_swap=True,
        )

        rows = scan_repo_facts(
            WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
        )

        assert len(rows) == 4
        for r in rows:
            # Every projected field the resolver needs is present.
            assert r["SK"]
            assert r["kind"] == "calls"
            assert r["source_symbol_id"]
            assert r["target_symbol_id"]
            assert r["file_path"]
            assert r["language"] == "python"
            assert int(r["line"]) >= 10

    def test_scan_repo_facts_paginates(self, tables):
        # 60 rows forces moto to return more than one Query page when the
        # caller reads with a small cap -- the pagination loop must gather all.
        from shared.symbol_graph_writer import (  # type: ignore
            scan_repo_facts,
            write_facts,
        )

        write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[_calls_fact(i) for i in range(60)],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            skip_pointer_swap=True,
        )

        rows = scan_repo_facts(
            WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
        )
        assert len({r["SK"] for r in rows}) == 60

    def test_scan_repo_facts_empty_partition(self, tables):
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

        assert (
            scan_repo_facts(
                WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
            )
            == []
        )

    def test_rewrite_empty_is_noop(self, tables):
        from shared.symbol_graph_writer import (  # type: ignore
            rewrite_resolved_targets,
        )

        result = rewrite_resolved_targets(
            WORKSPACE_ID,
            REPO,
            NEW_SHA,
            [],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result.rewritten == 0
        assert result.deleted == 0

    def _seed_one_raw_call(self, raw_target):
        from shared.symbol_graph_writer import (  # type: ignore
            scan_repo_facts,
            write_facts,
        )

        orig = FactShim(
            kind="calls",
            source_symbol_id="python:lambda.caller0.handler::lambda_handler",
            target_symbol_id=raw_target,
            file_path="lambda/caller0/handler.py",
            line=11,
            language="python",
        )
        write_facts(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=NEW_SHA,
            facts=[orig],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            skip_pointer_swap=True,
        )
        rows = scan_repo_facts(
            WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
        )
        assert len(rows) == 1
        return orig, rows[0]["SK"]

    def test_rewrite_replaces_changed_row(self, tables):
        from shared.symbol_graph_writer import (  # type: ignore
            rewrite_resolved_targets,
            scan_repo_facts,
        )

        raw = "python:lambda.caller0.handler::log_audit_entry"
        resolved = "python:lambda.shared.audit::log_audit_entry"
        orig, old_sk = self._seed_one_raw_call(raw)

        new_fact = FactShim(
            kind="calls",
            source_symbol_id=orig.source_symbol_id,
            target_symbol_id=resolved,
            file_path=orig.file_path,
            line=orig.line,
            language="python",
        )
        result = rewrite_resolved_targets(
            WORKSPACE_ID,
            REPO,
            NEW_SHA,
            [(old_sk, new_fact)],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )

        assert result.rewritten == 1
        assert result.deleted == 1

        rows = scan_repo_facts(
            WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
        )
        assert len(rows) == 1  # old row replaced, not duplicated
        assert rows[0]["target_symbol_id"] == resolved
        assert rows[0]["SK"] != old_sk

    def test_rewrite_moves_gsi1_target_partition(self, tables):
        # The GSI1 (target-kind-index) partition is keyed on the target, so a
        # resolved rewrite must move the row: the OLD target partition empties
        # and the NEW target partition gains the row. This is the access path
        # ``callers(definition_id)`` Queries -- the whole point of resolution.
        from shared.symbol_graph_writer import (  # type: ignore
            rewrite_resolved_targets,
        )

        raw = "python:lambda.caller0.handler::log_audit_entry"
        resolved = "python:lambda.shared.audit::log_audit_entry"
        orig, old_sk = self._seed_one_raw_call(raw)
        new_fact = FactShim(
            kind="calls",
            source_symbol_id=orig.source_symbol_id,
            target_symbol_id=resolved,
            file_path=orig.file_path,
            line=orig.line,
            language="python",
        )
        rewrite_resolved_targets(
            WORKSPACE_ID,
            REPO,
            NEW_SHA,
            [(old_sk, new_fact)],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )

        table = tables.Table(SYMBOL_GRAPH_TABLE)
        old_partition = table.query(
            IndexName="target-kind-index",
            KeyConditionExpression=Key("GSI1PK").eq(f"{WORKSPACE_ID}#{raw}"),
        )
        new_partition = table.query(
            IndexName="target-kind-index",
            KeyConditionExpression=Key("GSI1PK").eq(f"{WORKSPACE_ID}#{resolved}"),
        )
        assert old_partition["Count"] == 0
        assert new_partition["Count"] == 1
        assert new_partition["Items"][0]["target_symbol_id"] == resolved
