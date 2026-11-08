"""Integration test for the ADR-055 whole-repo resolution post-pass.

``process-batch-summary``'s ``_resolve_repo_symbol_graph`` is the glue that
runs once at the last-close of a full regen: it scans every stored fact for a
repo, rebinds cross-module reference targets to their definition ids via the
registered per-language resolver, and rewrites only the changed edges in
DynamoDB. This drives the REAL extractor + REAL resolver + REAL writer through
moto so a cross-module Python call is proven to rebind end-to-end in the graph
(the thing Plane-1 BREAKING_CHANGES needs).
"""

from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import boto3
import pytest
from boto3.dynamodb.conditions import Key
from moto import mock_aws

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

_PBS_DIR = _LAMBDA_ROOT / "process-batch-summary"
_FIXTURE_DIR = _LAMBDA_ROOT / "tests" / "fixtures" / "extractor" / "python"

SYMBOL_GRAPH_TABLE = "test-symbol-graph-res"
WORKSPACE_ID = "ws-res"
REPO = "resolve-repo"
SHA = "sha-res-1"


@pytest.fixture(autouse=True)
def _aws_mocks():
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _reset_writer_clients():
    from shared import symbol_graph_writer  # type: ignore

    symbol_graph_writer._reset_clients_for_tests()
    symbol_graph_writer._set_resource_for_tests(
        boto3.resource("dynamodb", region_name="us-west-2")
    )
    yield
    symbol_graph_writer._reset_clients_for_tests()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    yield


def _create_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    attrs = [("PK", "S"), ("SK", "S"), ("GSI1PK", "S"), ("GSI1SK", "S"),
             ("GSI2PK", "S"), ("GSI2SK", "S"), ("GSI3PK", "S"), ("GSI3SK", "S")]
    client.create_table(
        TableName=SYMBOL_GRAPH_TABLE,
        AttributeDefinitions=[
            {"AttributeName": a, "AttributeType": t} for a, t in attrs
        ],
        KeySchema=[
            {"AttributeName": "PK", "KeyType": "HASH"},
            {"AttributeName": "SK", "KeyType": "RANGE"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": idx,
                "KeySchema": [
                    {"AttributeName": pk, "KeyType": "HASH"},
                    {"AttributeName": sk, "KeyType": "RANGE"},
                ],
                "Projection": {"ProjectionType": "ALL"},
            }
            for idx, pk, sk in [
                ("target-kind-index", "GSI1PK", "GSI1SK"),
                ("target-prefix-index", "GSI2PK", "GSI2SK"),
                ("target-name-index", "GSI3PK", "GSI3SK"),
            ]
        ],
        BillingMode="PAY_PER_REQUEST",
    )


_handler_module = None


def _load_handler():
    global _handler_module
    if _handler_module is None:
        spec = importlib.util.spec_from_file_location(
            "process_batch_summary_handler_res", str(_PBS_DIR / "handler.py")
        )
        mod = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = mod
        spec.loader.exec_module(mod)
        _handler_module = mod
    return _handler_module


def _extract(name):
    # The handler load inserts the process-batch-summary dir on sys.path, so
    # ``extractor`` resolves to the same package the resolver registered in.
    from extractor import extract_facts  # type: ignore

    return list(extract_facts(str(_FIXTURE_DIR / name), "python"))


def _seed_raw_facts():
    """Write the fixtures' RAW (unresolved) facts into the graph, exactly as
    the per-batch extractor would during a regen."""
    from shared.symbol_graph_writer import write_facts  # type: ignore

    facts = _extract("resolve_defs.py") + _extract("resolve_caller.py")
    write_facts(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        facts=facts,
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE,
        skip_pointer_swap=True,
    )
    return facts


def _scan_calls():
    from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

    rows = scan_repo_facts(
        WORKSPACE_ID, REPO, symbol_graph_table_name=SYMBOL_GRAPH_TABLE
    )
    return [r for r in rows if r["kind"] == "calls"]


class TestResolveRepoSymbolGraphPostPass:
    def test_cross_module_call_rebinds_in_dynamodb(self):
        _create_table()
        handler = _load_handler()
        _seed_raw_facts()

        # Pre-condition: the caller's call to target_fn points at the CALLER's
        # own module namespace (resolve_caller), never the definition.
        pre = _scan_calls()
        caller_call = next(
            r for r in pre
            if r["source_symbol_id"].endswith("::caller")
        )
        assert "resolve_caller" in caller_call["target_symbol_id"]
        assert "resolve_defs" not in caller_call["target_symbol_id"]

        # Run the post-pass.
        handler._resolve_repo_symbol_graph(WORKSPACE_ID, REPO, SHA)

        # Post-condition: the call now targets the target_fn DEFINITION in
        # resolve_defs; the row moved to the definition's GSI1 partition.
        post = _scan_calls()
        caller_call = next(
            r for r in post
            if r["source_symbol_id"].endswith("::caller")
        )
        assert caller_call["target_symbol_id"].endswith("::target_fn")
        assert "resolve_defs" in caller_call["target_symbol_id"]

        table = boto3.resource("dynamodb", region_name="us-west-2").Table(
            SYMBOL_GRAPH_TABLE
        )
        gsi = table.query(
            IndexName="target-kind-index",
            KeyConditionExpression=Key("GSI1PK").eq(
                f"{WORKSPACE_ID}#{caller_call['target_symbol_id']}"
            ),
        )
        # defines + calls both target target_fn's id now; at least the call edge.
        assert any(i["kind"] == "calls" for i in gsi["Items"])

    def test_unresolvable_call_left_raw(self):
        _create_table()
        handler = _load_handler()
        _seed_raw_facts()

        handler._resolve_repo_symbol_graph(WORKSPACE_ID, REPO, SHA)

        post = _scan_calls()
        undefined = next(
            r for r in post
            if r["target_symbol_id"].endswith("::undefined_helper")
        )
        # Never guessed -- still in the caller's namespace.
        assert "resolve_caller" in undefined["target_symbol_id"]

    def test_empty_graph_is_noop(self):
        _create_table()
        handler = _load_handler()
        # No facts seeded. Must not raise, must not write.
        handler._resolve_repo_symbol_graph(WORKSPACE_ID, REPO, SHA)
        assert _scan_calls() == []
