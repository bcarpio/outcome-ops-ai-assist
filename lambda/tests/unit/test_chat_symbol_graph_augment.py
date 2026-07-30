"""End-to-end test of ``chat.handler._augment_context_with_symbol_graph``
against a moto-backed symbol-graph table.

This is the chat-augmentation "Definition of done" gate:

> ``report_repos_snapshot`` and ``@acme-corp/shared-utils/http2`` queries
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

    # Router uses shared.model_client.invoke (ADR-035 chunk 11). To
    # avoid talking to Bedrock in unit tests, we patch
    # chat.router.invoke_model and route through a MagicMock that
    # preserves the legacy .converse() surface so existing call_args /
    # call_count assertions still work.
    from shared.model_client_types import Response, StopReason, Usage
    from unittest.mock import patch

    bedrock_stub = MagicMock()

    def _invoke_shim(**kwargs):
        converse_kwargs = {
            "modelId": kwargs.get("model_id"),
            "system": ([{"text": s} for s in (kwargs["system"] if isinstance(kwargs.get("system"), list) else [kwargs["system"]] if kwargs.get("system") else [])]),
            "messages": kwargs.get("messages"),
            "inferenceConfig": {
                "maxTokens": kwargs.get("max_tokens"),
                "temperature": kwargs.get("temperature"),
            },
        }
        raw = bedrock_stub.converse(**converse_kwargs)
        content = raw.get("output", {}).get("message", {}).get("content", [])
        text = content[0].get("text", "") if content else ""
        usage = raw.get("usage", {})
        return Response(
            text=text,
            content_blocks=content,
            usage=Usage(
                input_tokens=usage.get("inputTokens", 0),
                output_tokens=usage.get("outputTokens", 0),
            ),
            stop_reason=StopReason.END_TURN,
        )

    with patch.object(router, "invoke_model", side_effect=_invoke_shim):
        yield bedrock_stub

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_pointer._reset_clients_for_tests()


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
    # Graph fired — no skip reason.
    assert out["graph_skip_reason"] is None


def test_augment_structural_no_symbol_sets_no_symbol_reason(_inject_clients):
    """STRUCTURAL intent but the router extracted no symbol: graph can't
    anchor, so it's a skip with reason 'no_symbol' (and a '(none
    extracted)' block is rendered so the model says so)."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response("structural", None)

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="How is auth structured here?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"].intent == "structural"
    assert out["graph_skip_reason"] == "no_symbol"


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
    assert out["graph_skip_reason"] == "intent_pattern"


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
    assert out["graph_skip_reason"] == "no_workspace"
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
    assert out["graph_skip_reason"] == "kill_switch"


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
    assert out["graph_skip_reason"] == "kill_switch"
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
    # Graph fired (returned 0) — that's an empty result, not a skip.
    assert out["graph_skip_reason"] is None


def test_augment_multi_canonical_runs_callers_per_canonical_and_merges(
    _inject_clients,
):
    """When fuzzy returns multiple canonicals (each Lambda defines its
    own local ``require_auth``) the augment must run callers() for
    every canonical and merge the results, not just render a
    disambiguation list of definitions. The model otherwise replies
    "I have definitions, not callers" to questions like "who calls
    require_auth?" — which is what surfaced on prd 2026-05-16.

    Sets the four telemetry fields the chat metadata frame propagates
    to the UI chip so the user can see "Graph: callers (N results)
    across M matches" instead of "Graph: skipped".
    """
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "require_auth"
    )

    # Two canonicals share the bare name -- the same shape as the
    # prd ingest where every integration Lambda has its own local
    # require_auth.
    auth_canonical = "python:lambda.auth.handler::require_auth"
    org_canonical = "python:lambda.org-management.handler::require_auth"

    # Defines for both canonicals.
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=auth_canonical,
        file_path="lambda/auth/handler.py",
        line=588,
        language="python",
    )
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.org-management.handler::<module>",
        target_symbol_id=org_canonical,
        file_path="lambda/org-management/handler.py",
        line=660,
        language="python",
    )

    # auth.handler::require_auth has 2 callers; org-management has 1.
    # Merged total = 3.
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:lambda.auth.handler::handle_invite",
        target_symbol_id=auth_canonical,
        file_path="lambda/auth/handler.py",
        line=1325,
        language="python",
    )
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:lambda.auth.handler::handle_workspace_invite",
        target_symbol_id=auth_canonical,
        file_path="lambda/auth/handler.py",
        line=1462,
        language="python",
    )
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:lambda.org-management.handler::lambda_handler",
        target_symbol_id=org_canonical,
        file_path="lambda/org-management/handler.py",
        line=5888,
        language="python",
    )

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="Which lambdas call require_auth?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )

    # The block must enumerate callers per canonical (this is the
    # functional fix on top of the telemetry fix).
    assert "lambda/auth/handler.py:1325" in out["graph_block"]
    assert "lambda/auth/handler.py:1462" in out["graph_block"]
    assert "lambda/org-management/handler.py:5888" in out["graph_block"]
    # And the per-canonical headers so the model knows which canonical
    # owns which callers.
    assert auth_canonical in out["graph_block"]
    assert org_canonical in out["graph_block"]
    # Regression guard (2026-06-02 audit): the renderer must NOT emit
    # an aggregate "across all matches" or "N distinct definitions"
    # summary line. Those numbers (CANONICAL count mislabeled as
    # definitions, retrieval total mislabeled as "call sites") were
    # the renderer-authored prose the LLM parroted as wrong claims
    # ("40 distinct definitions / 94 direct call sites total" against
    # ground truth of 24 / 51). ADR-032: aggregates the LLM might
    # quote MUST come from per-tool fields (total_known) or not
    # appear at all.
    assert "across all matches" not in out["graph_block"], (
        f"Renderer regressed: aggregate summary line returned: "
        f"{out['graph_block']!r}"
    )
    assert "distinct definitions" not in out["graph_block"], (
        f"Renderer regressed: 'distinct definitions' aggregate returned"
    )
    # ADR-032 2026-06-04 follow-up: the labeled aggregates the model
    # was previously allowed to quote (CANONICALS_RESOLVED, TOTAL_
    # SITES_ACROSS_CANONICALS) were REMOVED from the renderer because
    # prd Haiku consistently quoted the numbers but dropped the
    # "(includes test files)" qualifier — a half-violation the rule
    # couldn't enforce at temp=0.1. The new contract: no aggregate
    # number reaches the model's context at all. The table conveys
    # everything; the reader sums the column or a UI surface renders
    # the headline number post-synthesis.
    assert "COMPUTED_AGGREGATES" not in out["graph_block"], (
        "Renderer must NOT emit COMPUTED_AGGREGATES per ADR-032 "
        "2026-06-04 follow-up. The labeled-aggregate scheme was "
        "removed after Haiku stripped the mandatory qualifier."
    )
    assert "CANONICALS_RESOLVED" not in out["graph_block"]
    assert "TOTAL_SITES_ACROSS_CANONICALS" not in out["graph_block"]
    assert "include test files" not in out["graph_block"]

    # Telemetry — now reports callers, not definitions.
    assert out["retrievals_fired"] == ["graph"]
    assert out["graph_relation"] == "callers"
    assert out["graph_result_count"] == 3
    paths = {(s["file_path"], s["line"]) for s in out["graph_sites"]}
    assert paths == {
        ("lambda/auth/handler.py", 1325),
        ("lambda/auth/handler.py", 1462),
        ("lambda/org-management/handler.py", 5888),
    }


def test_augment_importer_routes_on_subpath_symbol(_inject_clients):
    """A slash-shaped symbol switches the query from callers() to
    importers() -- the Acme proof case."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "@acme-corp/shared-utils/http2"
    )

    # Seed one importer.
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="imports",
        source_symbol_id="typescript:apps.app1::index",
        target_symbol_id="@acme-corp/shared-utils/http2",
        file_path="apps/app1/index.ts",
        line=7,
        language="typescript",
    )

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="Which applications use `@acme-corp/shared-utils/http2`?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert "Importers (1 site" in out["graph_block"]
    assert "apps/app1/index.ts:7" in out["graph_block"]


# ===========================================================================
# Chunk 02 — graph-fire backstop on a resolved symbol
# ===========================================================================


def test_augment_pattern_intent_with_resolved_symbol_fires_graph(_inject_clients):
    """Router says PATTERN, but the extracted symbol resolves in the
    graph — the backstop fires the graph anyway (resolution beats the
    intent guess)."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response("pattern", "require_auth")

    canonical = "python:lambda.auth.handler::require_auth"
    _put_fact(
        workspace_id=WORKSPACE_ID, repo=REPO, commit_sha=SHA, kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=canonical, file_path="lambda/auth/handler.py", line=588,
    )
    _put_fact(
        workspace_id=WORKSPACE_ID, repo=REPO, commit_sha=SHA, kind="calls",
        source_symbol_id="python:lambda.auth.handler::handle_invite",
        target_symbol_id=canonical, file_path="lambda/auth/handler.py", line=1325,
    )

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="Is require_auth implemented to our auth standard?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"].intent == "pattern"
    # Backstop fired: graph ran despite PATTERN intent.
    assert out["graph_skip_reason"] is None
    assert "backstop:resolved_symbol" in out["retrievals_fired"]
    assert "lambda/auth/handler.py:1325" in out["graph_block"]


def test_augment_pattern_intent_with_unresolved_symbol_skips_graph(_inject_clients):
    """Router says PATTERN and the extracted symbol does NOT resolve —
    trust the router and skip, with reason 'backstop_no_resolve'."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response("pattern", "nonexistent_sym")

    handler = _import_augment()
    out = handler._augment_context_with_symbol_graph(
        question="What are our auth standards for nonexistent_sym?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )
    assert out["decision"].intent == "pattern"
    assert out["graph_skip_reason"] == "backstop_no_resolve"
    assert out["graph_block"] == ""


# ===========================================================================
# Chunk 07 — multi-workspace fan-out
# ===========================================================================


def test_augment_fans_out_across_workspaces(_inject_clients):
    """Symbol defined only in a SECOND selected workspace (not the first)
    must still resolve — the graph fans out across all workspace_ids,
    not just workspace_ids[0]."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response("structural", "require_auth")

    ws_a = "ws-empty-first"
    ws_b = "ws-has-symbol"
    canonical = "python:lambda.auth.handler::require_auth"
    # Seed require_auth ONLY in ws_b.
    _put_fact(
        workspace_id=ws_b, repo=REPO, commit_sha=SHA, kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=canonical, file_path="lambda/auth/handler.py", line=588,
    )
    _put_fact(
        workspace_id=ws_b, repo=REPO, commit_sha=SHA, kind="calls",
        source_symbol_id="python:lambda.auth.handler::handle_invite",
        target_symbol_id=canonical, file_path="lambda/auth/handler.py", line=1325,
    )

    handler = _import_augment()
    # ws_a is FIRST and has no facts; pre-fan-out this returned 0.
    out = handler._augment_context_with_symbol_graph(
        question="Which lambdas call require_auth?",
        workspace_ids=[ws_a, ws_b],
        user_id="user@x.com",
    )
    assert out["graph_skip_reason"] is None
    assert out["graph_result_count"] == 1
    assert "lambda/auth/handler.py:1325" in out["graph_block"]


# ===========================================================================
# ADR-032 — Canonical-set truncation surfacing.
#
# The 2026-06-02 require_auth audit failure: chat router called
# fuzzy_resolve_by_name with no limit, default 25 clipped a 40-canonical
# set, 8 lambdas silently dropped from the relation fan-out. After the
# chunk-03 fix the router passes an explicit _FUZZY_RESOLVE_LIMIT (200)
# AND, when the resolver itself still truncates (workspace exceeds 200),
# the augment block must carry an explicit "NOTE: bare-name lookup was
# clipped..." disclosure so the chat synthesis prompt rule (chunk 04)
# can bind the LLM to surface the incompleteness rather than ship a
# confidently-partial enumeration.
# ===========================================================================


def test_augment_fuzzy_resolver_uses_explicit_limit_not_default(
    _inject_clients, monkeypatch
):
    """Regression guard: the chat fuzzy resolver MUST pass an explicit
    limit to the symbol-graph reader. Falling through to the reader's
    default (25) was the silent-truncation cause for require_auth."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "report_repos_snapshot"
    )

    canonical = (
        "python:lambda.shared.outcomeops_license::report_repos_snapshot"
    )
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.shared.outcomeops_license::<module>",
        target_symbol_id=canonical,
        file_path="lambda/shared/outcomeops_license.py",
        line=142,
    )

    handler = _import_augment()

    # The chat handler imports fuzzy_resolve_by_name lazily inside the
    # _fuzzy_resolve_symbol function, so the live symbol lives at
    # shared.symbol_graph_reader.fuzzy_resolve_by_name. Patch there.
    from shared import symbol_graph_reader  # type: ignore

    captured = {}
    real = symbol_graph_reader.fuzzy_resolve_by_name

    def _spy(*args, **kwargs):
        captured["limit"] = kwargs.get("limit")
        captured["args"] = args
        return real(*args, **kwargs)

    monkeypatch.setattr(
        symbol_graph_reader, "fuzzy_resolve_by_name", _spy
    )

    out = handler._augment_context_with_symbol_graph(
        question="Which Lambdas call `report_repos_snapshot`?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )

    # The resolver was invoked with an explicit limit (not None / not
    # the reader default 25). The exact value is the constant the
    # router exports so a future bump is visible.
    assert captured.get("limit") == handler._FUZZY_RESOLVE_LIMIT
    assert handler._FUZZY_RESOLVE_LIMIT >= 200
    # And the augment recorded the structured signal on `out` so the
    # downstream rendering can surface the cap if it's ever hit.
    assert out["graph_canonicals_limit"] == handler._FUZZY_RESOLVE_LIMIT
    assert out["graph_canonicals_truncated"] is False


def test_augment_canonical_truncation_prepends_disclosure_note(
    _inject_clients, monkeypatch
):
    """When fuzzy resolution itself was clipped, the augment block
    must prepend an explicit NOTE so the chat synthesis prompt rule
    (chunk 04) can bind on it. The note names the cap and tells the
    LLM the answer must surface the incompleteness."""
    _create_symbol_graph_table()
    _create_code_maps_table()

    bedrock_stub = _inject_clients
    bedrock_stub.converse.return_value = _bedrock_response(
        "structural", "require_auth"
    )

    # Seed one canonical + one calls fact so the augment has SOMETHING
    # to render around the disclosure.
    canonical = "python:lambda.auth.handler::require_auth"
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="defines",
        source_symbol_id="python:lambda.auth.handler::<module>",
        target_symbol_id=canonical,
        file_path="lambda/auth/handler.py",
        line=588,
    )
    _put_fact(
        workspace_id=WORKSPACE_ID,
        repo=REPO,
        commit_sha=SHA,
        kind="calls",
        source_symbol_id="python:lambda.auth.handler::handle_verify",
        target_symbol_id=canonical,
        file_path="lambda/auth/handler.py",
        line=1325,
    )

    handler = _import_augment()

    # Stub the fuzzy resolver to claim truncation -- can't easily seed
    # 201+ canonicals in moto, and this isolates the rendering contract.
    from shared.symbol_graph_reader import ResolutionResult  # type: ignore

    def _stub_truncated(*args, **kwargs):
        return ResolutionResult(
            canonicals=[canonical],
            truncated=True,
            total_known=None,
        )

    from shared import symbol_graph_reader  # type: ignore

    monkeypatch.setattr(
        symbol_graph_reader, "fuzzy_resolve_by_name", _stub_truncated
    )

    out = handler._augment_context_with_symbol_graph(
        question="Which Lambdas call `require_auth`?",
        workspace_ids=[WORKSPACE_ID],
        user_id="user@x.com",
    )

    assert out["graph_canonicals_truncated"] is True
    block = out["graph_block"]
    assert "NOTE: the bare-name lookup" in block
    assert str(handler._FUZZY_RESOLVE_LIMIT) in block
    assert "surface this incompleteness" in block
    # The actual rendered facts still appear -- the disclosure is
    # additive, not a replacement.
    assert "lambda/auth/handler.py:1325" in block
