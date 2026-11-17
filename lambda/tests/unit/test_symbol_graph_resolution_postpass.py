# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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


# =============================================================================
# ADR-056: cross-repo resolution via the workspace-wide definitions index.
# =============================================================================

_WS_TABLE_XR = "test-workspaces-xr"
_WS_XR = "ws-xrepo"


def _create_workspaces_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=_WS_TABLE_XR,
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
    tab = boto3.resource("dynamodb", region_name="us-west-2").Table(_WS_TABLE_XR)
    for owner_repo in ("bcarpio/producer-xr", "bcarpio/consumer-xr"):
        tab.put_item(Item={
            "PK": f"WS#{_WS_XR}", "SK": f"GITHUB_REPO#{owner_repo}",
            "workspace_id": _WS_XR,
        })


def _seed_xr():
    from shared.symbol_graph_writer import write_facts  # type: ignore
    from extractor.base import Fact  # type: ignore

    write_facts(
        workspace_id=_WS_XR, repo="producer-xr", commit_sha="sha-prod",
        facts=[Fact("defines", "python:shared_lib.math_utils::<module>",
                    "python:shared_lib.math_utils::add", "shared_lib/math_utils.py",
                    4, "python")],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE, skip_pointer_swap=True,
    )
    write_facts(
        workspace_id=_WS_XR, repo="consumer-xr", commit_sha="sha-cons",
        facts=[
            Fact("defines", "python:app.main::<module>",
                 "python:app.main::compute_total", "app/main.py", 7, "python"),
            Fact("imports", "python:app.main::<module>",
                 "python:shared_lib.math_utils::add", "app/main.py", 4, "python"),
            Fact("calls", "python:app.main::compute_total",
                 "python:app.main::add", "app/main.py", 8, "python"),
        ],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE, skip_pointer_swap=True,
    )


class TestCrossRepoPostPass:
    def test_consumer_call_rebinds_to_producer_definition(self, monkeypatch):
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

        _create_table()
        _create_workspaces_table()
        handler = _load_handler()
        # Bind the workspaces-table read to THIS moto context.
        monkeypatch.setattr(
            handler, "dynamodb_resource",
            boto3.resource("dynamodb", region_name="us-west-2"),
        )
        monkeypatch.setenv("WORKSPACES_TABLE", _WS_TABLE_XR)
        _seed_xr()

        # Pre: the consumer's call targets its OWN namespace (within-repo raw).
        pre = scan_repo_facts(_WS_XR, "consumer-xr",
                              symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        call = next(r for r in pre if r["kind"] == "calls")
        assert call["target_symbol_id"] == "python:app.main::add"

        # Run the post-pass on the CONSUMER; it loads the producer's defines.
        handler._resolve_repo_symbol_graph(_WS_XR, "consumer-xr", "sha-cons")

        # Post: the call now targets the PRODUCER's definition (cross-repo).
        post = scan_repo_facts(_WS_XR, "consumer-xr",
                               symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        call = next(r for r in post if r["kind"] == "calls")
        assert call["target_symbol_id"] == "python:shared_lib.math_utils::add"

    def test_list_workspace_repos_strips_owner_and_excludes_self(self, monkeypatch):
        _create_workspaces_table()
        handler = _load_handler()
        monkeypatch.setattr(
            handler, "dynamodb_resource",
            boto3.resource("dynamodb", region_name="us-west-2"),
        )
        monkeypatch.setenv("WORKSPACES_TABLE", _WS_TABLE_XR)
        repos = handler._list_workspace_repos(_WS_XR, exclude_bare="consumer-xr")
        assert repos == ["producer-xr"]  # owner stripped, self excluded


# =============================================================================
# ADR-056 v2: producer-push re-resolution. When a producer finishes a regen,
# the repos that IMPORT it are re-resolved so their cross-repo bindings refresh
# without a consumer re-ingest. Importer-narrowed via the target GSI; reuses the
# v1 post-pass per consumer.
# =============================================================================


class TestProducerPushReresolve:
    def _setup(self, monkeypatch):
        _create_table()
        _create_workspaces_table()
        handler = _load_handler()
        monkeypatch.setattr(
            handler, "dynamodb_resource",
            boto3.resource("dynamodb", region_name="us-west-2"),
        )
        monkeypatch.setenv("WORKSPACES_TABLE", _WS_TABLE_XR)
        return handler

    def test_find_importer_repos_returns_consumer(self, monkeypatch):
        # The producer's def is imported by consumer-xr; the GSI-narrowed
        # importer lookup must surface exactly that repo (producer excluded).
        handler = self._setup(monkeypatch)
        _seed_xr()
        assert handler._find_importer_repos(_WS_XR, "producer-xr") == [
            (_WS_XR, "consumer-xr")
        ]

    def test_producer_push_rebinds_importer_consumer(self, monkeypatch):
        # Headline: the consumer's cross-repo call is RAW (ordering gap -- it
        # was ingested before the producer's defines existed). A producer-push
        # message re-resolves the importer consumer and binds the call to the
        # producer's definition WITHOUT re-resolving the consumer directly.
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

        handler = self._setup(monkeypatch)
        _seed_xr()

        pre = scan_repo_facts(_WS_XR, "consumer-xr",
                              symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        assert next(r for r in pre if r["kind"] == "calls")[
            "target_symbol_id"] == "python:app.main::add"

        handler.handle_xrepo_reresolve_message({
            "message_type": "xrepo_reresolve",
            "workspace_id": _WS_XR,
            "repo": "producer-xr",
            "commit_sha": "sha-prod",
        })

        post = scan_repo_facts(_WS_XR, "consumer-xr",
                               symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        assert next(r for r in post if r["kind"] == "calls")[
            "target_symbol_id"] == "python:shared_lib.math_utils::add"

    def test_producer_push_is_idempotent(self, monkeypatch):
        # Re-running the push after the consumer is already bound rewrites
        # nothing (bound == target -> no remap). Proves Case B idempotency.
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

        handler = self._setup(monkeypatch)
        _seed_xr()
        msg = {
            "message_type": "xrepo_reresolve", "workspace_id": _WS_XR,
            "repo": "producer-xr", "commit_sha": "sha-prod",
        }
        handler.handle_xrepo_reresolve_message(msg)
        first = scan_repo_facts(_WS_XR, "consumer-xr",
                                symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        handler.handle_xrepo_reresolve_message(msg)
        second = scan_repo_facts(_WS_XR, "consumer-xr",
                                 symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        # Same SKs, same targets -- no churn on a stable workspace.
        assert sorted((r["SK"], r["target_symbol_id"]) for r in first) == \
            sorted((r["SK"], r["target_symbol_id"]) for r in second)

    def test_producer_with_no_importers_is_noop(self, monkeypatch):
        # A producer nobody imports yields an empty importer set and does no
        # work -- the cold-producer path.
        from shared.symbol_graph_writer import write_facts  # type: ignore
        from extractor.base import Fact  # type: ignore

        handler = self._setup(monkeypatch)
        write_facts(
            workspace_id=_WS_XR, repo="producer-xr", commit_sha="sha-prod",
            facts=[Fact("defines", "python:shared_lib.math_utils::<module>",
                        "python:shared_lib.math_utils::add",
                        "shared_lib/math_utils.py", 4, "python")],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE, skip_pointer_swap=True,
        )
        assert handler._find_importer_repos(_WS_XR, "producer-xr") == []
        # Must not raise.
        handler.handle_xrepo_reresolve_message({
            "message_type": "xrepo_reresolve", "workspace_id": _WS_XR,
            "repo": "producer-xr", "commit_sha": "sha-prod",
        })

    def test_missing_workspace_or_repo_drops(self, monkeypatch):
        handler = self._setup(monkeypatch)
        # No raise, no work: guard clause on missing identifiers.
        handler.handle_xrepo_reresolve_message({"message_type": "xrepo_reresolve"})

    def test_enqueue_sends_message_to_code_maps_queue(self, monkeypatch):
        handler = self._setup(monkeypatch)
        sent = {}

        class _FakeSQS:
            def send_message(self, **kwargs):
                sent.update(kwargs)

        monkeypatch.setattr(handler, "sqs_client", _FakeSQS())
        monkeypatch.setattr(handler, "CODE_MAPS_QUEUE_URL", "https://sqs/test-q")
        handler._enqueue_xrepo_reresolve(_WS_XR, "producer-xr", "sha-prod")

        import json as _json
        assert sent["QueueUrl"] == "https://sqs/test-q"
        body = _json.loads(sent["MessageBody"])
        assert body == {
            "message_type": "xrepo_reresolve",
            "workspace_id": _WS_XR,
            "repo": "producer-xr",
            "commit_sha": "sha-prod",
        }

    def test_enqueue_without_queue_url_is_noop(self, monkeypatch):
        handler = self._setup(monkeypatch)

        class _FakeSQS:
            def send_message(self, **kwargs):  # pragma: no cover - must not run
                raise AssertionError("send_message called with no queue URL")

        monkeypatch.setattr(handler, "sqs_client", _FakeSQS())
        monkeypatch.setattr(handler, "CODE_MAPS_QUEUE_URL", "")
        handler._enqueue_xrepo_reresolve(_WS_XR, "producer-xr", "sha-prod")

    def test_record_routing_dispatches_to_handler(self, monkeypatch):
        # process_batch_record must route an xrepo_reresolve message to the
        # producer-push handler and return before the shard path.
        import json as _json

        handler = self._setup(monkeypatch)
        called = {}
        monkeypatch.setattr(
            handler, "handle_xrepo_reresolve_message",
            lambda batch: called.setdefault("batch", batch),
        )
        handler.process_batch_record({"body": _json.dumps({
            "message_type": "xrepo_reresolve",
            "workspace_id": _WS_XR, "repo": "producer-xr", "commit_sha": "s",
        })})
        assert called["batch"]["repo"] == "producer-xr"


# =============================================================================
# ADR-056 scope extension: cross-repo resolution across is_global and granted
# workspaces (not just the repo's own workspace).
# =============================================================================

_XWS_TABLE = "test-workspaces-xws"
# A = consumer workspace, G = global, B = grantor (grants A), C = unshared.
_A, _G, _B, _C = "ws-a", "ws-global", "ws-b", "ws-c"


def _create_xws_workspaces_table():
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=_XWS_TABLE,
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
    tab = boto3.resource("dynamodb", region_name="us-west-2").Table(_XWS_TABLE)
    # METADATA rows: G is global; B grants A; A and C are plain.
    tab.put_item(Item={"PK": f"WS#{_A}", "SK": "METADATA", "workspace_id": _A})
    tab.put_item(Item={"PK": f"WS#{_G}", "SK": "METADATA", "workspace_id": _G,
                       "is_global": True})
    tab.put_item(Item={"PK": f"WS#{_B}", "SK": "METADATA", "workspace_id": _B,
                       "search_allowed_workspaces": [_A]})
    tab.put_item(Item={"PK": f"WS#{_C}", "SK": "METADATA", "workspace_id": _C})
    # Repo rows per workspace (SK=GITHUB_REPO#owner/repo).
    for ws, repo in ((_A, "consumer-ws"), (_G, "global-lib"),
                     (_B, "granted-lib"), (_C, "unshared-lib")):
        tab.put_item(Item={"PK": f"WS#{ws}", "SK": f"GITHUB_REPO#bcarpio/{repo}",
                           "workspace_id": ws})


def _seed_xws():
    from shared.symbol_graph_writer import write_facts  # type: ignore
    from extractor.base import Fact  # type: ignore

    def defines(ws, repo, module, name, fp):
        write_facts(
            workspace_id=ws, repo=repo, commit_sha=f"sha-{repo}",
            facts=[Fact("defines", f"python:{module}::<module>",
                        f"python:{module}::{name}", fp, 2, "python")],
            symbol_graph_table_name=SYMBOL_GRAPH_TABLE, skip_pointer_swap=True,
        )

    defines(_G, "global-lib", "global_mod", "gfunc", "global_mod.py")
    defines(_B, "granted-lib", "granted_mod", "tfunc", "granted_mod.py")
    defines(_C, "unshared-lib", "unshared_mod", "ufunc", "unshared_mod.py")

    # Consumer in A: imports + raw calls to all three producers' symbols.
    write_facts(
        workspace_id=_A, repo="consumer-ws", commit_sha="sha-consumer-ws",
        facts=[
            Fact("defines", "python:app.main::<module>",
                 "python:app.main::use", "app/main.py", 9, "python"),
            Fact("imports", "python:app.main::<module>",
                 "python:global_mod::gfunc", "app/main.py", 1, "python"),
            Fact("imports", "python:app.main::<module>",
                 "python:granted_mod::tfunc", "app/main.py", 2, "python"),
            Fact("imports", "python:app.main::<module>",
                 "python:unshared_mod::ufunc", "app/main.py", 3, "python"),
            Fact("calls", "python:app.main::use",
                 "python:app.main::gfunc", "app/main.py", 10, "python"),
            Fact("calls", "python:app.main::use",
                 "python:app.main::tfunc", "app/main.py", 11, "python"),
            Fact("calls", "python:app.main::use",
                 "python:app.main::ufunc", "app/main.py", 12, "python"),
        ],
        symbol_graph_table_name=SYMBOL_GRAPH_TABLE, skip_pointer_swap=True,
    )


class TestCrossWorkspaceScopeResolution:
    def _setup(self, monkeypatch):
        _create_table()
        _create_xws_workspaces_table()
        handler = _load_handler()
        monkeypatch.setattr(
            handler, "dynamodb_resource",
            boto3.resource("dynamodb", region_name="us-west-2"),
        )
        monkeypatch.setenv("WORKSPACES_TABLE", _XWS_TABLE)
        _seed_xws()
        return handler

    def _calls(self):
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore
        rows = scan_repo_facts(_A, "consumer-ws",
                               symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
        return {r["source_symbol_id"].split("::")[-1] + "->" +
                r["target_symbol_id"]: r for r in rows if r["kind"] == "calls"}

    def test_global_and_granted_bind_unshared_stays_raw(self, monkeypatch):
        handler = self._setup(monkeypatch)
        handler._resolve_repo_symbol_graph(_A, "consumer-ws", "sha-consumer-ws")
        targets = {r["target_symbol_id"]
                   for r in self._calls().values()}
        # global (G in scope) and granted (B granted A) bind cross-workspace;
        # unshared (C not in scope) stays raw in the consumer namespace.
        assert "python:global_mod::gfunc" in targets
        assert "python:granted_mod::tfunc" in targets
        assert "python:app.main::ufunc" in targets          # raw, unbound
        assert "python:unshared_mod::ufunc" not in targets   # never crossed to C

    def test_scope_pairs_exclude_self_and_unshared(self, monkeypatch):
        handler = self._setup(monkeypatch)
        pairs = set(handler._resolve_definition_scope_pairs(_A, "consumer-ws"))
        assert (_G, "global-lib") in pairs
        assert (_B, "granted-lib") in pairs
        assert (_C, "unshared-lib") not in pairs     # C not in scope
        assert (_A, "consumer-ws") not in pairs       # self excluded

    def test_scope_failure_falls_back_to_own_workspace(self, monkeypatch):
        handler = self._setup(monkeypatch)
        import shared.pr_check_scope as scope_mod
        monkeypatch.setattr(scope_mod, "list_global_workspace_ids",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        # Must not raise; falls back to own-workspace siblings only.
        pairs = handler._resolve_definition_scope_pairs(_A, "consumer-ws")
        assert all(ws == _A for ws, _ in pairs)

    def test_global_producer_fan_out_finds_cross_workspace_consumer(self, monkeypatch):
        handler = self._setup(monkeypatch)
        # Producer global-lib is in the is_global workspace G; its consumer lives
        # in workspace A. Fan-out (all workspaces, since G is global) finds it.
        assert handler._find_importer_repos(_G, "global-lib") == [
            (_A, "consumer-ws")
        ]

    def test_granted_producer_fan_out_finds_consumer(self, monkeypatch):
        handler = self._setup(monkeypatch)
        # granted-lib is in B, which granted A; fan-out = B + B's grants {A}.
        assert handler._find_importer_repos(_B, "granted-lib") == [
            (_A, "consumer-ws")
        ]

    def test_unshared_producer_has_no_cross_workspace_fan_out(self, monkeypatch):
        handler = self._setup(monkeypatch)
        # unshared-lib is in C, which is neither global nor grants anyone; its
        # consumer in A is NOT re-resolved (consent gating in the fan-out).
        assert handler._find_importer_repos(_C, "unshared-lib") == []

    def test_global_producer_push_rebinds_cross_workspace(self, monkeypatch):
        from shared.symbol_graph_writer import scan_repo_facts  # type: ignore

        handler = self._setup(monkeypatch)
        # The consumer's calls are raw. A producer-push from the GLOBAL producer
        # re-resolves the cross-workspace consumer and binds the global symbol.
        handler.handle_xrepo_reresolve_message({
            "message_type": "xrepo_reresolve",
            "workspace_id": _G, "repo": "global-lib", "commit_sha": "sha-global-lib",
        })
        targets = {r["target_symbol_id"] for r in scan_repo_facts(
            _A, "consumer-ws", symbol_graph_table_name=SYMBOL_GRAPH_TABLE)
            if r["kind"] == "calls"}
        assert "python:global_mod::gfunc" in targets   # rebound cross-workspace
        assert "python:granted_mod::tfunc" in targets  # granted also in scope
        assert "python:app.main::ufunc" in targets      # unshared stays raw
