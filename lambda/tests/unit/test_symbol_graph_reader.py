# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.symbol_graph_reader``.

Covers the four query shapes (``callers``, ``importers``,
``implementers``, ``definitions``), the empty-result invariant
(``QueryResult(sites=[], ...)`` is the answer, not ``None`` and not an
exception), truncation, prefix vs exact ``importers``, workspace
scoping, staleness reporting, retry/exhaustion, and the
``log_audit_entry`` integration that anchors the OutcomeOps
proof case. See docs/symbol-graph-validation.md.

Test isolation mirrors ``test_symbol_graph_writer.py`` /
``test_symbol_graph_pointer.py``:

- ``mock_aws`` autouse so no test can touch real AWS endpoints.
- ``_set_resource_for_tests`` autouse injection so the reader module's
  module-level boto3 reference can't resolve to the MagicMock that
  ``test_check_handlers_init`` plants in ``sys.modules`` under
  cross-suite ordering.
"""

from __future__ import annotations

# Defensive sys.modules cleanup at module import time — see the writer
# test's comment about ``test_check_handlers_init.py:16`` replacing
# ``sys.modules['boto3']`` with a MagicMock. We need to ensure that the
# reader module (which we import lazily inside each test) resolves
# ``boto3`` to the real package; clearing any prior ``shared.*`` cached
# entry that may already hold a bound MagicMock keeps the autouse fixture
# in control.
import sys as _sys
for _name in list(_sys.modules):
    if _name.startswith("shared.symbol_graph_reader"):
        _sys.modules.pop(_name, None)

import sys
import time
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from botocore.exceptions import ClientError
from moto import mock_aws


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.symbol_graph_reader import ...`` works
# without invoking the rest of any Lambda handler.
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SYMBOL_GRAPH_TABLE = "test-reader-symbol-graph"
CODE_MAPS_TABLE = "test-reader-code-maps"
WORKSPACE_ID = "ws-reader"
OTHER_WORKSPACE_ID = "ws-reader-other"
REPO = "owner/repo"
OTHER_REPO = "owner/other-repo"
SHA = "commit-sha-1"


# ---------------------------------------------------------------------------
# Autouse fixtures — same pattern as the writer/pointer test files.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    """No test may touch real AWS endpoints."""
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_reader_clients():
    """Inject this file's real boto3.resource into the reader AND the
    pointer module (the reader calls into ``symbol_graph_pointer``).
    """
    from shared import symbol_graph_pointer  # type: ignore
    from shared import symbol_graph_reader  # type: ignore
    from shared import symbol_graph_writer  # type: ignore

    real_resource = boto3.resource("dynamodb", region_name="us-west-2")

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_reader._set_resource_for_tests(real_resource)

    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_pointer._set_resource_for_tests(real_resource)

    # Writer is used in a couple of tests to seed facts via the public
    # API (kept the seeding realistic).
    symbol_graph_writer._reset_clients_for_tests()
    symbol_graph_writer._set_resource_for_tests(real_resource)

    yield

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_writer._reset_clients_for_tests()


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
    monkeypatch.delenv("CODE_MAPS_TABLE", raising=False)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


# ---------------------------------------------------------------------------
# Helpers
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
    _create_symbol_graph_table()
    _create_code_maps_table()
    return boto3.resource("dynamodb", region_name="us-west-2")


def _put_fact(
    table,
    *,
    workspace_id: str,
    repo: str,
    commit_sha: str,
    kind: str,
    source_symbol_id: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
    language: str,
    gsi2: bool = True,
) -> None:
    """Write one Fact directly via put_item.

    Mirrors the writer's key construction so tests don't depend on the
    writer's public surface for seeding (kept the reader tests
    independently auditable).
    """
    item = {
        "PK": f"{workspace_id}#{repo}#{commit_sha}",
        "SK": f"{kind}#{source_symbol_id}#{target_symbol_id}#{file_path}#{line}",
        "GSI1PK": f"{workspace_id}#{target_symbol_id}",
        "GSI1SK": f"{kind}#{repo}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": repo,
        "commit_sha": commit_sha,
        "kind": kind,
        "source_symbol_id": source_symbol_id,
        "target_symbol_id": target_symbol_id,
        "file_path": file_path,
        "line": int(line),
        "language": language,
    }
    if gsi2 and "/" in target_symbol_id:
        head, _, tail = target_symbol_id.rpartition("/")
        if head and tail:
            item["GSI2PK"] = f"{workspace_id}#{head}"
            item["GSI2SK"] = f"{tail}#{kind}#{repo}#{file_path}#{line}"

    # Populate GSI3 (target-name-index) so the fuzzy_resolve_by_name
    # tests see the rows. Derivation matches the writer.
    s = target_symbol_id
    if ":" in s and "::" not in s.split(":", 1)[0]:
        s = s.split(":", 1)[1]
    for sep in ("::", ".", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    bare = s
    if bare:
        item["bare_name"] = bare
        item["GSI3PK"] = f"{workspace_id}#{bare}"
        item["GSI3SK"] = (
            f"{target_symbol_id}#{kind}#{repo}#{file_path}#{line}"
        )
    table.put_item(Item=item)


# ===========================================================================
# Empty result
# ===========================================================================


def test_callers_empty_result_returns_query_result_not_none(tables):
    """An unknown symbol returns ``QueryResult(sites=[], truncated=False, ...)``."""
    from shared.symbol_graph_reader import callers, QueryResult  # type: ignore

    result = callers(
        WORKSPACE_ID,
        "python:unknown.module::unknown_symbol",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert isinstance(result, QueryResult)
    assert result.sites == []
    assert result.truncated is False
    # No repos contributed -> no SHA and no staleness anchor.
    assert result.graph_sha is None
    assert result.staleness_seconds is None


def test_definitions_empty_result_returns_empty_sites_not_none(tables):
    """``definitions(X)`` returns ``QueryResult(sites=[], complete=True,
    total_known=0, ...)`` for unknown symbols -- never None, and
    completeness-honest (the absence IS the answer)."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    result = definitions(
        WORKSPACE_ID,
        "python:unknown.module::unknown_symbol",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.sites == []
    assert result.complete is True
    assert result.truncated is False
    assert result.total_known == 0


# ===========================================================================
# Single result
# ===========================================================================


def test_callers_single_result(tables):
    """One caller -> one CallSite in the result."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.audit::log_audit_entry"
    _put_fact(
        tables.Table(SYMBOL_GRAPH_TABLE),
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:lambda.github_integration.handler::lambda_handler",
        target_symbol_id=target,
        file_path="lambda/github-integration/handler.py",
        line=137,
        language="python",
    )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    site = result.sites[0]
    assert site.file_path == "lambda/github-integration/handler.py"
    assert site.line == 137
    assert site.repo == REPO
    assert site.language == "python"
    assert (
        site.source_symbol_id
        == "python:lambda.github_integration.handler::lambda_handler"
    )
    assert result.truncated is False


def test_definitions_single_repo_returns_single_element_list(tables):
    """Normal case: symbol defined exactly once -> 1-element list."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    sym = "python:lambda.shared.audit::log_audit_entry"
    _put_fact(
        tables.Table(SYMBOL_GRAPH_TABLE),
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.shared.audit",
        target_symbol_id=sym,
        file_path="lambda/shared/audit.py",
        line=42,
        language="python",
    )

    result = definitions(
        WORKSPACE_ID,
        sym,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    assert result.sites[0].file_path == "lambda/shared/audit.py"
    assert result.sites[0].line == 42
    assert result.complete is True
    assert result.total_known == 1


def test_definitions_cross_repo_duplicate_returns_multi_element_list(tables):
    """Same symbol declared in two repos (interface re-declarations,
    duplicated code) -> multi-element list."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    sym = "typescript:@org/types::IBookingClient"
    for i, repo in enumerate([REPO, OTHER_REPO]):
        _put_fact(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            repo=repo,
            commit_sha=SHA,
            kind="defines",
            source_symbol_id=f"typescript:apps/app{i}/src/types",
            target_symbol_id=sym,
            file_path=f"apps/app{i}/src/types.ts",
            line=10 + i,
            language="typescript",
        )

    result = definitions(
        WORKSPACE_ID,
        sym,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 2
    repos = {site.repo for site in result.sites}
    assert repos == {REPO, OTHER_REPO}
    assert result.complete is True
    assert result.total_known == 2


# ===========================================================================
# Truncation
# ===========================================================================


def test_callers_truncated_when_result_exceeds_limit(tables):
    """Writing limit+5 facts and querying with limit -> truncated=True
    and ``len(sites) == limit``.
    """
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.audit::log_audit_entry"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    limit = 10
    for i in range(limit + 5):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:lambda.caller{i}::lambda_handler",
            target_symbol_id=target,
            file_path=f"lambda/caller{i}/handler.py",
            line=100 + i,
            language="python",
        )

    result = callers(
        WORKSPACE_ID,
        target,
        limit=limit,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == limit
    assert result.truncated is True


def test_callers_not_truncated_when_under_limit(tables):
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:foo::bar"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    for i in range(3):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:caller{i}::fn",
            target_symbol_id=target,
            file_path=f"src/caller{i}.py",
            line=i,
            language="python",
        )

    result = callers(
        WORKSPACE_ID,
        target,
        limit=100,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 3
    assert result.truncated is False


# ===========================================================================
# Importers prefix vs exact
# ===========================================================================


def test_importers_prefix_returns_all_subpath_consumers(tables):
    """``importers("typescript:@acme-corp/shared-utils")`` -> every consumer
    of any subpath under the package."""
    from shared.symbol_graph_reader import importers  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    subpaths = ["http2", "logger", "auth"]
    for i, sub in enumerate(subpaths):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="imports",
            source_symbol_id=f"typescript:apps/app{i}/src/index::default",
            target_symbol_id=f"typescript:@acme-corp/shared-utils/{sub}",
            file_path=f"apps/app{i}/src/index.ts",
            line=1,
            language="typescript",
        )

    result = importers(
        WORKSPACE_ID,
        "typescript:@acme-corp/shared-utils",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 3
    files = {s.file_path for s in result.sites}
    assert files == {f"apps/app{i}/src/index.ts" for i in range(3)}


def test_importers_exact_subpath_returns_only_that_subpath(tables):
    """``importers("typescript:@acme-corp/shared-utils/http2")`` -> only the
    http2 consumer."""
    from shared.symbol_graph_reader import importers  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    subpaths = ["http2", "logger", "auth"]
    for i, sub in enumerate(subpaths):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="imports",
            source_symbol_id=f"typescript:apps/app{i}/src/index::default",
            target_symbol_id=f"typescript:@acme-corp/shared-utils/{sub}",
            file_path=f"apps/app{i}/src/index.ts",
            line=1,
            language="typescript",
        )

    result = importers(
        WORKSPACE_ID,
        "typescript:@acme-corp/shared-utils/http2",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    assert result.sites[0].file_path == "apps/app0/src/index.ts"


def test_importers_wildcard_suffix_forces_prefix(tables):
    """Explicit ``/*`` suffix -> always prefix mode."""
    from shared.symbol_graph_reader import importers  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="imports",
        source_symbol_id="typescript:apps/app0/src/index::default",
        target_symbol_id="typescript:@acme-corp/shared-utils/http2",
        file_path="apps/app0/src/index.ts",
        line=1,
        language="typescript",
    )

    result = importers(
        WORKSPACE_ID,
        "typescript:@acme-corp/shared-utils/*",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1


# ===========================================================================
# Implementers
# ===========================================================================


def test_implementers_returns_all_implementers(tables):
    from shared.symbol_graph_reader import implementers  # type: ignore

    iface = "java:com.example.api::Service"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    for i in range(4):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="implements",
            source_symbol_id=f"java:com.example.svc{i}::ServiceImpl",
            target_symbol_id=iface,
            file_path=f"src/main/java/com/example/svc{i}/ServiceImpl.java",
            line=42,
            language="java",
        )

    result = implementers(
        WORKSPACE_ID,
        iface,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 4


# ===========================================================================
# Workspace scoping
# ===========================================================================


def test_workspace_scoping_isolates_facts(tables):
    """Facts in workspace A don't bleed into workspace B's query."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.audit::log_audit_entry"
    table = tables.Table(SYMBOL_GRAPH_TABLE)

    # 3 facts in WORKSPACE_ID
    for i in range(3):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:caller{i}::fn",
            target_symbol_id=target,
            file_path=f"caller{i}.py",
            line=i,
            language="python",
        )

    # 5 facts in OTHER_WORKSPACE_ID with the SAME target — must NOT
    # appear in WORKSPACE_ID's result.
    for i in range(5):
        _put_fact(
            table,
            workspace_id=OTHER_WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:other_caller{i}::fn",
            target_symbol_id=target,
            file_path=f"other_caller{i}.py",
            line=i,
            language="python",
        )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 3
    files = {s.file_path for s in result.sites}
    assert all(f.startswith("caller") for f in files)


# ===========================================================================
# Staleness reporting
# ===========================================================================


def test_staleness_seconds_matches_committed_at_math(tables):
    """``staleness_seconds`` = now - graph_sha_committed_at on the
    pointer row. Tests with a single-repo answer so we also assert
    graph_sha is surfaced."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:foo::bar"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:caller::fn",
        target_symbol_id=target,
        file_path="caller.py",
        line=1,
        language="python",
    )

    # Seed the pointer with a known committed-at timestamp 90 seconds ago.
    code_maps = tables.Table(CODE_MAPS_TABLE)
    committed_at = int(time.time()) - 90
    code_maps.put_item(
        Item={
            "PK": f"SYMBOL_GRAPH#{WORKSPACE_ID}#{REPO}",
            "SK": "POINTER",
            "graph_sha": SHA,
            "graph_sha_committed_at": committed_at,
        }
    )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result.graph_sha == SHA
    assert result.staleness_seconds is not None
    # Allow a few seconds of slack for test execution time.
    assert 88 <= result.staleness_seconds <= 100


def test_staleness_multi_repo_uses_oldest_committed_at(tables):
    """Workspace-wide answer spanning two repos -> graph_sha is None,
    staleness reflects the OLDEST committed-at across the repos."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:foo::bar"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    for repo in (REPO, OTHER_REPO):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=repo,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:caller_{repo}::fn",
            target_symbol_id=target,
            file_path=f"{repo}/caller.py",
            line=1,
            language="python",
        )

    code_maps = tables.Table(CODE_MAPS_TABLE)
    now = int(time.time())
    # REPO is fresh (10s old), OTHER_REPO is stale (300s old) — worst-case wins.
    code_maps.put_item(
        Item={
            "PK": f"SYMBOL_GRAPH#{WORKSPACE_ID}#{REPO}",
            "SK": "POINTER",
            "graph_sha": SHA,
            "graph_sha_committed_at": now - 10,
        }
    )
    code_maps.put_item(
        Item={
            "PK": f"SYMBOL_GRAPH#{WORKSPACE_ID}#{OTHER_REPO}",
            "SK": "POINTER",
            "graph_sha": SHA,
            "graph_sha_committed_at": now - 300,
        }
    )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 2
    # Multi-repo answer -> no single SHA describes it.
    assert result.graph_sha is None
    assert result.staleness_seconds is not None
    # Worst-case = OTHER_REPO's 300s, with a few seconds of slack.
    assert 295 <= result.staleness_seconds <= 320


def test_staleness_none_when_no_pointer_row(tables):
    """No pointer row -> staleness_seconds is None (we answer the query
    but honestly report 'no freshness anchor available')."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:foo::bar"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:caller::fn",
        target_symbol_id=target,
        file_path="caller.py",
        line=1,
        language="python",
    )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    assert result.staleness_seconds is None


# ===========================================================================
# Retry / SymbolGraphUnavailable
# ===========================================================================


def _throughput_error() -> ClientError:
    return ClientError(
        error_response={
            "Error": {
                "Code": "ProvisionedThroughputExceededException",
                "Message": "transient",
            },
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        operation_name="Query",
    )


def test_query_retries_on_transient_error_then_succeeds(tables):
    """Simulate one transient ClientError, then success -> result returned."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:foo::bar"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:caller::fn",
        target_symbol_id=target,
        file_path="caller.py",
        line=1,
        language="python",
    )

    # Hold a reference to the real (moto-backed) Table query so we can
    # delegate to it after the first failure.
    from shared import symbol_graph_reader  # type: ignore

    real_table = symbol_graph_reader._resource().Table(SYMBOL_GRAPH_TABLE)
    real_query = real_table.query

    call_count = {"n": 0}

    def _flaky_query(**kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise _throughput_error()
        return real_query(**kwargs)

    with patch.object(real_table, "query", side_effect=_flaky_query):
        with patch.object(
            symbol_graph_reader._resource(),
            "Table",
            return_value=real_table,
        ):
            result = callers(
                WORKSPACE_ID,
                target,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                code_maps_table_name=CODE_MAPS_TABLE,
            )

    assert call_count["n"] >= 2  # at least one retry happened
    assert len(result.sites) == 1


def test_query_raises_symbol_graph_unavailable_on_exhaustion(tables):
    """Persistent transient error -> SymbolGraphUnavailable after retries."""
    from shared import symbol_graph_reader  # type: ignore
    from shared.symbol_graph_reader import (  # type: ignore
        callers,
        SymbolGraphUnavailable,
    )

    real_table = symbol_graph_reader._resource().Table(SYMBOL_GRAPH_TABLE)

    def _always_throw(**_kwargs):
        raise _throughput_error()

    with patch.object(real_table, "query", side_effect=_always_throw):
        with patch.object(
            symbol_graph_reader._resource(),
            "Table",
            return_value=real_table,
        ):
            with pytest.raises(SymbolGraphUnavailable):
                callers(
                    WORKSPACE_ID,
                    "python:foo::bar",
                    symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                    code_maps_table_name=CODE_MAPS_TABLE,
                )


def test_non_retryable_client_error_propagates(tables):
    """4xx errors outside the retry-allowlist propagate as ClientError —
    not SymbolGraphUnavailable. (e.g. ResourceNotFoundException on a
    misconfigured table is a real misconfig, not a transient.)"""
    from shared import symbol_graph_reader  # type: ignore
    from shared.symbol_graph_reader import callers  # type: ignore

    real_table = symbol_graph_reader._resource().Table(SYMBOL_GRAPH_TABLE)

    def _not_found(**_kwargs):
        raise ClientError(
            error_response={
                "Error": {
                    "Code": "ResourceNotFoundException",
                    "Message": "table missing",
                },
                "ResponseMetadata": {"HTTPStatusCode": 400},
            },
            operation_name="Query",
        )

    with patch.object(real_table, "query", side_effect=_not_found):
        with patch.object(
            symbol_graph_reader._resource(),
            "Table",
            return_value=real_table,
        ):
            with pytest.raises(ClientError):
                callers(
                    WORKSPACE_ID,
                    "python:foo::bar",
                    symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
                    code_maps_table_name=CODE_MAPS_TABLE,
                )


# ===========================================================================
# Negative cases
# ===========================================================================


def test_callers_rejects_empty_required_args():
    from shared.symbol_graph_reader import callers  # type: ignore

    with pytest.raises(ValueError):
        callers("", "x", symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
    with pytest.raises(ValueError):
        callers(WORKSPACE_ID, "", symbol_graph_table_name=SYMBOL_GRAPH_TABLE)


def test_callers_rejects_missing_table_name(monkeypatch):
    from shared.symbol_graph_reader import callers  # type: ignore

    monkeypatch.delenv("SYMBOL_GRAPH_TABLE", raising=False)
    with pytest.raises(ValueError, match="symbol_graph_table_name"):
        callers(WORKSPACE_ID, "python:foo::bar")


def test_callers_table_name_from_env(tables, monkeypatch):
    """SYMBOL_GRAPH_TABLE env var is honored when the caller omits the kwarg."""
    from shared.symbol_graph_reader import callers  # type: ignore

    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    result = callers(WORKSPACE_ID, "python:foo::bar")
    assert result.sites == []


# ===========================================================================
# log_audit_entry integration
# ===========================================================================


def test_callers_log_audit_entry_resolves_nine_known_callers(tables):
    """callers(workspace_id, "...log_audit_entry") returns exactly
    the 9 known callers of log_audit_entry. Validates the proof case
    end-to-end against the reader's public surface. See
    docs/symbol-graph-validation.md.
    """
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.audit::log_audit_entry"

    # The 9 callers of log_audit_entry. Order doesn't matter, the
    # reader doesn't promise
    # an order, just the set.
    known_callers = [
        ("github-integration", "lambda/github-integration/handler.py", 137),
        ("jira-integration", "lambda/jira-integration/handler.py", 114),
        ("confluence-integration", "lambda/confluence-integration/handler.py", 129),
        ("outlook-integration", "lambda/outlook-integration/handler.py", 127),
        ("github-sync", "lambda/github-sync/handler.py", 789),
        ("jira-sync", "lambda/jira-sync/handler.py", 452),
        ("confluence-sync", "lambda/confluence-sync/handler.py", 476),
        ("file-ingestion", "lambda/file-ingestion/handler.py", 1456),
        ("scheduler", "lambda/scheduled-reporter/handler.py", 42),
    ]

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    for name, file_path, line in known_callers:
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:lambda.{name.replace('-', '_')}.handler::lambda_handler",
            target_symbol_id=target,
            file_path=file_path,
            line=line,
            language="python",
        )

    result = callers(
        WORKSPACE_ID,
        target,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 9
    assert result.truncated is False

    returned_files = {s.file_path for s in result.sites}
    expected_files = {f for _, f, _ in known_callers}
    assert returned_files == expected_files

    # Spot-check that lines round-trip too (int coercion path).
    by_file = {s.file_path: s.line for s in result.sites}
    for name, file_path, line in known_callers:
        assert by_file[file_path] == line


# ===========================================================================
# fuzzy_resolve_by_name — chat-side bare-name resolution via target-name-index.
# ===========================================================================


def test_fuzzy_resolve_returns_single_canonical_for_unique_bare_name(tables):
    """One canonical, multiple edges pointing at it -> dedupes to a
    single-element list."""
    from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    canonical = "python:lambda.shared.audit::log_audit_entry"
    # One defines + several calls all pointing at the same target.
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha="abc",
        kind="defines",
        source_symbol_id="python:lambda.shared.audit::<module>",
        target_symbol_id=canonical,
        file_path="lambda/shared/audit.py",
        line=42,
        language="python",
    )
    for i in range(5):
        _put_fact(
            table,
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha="abc",
            kind="calls",
            source_symbol_id=f"python:lambda.caller{i}::lambda_handler",
            target_symbol_id=canonical,
            file_path=f"lambda/caller{i}/handler.py",
            line=10 + i,
            language="python",
        )

    result = fuzzy_resolve_by_name(
        WORKSPACE_ID,
        "log_audit_entry",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
    )
    assert result.canonicals == [canonical]
    assert result.complete is True
    assert result.total_known == 1


def test_fuzzy_resolve_returns_multiple_canonicals_for_overloaded_bare_name(tables):
    """Two canonicals share a bare name (same function name defined in
    two modules) -> both are returned, deduped to one entry each."""
    from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    auth_canonical = "python:lambda.auth.handler::handle_workspace_invite"
    org_canonical = "python:lambda.org-management.handler::handle_workspace_invite"

    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha="abc",
        kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=auth_canonical,
        file_path="lambda/auth/handler.py",
        line=1454,
        language="python",
    )
    _put_fact(
        table,
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha="abc",
        kind="defines",
        source_symbol_id="python:lambda.org-management.handler::<module>",
        target_symbol_id=org_canonical,
        file_path="lambda/org-management/handler.py",
        line=3229,
        language="python",
    )

    result = fuzzy_resolve_by_name(
        WORKSPACE_ID,
        "handle_workspace_invite",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
    )
    assert set(result.canonicals) == {auth_canonical, org_canonical}
    assert result.complete is True
    assert result.total_known == 2


def test_fuzzy_resolve_returns_empty_when_workspace_has_no_match(tables):
    """No row matches the bare name -> empty canonicals + complete=True
    (the absence IS the answer; completeness is honest)."""
    from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

    result = fuzzy_resolve_by_name(
        WORKSPACE_ID,
        "no_such_function",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
    )
    assert result.canonicals == []
    assert result.complete is True
    assert result.total_known == 0


def test_fuzzy_resolve_scopes_to_workspace(tables):
    """A canonical in workspace A must not leak into a fuzzy resolve
    scoped to workspace B."""
    from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

    table = tables.Table(SYMBOL_GRAPH_TABLE)
    canonical = "python:lambda.auth.handler::require_auth"
    _put_fact(
        table,
        workspace_id="ws-alpha",
        repo=REPO,
        commit_sha="abc",
        kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=canonical,
        file_path="lambda/auth/handler.py",
        line=588,
        language="python",
    )

    result = fuzzy_resolve_by_name(
        "ws-beta",
        "require_auth",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
    )
    assert result.canonicals == []
    assert result.complete is True


def test_fuzzy_resolve_returns_empty_for_blank_args(tables):
    """Defensive: empty workspace_id or bare_name returns a complete
    empty result without a DDB call."""
    from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

    r1 = fuzzy_resolve_by_name(
        "", "foo", symbol_graph_table_name=SYMBOL_GRAPH_TABLE
    )
    assert r1.canonicals == []
    assert r1.complete is True
    r2 = fuzzy_resolve_by_name(
        "ws-1", "", symbol_graph_table_name=SYMBOL_GRAPH_TABLE
    )
    assert r2.canonicals == []
    assert r2.complete is True



# ===========================================================================
# Signature deserialization (plan-signature-extraction )
# ===========================================================================


def test_definitions_surfaces_signature_when_present(tables):
    """A defines row with a persisted signature is returned on the
    CallSite as a dict the consumer can use directly."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    sym = "python:lambda.x.module::greet"
    table = tables.Table(SYMBOL_GRAPH_TABLE)
    item = {
        "PK": f"{WORKSPACE_ID}#{REPO}#{SHA}",
        "SK": f"defines#python:lambda.x.module::*#{sym}#lambda/x/module.py#10",
        "GSI1PK": f"{WORKSPACE_ID}#{sym}",
        "GSI1SK": f"defines#{REPO}#lambda/x/module.py#10",
        "workspace_id": WORKSPACE_ID,
        "repo": REPO,
        "commit_sha": SHA,
        "kind": "defines",
        "source_symbol_id": "python:lambda.x.module::*",
        "target_symbol_id": sym,
        "file_path": "lambda/x/module.py",
        "line": 10,
        "language": "python",
        "signature": {
            "params": [
                {"name": "name", "type": "str"},
                {"name": "loud", "type": "bool"},
            ],
            "returns": "str",
        },
    }
    table.put_item(Item=item)

    result = definitions(
        WORKSPACE_ID,
        sym,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    assert result.sites[0].signature == {
        "params": [
            {"name": "name", "type": "str"},
            {"name": "loud", "type": "bool"},
        ],
        "returns": "str",
    }


def test_definitions_back_compat_row_without_signature(tables):
    """A defines row predating the signature field reads as
    CallSite.signature == None (no crash, no fabrication)."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    sym = "python:lambda.x.legacy::do_a_thing"
    _put_fact(
        tables.Table(SYMBOL_GRAPH_TABLE),
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.x.legacy",
        target_symbol_id=sym,
        file_path="lambda/x/legacy.py",
        line=7,
        language="python",
    )
    result = definitions(
        WORKSPACE_ID,
        sym,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 1
    assert result.sites[0].signature is None


# ===========================================================================
# symbol_at_line: resolve (file, line) -> innermost defining symbol.
# The PK shape these tests seed is the LIVE v2 format
# (workspace_id#bare_repo, no commit_sha) because symbol_at_line
# queries the base table partition directly -- unlike the GSI-backed
# directional tools, where the seed's old PK shape (carrying a SHA)
# is invisible.
# ===========================================================================


def _put_defines_v2(
    table,
    *,
    workspace_id: str,
    bare_repo: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
    language: str = "python",
    commit_sha: str = SHA,
) -> None:
    """Seed one defines row using the LIVE base-table PK
    (workspace_id#bare_repo). The reader's symbol_at_line walks this
    partition by SK begins_with('defines#'), filtering on file_path.
    """
    source = target_symbol_id.rsplit("::", 1)[0]
    item = {
        "PK": f"{workspace_id}#{bare_repo}",
        "SK": (
            f"defines#{source}#{target_symbol_id}#{file_path}#{line}"
        ),
        "GSI1PK": f"{workspace_id}#{target_symbol_id}",
        "GSI1SK": f"defines#{bare_repo}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": bare_repo,
        "commit_sha": commit_sha,
        "kind": "defines",
        "source_symbol_id": source,
        "target_symbol_id": target_symbol_id,
        "file_path": file_path,
        "line": int(line),
        "language": language,
    }
    table.put_item(Item=item)


class TestSymbolAtLine:
    """Resolve (file, line) -> innermost defining symbol.

    v1 uses start-line only; the contract is "largest start_line <=
    target". A line between two functions returns the previous
    function -- a known v1 limitation that the chat prompt grounding
    rule (ADR-031) tells synthesis to handle by dropping the
    enclosing-name claim.
    """

    BARE = "auth-service"
    FILE = "lambda/auth/handler.py"

    def _seed_typical_file(self, table):
        """A handler.py with three defines:
        - line 487: _claim_magic_token_jti (function)
        - line 531: verify_magic_token (function)
        - line 1215: handle_verify (function)
        All in the same file. Matches the magic_link audit shape
        the plan was built around.
        """
        for sym, ln in [
            ("python:lambda.auth.handler::_claim_magic_token_jti", 487),
            ("python:lambda.auth.handler::verify_magic_token", 531),
            ("python:lambda.auth.handler::handle_verify", 1215),
        ]:
            _put_defines_v2(
                table,
                workspace_id=WORKSPACE_ID,
                bare_repo=self.BARE,
                target_symbol_id=sym,
                file_path=self.FILE,
                line=ln,
            )

    def test_line_inside_a_function_returns_that_function(self, tables):
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        self._seed_typical_file(tables.Table(SYMBOL_GRAPH_TABLE))
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            self.FILE,
            544,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is not None
        # The magic_link audit failure: 544 is INSIDE verify_magic_token
        # (start 531), NOT handle_verify (start 1215). The reader must
        # return verify_magic_token.
        assert (
            result.target_symbol_id
            == "python:lambda.auth.handler::verify_magic_token"
        )
        assert result.file_path == self.FILE
        assert result.line == 531

    def test_line_equals_start_line_returns_the_function(self, tables):
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        self._seed_typical_file(tables.Table(SYMBOL_GRAPH_TABLE))
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            self.FILE,
            531,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is not None
        assert (
            result.target_symbol_id
            == "python:lambda.auth.handler::verify_magic_token"
        )

    def test_line_above_any_defines_returns_none(self, tables):
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        self._seed_typical_file(tables.Table(SYMBOL_GRAPH_TABLE))
        # Line 100 is before the first defines at line 487. No
        # enclosing symbol -> None.
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            self.FILE,
            100,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is None

    def test_v1_limitation_line_between_two_functions(self, tables):
        """v1 only has start_line, so a line that falls AFTER one
        function ends but BEFORE the next starts incorrectly returns
        the previous function. ADR-031 tells synthesis to drop the
        enclosing-name claim in this case; v2 will add end_line for
        strict containment. This test pins the v1 behavior so a
        future end_line addition will need to update it deliberately.
        """
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        # Two functions with a gap: 487 and 531 -- line 525 is module-
        # level or blank between them.
        for sym, ln in [
            ("python:lambda.auth.handler::a_func", 487),
            ("python:lambda.auth.handler::b_func", 531),
        ]:
            _put_defines_v2(
                tables.Table(SYMBOL_GRAPH_TABLE),
                workspace_id=WORKSPACE_ID,
                bare_repo=self.BARE,
                target_symbol_id=sym,
                file_path=self.FILE,
                line=ln,
            )
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            self.FILE,
            525,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is not None
        # v1 limitation: returns the previous function. Documented.
        assert (
            result.target_symbol_id
            == "python:lambda.auth.handler::a_func"
        )

    def test_nested_class_method_returns_innermost(self, tables):
        """class C: at line 100; method m: at line 110. For target
        line 115, both class and method qualify. Innermost = method
        (highest start_line)."""
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        for sym, ln in [
            ("csharp:Acme::C", 100),
            ("csharp:Acme::C::m", 110),
        ]:
            _put_defines_v2(
                tables.Table(SYMBOL_GRAPH_TABLE),
                workspace_id=WORKSPACE_ID,
                bare_repo=self.BARE,
                target_symbol_id=sym,
                file_path="src/Acme/C.cs",
                line=ln,
                language="csharp",
            )
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            "src/Acme/C.cs",
            115,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is not None
        assert result.target_symbol_id == "csharp:Acme::C::m"

    def test_file_path_mismatch_returns_none(self, tables):
        """Defines exist for a different file in the same repo.
        Querying file_path with no defines must return None, not the
        first defines in the partition."""
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        _put_defines_v2(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            bare_repo=self.BARE,
            target_symbol_id="python:lambda.auth.handler::verify_magic_token",
            file_path=self.FILE,
            line=531,
        )
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            "lambda/other/handler.py",
            10,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is None

    def test_workspace_isolation(self, tables):
        """Defines exist in another workspace -- this workspace gets
        None, even though file_path + line match."""
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        _put_defines_v2(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=OTHER_WORKSPACE_ID,
            bare_repo=self.BARE,
            target_symbol_id="python:lambda.auth.handler::verify_magic_token",
            file_path=self.FILE,
            line=531,
        )
        result = symbol_at_line(
            WORKSPACE_ID,
            self.BARE,
            self.FILE,
            544,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is None

    def test_owner_prefixed_repo_normalizes_to_bare(self, tables):
        """Caller passes 'bcarpio/eShop'; graph PK is 'workspace#eShop'.
        The reader must strip the owner before the PK lookup."""
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        _put_defines_v2(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            bare_repo="eShop",
            target_symbol_id="csharp:eShop.ServiceDefaults::SignaturesDemo::Greet",
            file_path="src/eShop.ServiceDefaults/SignaturesDemo.cs",
            line=10,
            language="csharp",
        )
        result = symbol_at_line(
            WORKSPACE_ID,
            "bcarpio/eShop",
            "src/eShop.ServiceDefaults/SignaturesDemo.cs",
            12,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert result is not None
        assert (
            result.target_symbol_id
            == "csharp:eShop.ServiceDefaults::SignaturesDemo::Greet"
        )

    def test_missing_args_raises_value_error(self, tables):
        from shared.symbol_graph_reader import symbol_at_line  # type: ignore

        with pytest.raises(ValueError):
            symbol_at_line(
                "",
                self.BARE,
                self.FILE,
                10,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            )
        with pytest.raises(ValueError):
            symbol_at_line(
                WORKSPACE_ID,
                "",
                self.FILE,
                10,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            )
        with pytest.raises(ValueError):
            symbol_at_line(
                WORKSPACE_ID,
                self.BARE,
                "",
                10,
                symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            )


# ===========================================================================
# List-completeness contract (ADR-032) — definitions() + fuzzy_resolve_by_name()
# expose truncated/complete/total_known on a wrapped result type so consumers
# can detect a clipped list and either disclose or refuse.
# ===========================================================================


class TestListCompletenessContract:
    """Each list-returning reader populates the truncation fields the
    chat synthesis prompt rule binds against."""

    def test_definitions_truncates_when_limit_hit(self, tables):
        """A symbol with more definitions than the limit returns
        truncated=True, complete=False, total_known=None."""
        from shared.symbol_graph_reader import definitions  # type: ignore

        sym = "typescript:@org/types::IBookingClient"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        # Seed 6 cross-repo defines for the same symbol.
        for i in range(6):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=f"repo-{i}",
                commit_sha=SHA,
                kind="defines",
                source_symbol_id=f"typescript:apps/app{i}/src/types",
                target_symbol_id=sym,
                file_path=f"apps/app{i}/src/types.ts",
                line=10 + i,
                language="typescript",
            )
        # Cap at 3 -- forces truncation.
        result = definitions(
            WORKSPACE_ID,
            sym,
            limit=3,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(result.sites) == 3
        assert result.truncated is True
        assert result.complete is False
        # ADR-032: when truncated and the reader can't compute total
        # cheaply, total_known is None -- consumer must disclose or
        # call again with higher limit; LLM must NOT estimate.
        assert result.total_known is None

    def test_definitions_complete_when_under_limit(self, tables):
        """When the result fits under the limit, total_known is the
        exact len(sites) -- chat synthesis quotes this verbatim
        rather than computing the count."""
        from shared.symbol_graph_reader import definitions  # type: ignore

        sym = "python:lambda.shared.foo::bar"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        for i in range(3):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=f"repo-{i}",
                commit_sha=SHA,
                kind="defines",
                source_symbol_id=f"python:lambda.shared.foo",
                target_symbol_id=sym,
                file_path=f"src/foo{i}.py",
                line=10 + i,
                language="python",
            )
        result = definitions(
            WORKSPACE_ID,
            sym,
            limit=50,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(result.sites) == 3
        assert result.truncated is False
        assert result.complete is True
        assert result.total_known == 3

    def test_fuzzy_resolve_truncates_when_canonical_set_exceeds_limit(
        self, tables
    ):
        """The audit-failure shape: a bare name with N>limit distinct
        canonicals returns a clipped set AND a truncated=True signal
        so the chat router and prompt can
        recover instead of shipping silently wrong."""
        from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

        bare = "require_auth"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        # Seed 10 distinct canonicals (simulating per-lambda vendored
        # require_auth) with require_auth as the bare name.
        for i in range(10):
            canonical = f"python:lambda.integration{i}.handler::require_auth"
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="defines",
                source_symbol_id=f"python:lambda.integration{i}.handler::<module>",
                target_symbol_id=canonical,
                file_path=f"lambda/integration{i}/handler.py",
                line=100 + i,
                language="python",
            )
        # Cap at 5 -- forces dedupe-truncation.
        result = fuzzy_resolve_by_name(
            WORKSPACE_ID,
            bare,
            limit=5,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert len(result.canonicals) == 5
        assert result.truncated is True
        assert result.complete is False
        assert result.total_known is None

    def test_fuzzy_resolve_complete_when_all_canonicals_returned(
        self, tables
    ):
        """When the limit covers the canonical set, complete=True and
        total_known is the deterministic count."""
        from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore

        bare = "log_audit_entry"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        for i in range(2):
            canonical = (
                f"python:lambda.shared{i}.audit"
                f"::log_audit_entry"
            )
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="defines",
                source_symbol_id=f"python:lambda.shared{i}.audit",
                target_symbol_id=canonical,
                file_path=f"lambda/shared{i}/audit.py",
                line=42 + i,
                language="python",
            )
        result = fuzzy_resolve_by_name(
            WORKSPACE_ID,
            bare,
            limit=25,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        )
        assert len(result.canonicals) == 2
        assert result.complete is True
        assert result.total_known == 2

    def test_callers_carries_total_known_when_complete(self, tables):
        """The three already-truncated-aware readers (callers,
        importers, implementers) gain total_known via QueryResult so
        the same prompt rule can apply uniformly."""
        from shared.symbol_graph_reader import callers  # type: ignore

        target = "python:lambda.shared.foo::bar"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        for i in range(4):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="calls",
                source_symbol_id=f"python:caller{i}::fn",
                target_symbol_id=target,
                file_path=f"src/caller{i}.py",
                line=10 + i,
                language="python",
            )
        result = callers(
            WORKSPACE_ID,
            target,
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
            code_maps_table_name=CODE_MAPS_TABLE,
        )
        assert len(result.sites) == 4
        assert result.complete is True
        assert result.total_known == 4
