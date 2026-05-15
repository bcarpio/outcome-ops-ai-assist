"""End-to-end test of ``chat.handler._augment_context_with_symbol_graph``
against a moto-backed symbol-graph table.

This is the chat-augmentation "Definition of done" gate:

> ``report_repos_snapshot`` and ``@hyatt-hpe/hpe-utils/http2`` queries
> return correct caller / importer lists (verified end-to-end on dev
> workspace after backfill).

We can't reach the dev DynamoDB from CI, so we substitute moto for
the live table and assert the augmentation helper renders the
expected block from end-to-end:

  user question
    -> router (Bedrock stubbed)
    -> definitions() lookup
    -> callers() lookup
    -> context_builder.build_symbol_facts_block
    -> string containing all 9 callers' file paths

Test isolation mirrors ``test_symbol_graph_reader.py`` exactly.
"""

from __future__ import annotations

import sys as _sys

for _name in list(_sys.modules):
    if (
        _name.startswith("shared.symbol_graph_reader")
        or _name.startswith("chat_router")
        or _name == "router"
        or _name == "context_builder"
    ):
        _sys.modules.pop(_name, None)

import sys
from pathlib import Path
from unittest.mock import MagicMock

import boto3
import pytest
from moto import mock_aws


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
_CHAT_DIR = _LAMBDA_ROOT / "chat"
for _p in (str(_LAMBDA_ROOT), str(_CHAT_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


SYMBOL_GRAPH_TABLE = "test-augment-symbol-graph"
CODE_MAPS_TABLE = "test-augment-code-maps"
WORKSPACE_ID = "ws-augment"
REPO = "owner/repo"
SHA = "sha-augment-1"


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)
    monkeypatch.setenv("AUDIT_TABLE", "")
    # The kill-switch name is standardized. Tests exercise the canonical
    # var by default and a dedicated backward-compat test below pins the
    # legacy fallback behavior.
    monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "true")
    monkeypatch.delenv("GRAPH_AUGMENT_ENABLED", raising=False)
    yield


@pytest.fixture(autouse=True)
def _inject_clients():
    """Wire the reader + router to the moto-backed clients used by
    this file. Both modules expose the same _set_*_for_tests pattern.
    """
    from shared import symbol_graph_pointer  # type: ignore
    from shared import symbol_graph_reader  # type: ignore
    import router  # type: ignore

    real_resource = boto3.resource("dynamodb", region_name="us-west-2")

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_reader._set_resource_for_tests(real_resource)
    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_pointer._set_resource_for_tests(real_resource)

    # Router uses a stubbed Bedrock client -- it shouldn't talk to
    # Bedrock in unit tests. The default response classifies as
    # "structural" with the symbol pulled out of the question.
    bedrock_stub = MagicMock()
    router._reset_clients_for_tests()
    router._set_bedrock_client_for_tests(bedrock_stub)

    yield bedrock_stub

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_pointer._reset_clients_for_tests()
    router._reset_clients_for_tests()


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


def _put_fact(
    *,
    workspace_id: str,
    repo: str,
    commit_sha: str,
    kind: str,
    source_symbol_id: str,
    target_symbol_id: str,
    file_path: str,
    line: int,
    language: str = "python",
    gsi2: bool = True,
) -> None:
    table = boto3.resource("dynamodb", region_name="us-west-2").Table(
        SYMBOL_GRAPH_TABLE
    )
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

    # GSI3 (target-name-index) — derivation matches the writer.
    s = target_symbol_id
    if ":" in s and "::" not in s.split(":", 1)[0]:
        s = s.split(":", 1)[1]
    for sep in ("::", ".", "/"):
        if sep in s:
            s = s.rsplit(sep, 1)[-1]
    if s:
        item["bare_name"] = s
        item["GSI3PK"] = f"{workspace_id}#{s}"
        item["GSI3SK"] = (
            f"{target_symbol_id}#{kind}#{repo}#{file_path}#{line}"
        )
    table.put_item(Item=item)


def _bedrock_response(intent: str, symbol):
    import json

    text = json.dumps({"intent": intent, "symbol": symbol})
    return {
        "output": {
            "message": {"content": [{"text": text}]},
        },
        "usage": {"inputTokens": 50, "outputTokens": 10},
    }


def _import_augment():
    """Import the chat handler lazily so its module-level boto3
    references resolve under mock_aws.
    """
    # The chat handler does ``sys.path.insert(0, lambda_dir)`` at
    # import time so its ``from shared.audit import ...`` resolves --
    # we just need to point at the chat dir on sys.path. Handler imports
    # boto3 lazily so under mock_aws this is safe.
    import importlib.util

    handler_path = _CHAT_DIR / "handler.py"
    spec = importlib.util.spec_from_file_location(
        "chat_handler_under_test", str(handler_path)
    )
    handler_module = importlib.util.module_from_spec(spec)
    sys.modules["chat_handler_under_test"] = handler_module
    spec.loader.exec_module(handler_module)
    return handler_module


# ===========================================================================
# report_repos_snapshot integration -- 9 known callers
# ===========================================================================


def test_augment_returns_nine_callers_for_report_repos_snapshot(_inject_clients):
    """Plan-gkg/05 DoD: end-to-end question -> structural block with
    9 callers of report_repos_snapshot.

    Seeds the 9 license-layer v2.0.0 caller sites (project memory
    project_license_layer_v2.md), stubs the router to return
    ``structural`` + the bare symbol name, and asserts the rendered
    block contains every expected file path + line.
    """
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "report_repos_snapshot"
    )

    target = "python:lambda.shared.outcomeops_license::report_repos_snapshot"

    # Definition site so the block carries a "Defined at" line.
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.shared.outcomeops_license",
        target_symbol_id=target,
        file_path="lambda/shared/outcomeops_license.py",
        line=142,
    )

    # The 9 callers wired in license-layer v2.0.0 (per the project
    # memory: 9 *-integration connect/disconnect handlers + the
    # hourly heartbeat scheduler). Same fixtures as
    # test_symbol_graph_reader.test_callers_report_repos_snapshot_*.
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

    for name, file_path, line in known_callers:
        _put_fact(
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="calls",
            source_symbol_id=f"python:lambda.{name.replace('-', '_')}.handler::lambda_handler",
            target_symbol_id=target,
            file_path=file_path,
            line=line,
        )

    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="Which Lambda functions call `report_repos_snapshot`?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    decision = out["decision"]
    graph_block = out["graph_block"]

    assert decision is not None
    assert decision.intent == "structural"
    assert decision.symbol_candidate == "report_repos_snapshot"

    # The block must contain a non-empty caller list with every
    # known file path. Order doesn't matter -- the renderer sorts.
    assert "Direct callers (9 sites" in graph_block
    for _, file_path, line in known_callers:
        assert f"{file_path}:{line}" in graph_block, (
            f"missing {file_path}:{line} from block: {graph_block!r}"
        )
    # Definition anchor came through too.
    assert "Defined at: lambda/shared/outcomeops_license.py:142" in graph_block


# ===========================================================================
# Negative-case integration tests
# ===========================================================================


def test_augment_pattern_intent_skips_graph_query(_inject_clients):
    """Pattern intent must NOT fire the graph query (routing decision
    table: pattern = RAG only)."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response("pattern", None)

    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="What are our Python standards?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"].intent == "pattern"
    assert out["graph_block"] == ""


def test_augment_no_workspace_skips_router_and_graph(_inject_clients):
    """No workspace = no graph anchor; the augmentation path early-
    returns without firing Bedrock."""
    bedrock_stub = _inject_clients
    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="Who calls X?",
        workspace_ids=None,
        user_id="user@x.com",
    )
    assert out["decision"] is None
    assert out["graph_block"] == ""
    bedrock_stub.converse.assert_not_called()


def test_augment_kill_switch_disables_augmentation(monkeypatch, _inject_clients):
    """GRAPH_RETRIEVAL_ENABLED=false bypasses router + reader entirely
    so an operator can disable in the field without redeploy."""
    monkeypatch.setenv("GRAPH_RETRIEVAL_ENABLED", "false")
    bedrock_stub = _inject_clients
    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="Who calls report_repos_snapshot?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"] is None
    assert out["graph_block"] == ""


def test_augment_kill_switch_legacy_var_still_honored(
    monkeypatch, _inject_clients
):
    """Backward-compat: when GRAPH_RETRIEVAL_ENABLED is unset but the
    legacy GRAPH_AUGMENT_ENABLED=false is set, augmentation stays off.
    Lets operators on older deploys roll forward without touching
    SSM."""
    monkeypatch.delenv("GRAPH_RETRIEVAL_ENABLED", raising=False)
    monkeypatch.setenv("GRAPH_AUGMENT_ENABLED", "false")
    bedrock_stub = _inject_clients
    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="Who calls report_repos_snapshot?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"] is None
    assert out["graph_block"] == ""
    bedrock_stub.converse.assert_not_called()


def test_augment_structural_with_zero_callers_renders_empty_block(_inject_clients):
    """Empty graph result must render an explicit 'zero callers'
    block ('Empty graph results' -- audit unlock)."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "never_called"
    )

    handler = _import_augment()

    out = handler._augment_context_with_symbol_graph(
        question="Who calls never_called?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"].intent == "structural"
    assert "Direct callers in this workspace: 0" in out["graph_block"]


def test_augment_importer_routes_on_subpath_symbol(_inject_clients):
    """A slash-shaped symbol switches the query from callers() to
    importers() -- the Hyatt proof case."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "@hyatt-hpe/hpe-utils/http2"
    )

    # Seed one importer.
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="imports",
        source_symbol_id="typescript:apps.app1::index",
        target_symbol_id="@hyatt-hpe/hpe-utils/http2",
        file_path="apps/app1/index.ts",
        line=7,
        language="typescript",
    )

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="Which applications use `@hyatt-hpe/hpe-utils/http2`?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert "Importers (1 site" in out["graph_block"]
    assert "apps/app1/index.ts:7" in out["graph_block"]
