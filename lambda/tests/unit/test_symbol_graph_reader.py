"""Unit tests for ``shared.symbol_graph_reader``.

Covers the four query shapes (``callers``, ``importers``,
``implementers``, ``definitions``), the empty-result invariant
(``QueryResult(sites=[], ...)`` is the answer, not ``None`` and not an
exception), truncation, prefix vs exact ``importers``, workspace
scoping, staleness reporting, retry/exhaustion, and the
``report_repos_snapshot`` integration that anchors the OutcomeOps
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


def test_definitions_empty_result_returns_empty_list_not_none(tables):
    """``definitions(X)`` returns ``[]`` for unknown symbols — never None."""
    from shared.symbol_graph_reader import definitions  # type: ignore

    result = definitions(
        WORKSPACE_ID,
        "python:unknown.module::unknown_symbol",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert result == []


# ===========================================================================
# Single result
# ===========================================================================


def test_callers_single_result(tables):
    """One caller -> one CallSite in the result."""
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
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

    sym = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
    _put_fact(
        tables.Table(SYMBOL_GRAPH_TABLE),
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.shared.outcomeops_license",
        target_symbol_id=sym,
        file_path="lambda/shared/outcomeops_license.py",
        line=42,
        language="python",
    )

    result = definitions(
        WORKSPACE_ID,
        sym,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result) == 1
    assert result[0].file_path == "lambda/shared/outcomeops_license.py"
    assert result[0].line == 42


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
    assert len(result) == 2
    repos = {site.repo for site in result}
    assert repos == {REPO, OTHER_REPO}


# ===========================================================================
# Truncation
# ===========================================================================


def test_callers_truncated_when_result_exceeds_limit(tables):
    """Writing limit+5 facts and querying with limit -> truncated=True
    and ``len(sites) == limit``.
    """
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
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
    """``importers("typescript:@hyatt-hpe/hpe-utils")`` -> every consumer
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
            target_symbol_id=f"typescript:@hyatt-hpe/hpe-utils/{sub}",
            file_path=f"apps/app{i}/src/index.ts",
            line=1,
            language="typescript",
        )

    result = importers(
        WORKSPACE_ID,
        "typescript:@hyatt-hpe/hpe-utils",
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        code_maps_table_name=CODE_MAPS_TABLE,
    )
    assert len(result.sites) == 3
    files = {s.file_path for s in result.sites}
    assert files == {f"apps/app{i}/src/index.ts" for i in range(3)}


def test_importers_exact_subpath_returns_only_that_subpath(tables):
    """``importers("typescript:@hyatt-hpe/hpe-utils/http2")`` -> only the
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
            target_symbol_id=f"typescript:@hyatt-hpe/hpe-utils/{sub}",
            file_path=f"apps/app{i}/src/index.ts",
            line=1,
            language="typescript",
        )

    result = importers(
        WORKSPACE_ID,
        "typescript:@hyatt-hpe/hpe-utils/http2",
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
        target_symbol_id="typescript:@hyatt-hpe/hpe-utils/http2",
        file_path="apps/app0/src/index.ts",
        line=1,
        language="typescript",
    )

    result = importers(
        WORKSPACE_ID,
        "typescript:@hyatt-hpe/hpe-utils/*",
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

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"
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
# report_repos_snapshot integration
# ===========================================================================


def test_callers_report_repos_snapshot_resolves_nine_known_callers(tables):
    """callers(workspace_id, "...report_repos_snapshot") returns exactly
    the 9 known callers (the integration handlers wired in license-layer
    v2.0.0). Validates the OutcomeOps proof case end-to-end against the
    reader's public surface. See docs/symbol-graph-validation.md.
    """
    from shared.symbol_graph_reader import callers  # type: ignore

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"

    # The 9 callers wired in license-layer v2.0.0 (per the project
    # memory: 9 *-integration connect/disconnect handlers + the hourly
    # heartbeat). Order doesn't matter — the reader doesn't promise
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
        ("heartbeat", "lambda/license-heartbeat/handler.py", 42),
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
