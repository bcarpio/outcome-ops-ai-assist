"""Unit tests for ``shared.symbol_graph_writer`` (plan-gkg chunk 02, v2 schema).

Covers the v2 write path (SK-keyed upsert into a single (workspace, repo)
partition), the legacy pointer-swap atomicity guarantee, idempotency on
re-writes, and the round-trip from DynamoDB-style query patterns
(chunk 04 leans on the same access patterns).

Per ADR-005 + plan-gkg/09 the moto patches are wrapped in an
``autouse=True`` fixture so no test can accidentally fall through to a
real AWS endpoint. The fixture also resets the writer's lazy cached
DynamoDB resource between tests.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
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
# Autouse fixtures — ADR-005 + plan-gkg/09 cross-cutting standard.
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


def _calls_fact(i: int, target: str = "python:lambda.shared.outcomeops_license::report_repos_snapshot") -> FactShim:
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


def test_callers_query_via_gsi1(tables):
    """target-kind-index resolves callers(X) by (workspace, target)."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
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
        "typescript:@hyatt-hpe/hpe-utils/http2::*",
        "typescript:@hyatt-hpe/hpe-utils/logger::*",
        "typescript:@hyatt-hpe/hpe-utils/auth::*",
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
            f"{WORKSPACE_ID}#typescript:@hyatt-hpe/hpe-utils"
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
    target_callee = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
    facts.extend(_calls_fact(i, target=target_callee) for i in range(30))

    import_target = "typescript:@hyatt-hpe/hpe-utils/http2::*"
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
            f"{WORKSPACE_ID}#typescript:@hyatt-hpe/hpe-utils"
        ),
    )
    assert importers["Count"] == 20


# ===========================================================================
# Idempotency: writing identical Facts twice produces a single copy.
# ===========================================================================


def test_idempotent_rewrite_same_sha_no_duplicates(tables):
    """v2 schema: SK uniqueness (kind#source#target#file#line) makes
    every write a pure upsert into the ``(workspace, repo)`` partition.
    Rewriting the same Facts under the same SHA leaves the item count
    unchanged -- no delete pass needed.
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
    # v2: facts_deleted is always 0 (no delete pass in the writer).
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
# Regen: under v2 schema, a second write under NEW_SHA simply upserts
# overlapping rows in place within the one (workspace, repo) partition.
# ===========================================================================


def test_regen_under_new_sha_upserts_in_place(tables):
    """v2 schema: a second write under NEW_SHA after an OLD_SHA write
    upserts overlapping rows by SK within the single
    ``(workspace, repo)`` partition. Overlapping rows reflect NEW_SHA in
    their ``commit_sha`` attribute; new-to-NEW_SHA rows are added.

    No delete pass runs (``facts_deleted == 0`` always under v2).
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
    # v2: facts_deleted is always 0. facts_written counts dedupe-unique
    # PutItem operations the writer issued.
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
            target_symbol_id=f"python:target_{path}::report_repos_snapshot",
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
