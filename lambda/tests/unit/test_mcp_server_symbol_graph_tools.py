"""Unit tests for the symbol-graph MCP tools on the OutcomeOps gateway.

See docs/mcp-server.md.

Covers the four tool handlers exposed by ``lambda/mcp-server/handler.py``:

- ``find_callers``         (wraps symbol_graph_reader.callers)
- ``find_importers``       (wraps symbol_graph_reader.importers)
- ``find_implementers``    (wraps symbol_graph_reader.implementers)
- ``find_definitions``     (wraps symbol_graph_reader.definitions)

Each tool is exercised in four states: empty result, single result,
truncation, and the ``SymbolGraphUnavailable`` error path. Workspace
authorization is asserted to refuse access when the caller's email
isn't a member of the requested workspace (mirrors
``handle_list_workspace_documents``). The audit row
``ACTION_MCP_TOOL_INVOKED`` is asserted via the gateway's
``_emit_mcp_tool_invoked`` helper.

Test isolation defence mirrors ``test_symbol_graph_reader.py``: a
fresh module load per file, autouse ``mock_aws``, and an autouse
fixture that resets the reader's cached boto3 resource AND injects
the test's moto-backed resource so the cross-suite
``sys.modules['boto3']`` MagicMock contamination documented in the
writer can't bleed in.
"""

from __future__ import annotations

# Defensive sys.modules cleanup at module import time -- see the reader
# test's comment about ``test_check_handlers_init.py:16`` planting a
# MagicMock into ``sys.modules['boto3']``. Clearing any prior cached
# shared.symbol_graph_reader keeps our autouse fixture in control.
import sys as _sys
for _name in list(_sys.modules):
    if _name.startswith("shared.symbol_graph_reader"):
        _sys.modules.pop(_name, None)

import importlib.util
import io
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws


# ---------------------------------------------------------------------------
# Constants + sys.path bootstrap
# ---------------------------------------------------------------------------

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

SYMBOL_GRAPH_TABLE = "test-mcp-symbol-graph"
CODE_MAPS_TABLE = "test-mcp-code-maps"
WORKSPACE_ID = "ws-mcp-test"
OTHER_WORKSPACE_ID = "ws-mcp-other"
REPO = "owner/repo"
SHA = "sha-mcp-test"
USER_EMAIL = "user@example.com"

# Required env so the mcp-server handler module loads.
os.environ["DYNAMODB_TABLE"] = "test-table"
os.environ["S3_VECTORS_BUCKET"] = "test-vectors-bucket"
os.environ["S3_VECTORS_INDEX"] = "test-vectors-index"
os.environ["KB_BUCKET"] = "test-kb-bucket"
os.environ["ENVIRONMENT"] = "test"
os.environ["MCP_BASE_URL"] = "http://test-alb.example.com"


# ---------------------------------------------------------------------------
# MCP module loader
# ---------------------------------------------------------------------------


def _load_mcp_module():
    """Load the MCP server handler module under a unique name."""
    mod_name = "mcp_server_handler_symbol_graph_tools"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../mcp-server/handler.py"
    )
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mcp_module = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mcp_module
    spec.loader.exec_module(mcp_module)
    return mcp_module


mcp_module = _load_mcp_module()


# ---------------------------------------------------------------------------
# Autouse fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    """No test may touch real AWS endpoints."""
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


@pytest.fixture(autouse=True)
def _reset_reader_clients_and_mcp_caches():
    """Inject this file's real boto3 resource into the reader + pointer;
    clear the mcp-server's in-memory caches between tests."""

    # Reset cached AWS clients on the mcp module itself
    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()

    from shared import symbol_graph_pointer  # type: ignore
    from shared import symbol_graph_reader  # type: ignore

    real_resource = boto3.resource("dynamodb", region_name="us-west-2")
    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_reader._set_resource_for_tests(real_resource)
    symbol_graph_pointer._reset_clients_for_tests()
    symbol_graph_pointer._set_resource_for_tests(real_resource)

    yield

    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_pointer._reset_clients_for_tests()

    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None
    mcp_module._mcp_server_cache.clear()
    mcp_module._mcp_sessions.clear()
    mcp_module._sessions.clear()


# ---------------------------------------------------------------------------
# DynamoDB table helpers (same shape as test_symbol_graph_reader.py)
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
    """Direct put_item -- mirrors the helper in test_symbol_graph_reader.py
    so the tool tests don't depend on the writer's public surface."""

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

    # GSI3 (target-name-index) — derivation matches the writer in
    # shared.symbol_graph_writer._derive_bare_name.
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


# ---------------------------------------------------------------------------
# Workspace-access stub: the handlers call get_user_workspace_ids; in
# unit tests we just patch that to return whatever the test sets up.
# ---------------------------------------------------------------------------


def _patch_user_workspaces(workspace_ids):
    return patch.object(
        mcp_module, "get_user_workspace_ids", return_value=workspace_ids
    )


# ===========================================================================
# find_callers
# ===========================================================================


class TestFindCallers:

    def test_empty_result(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {
                    "symbol_id": "python:nonexistent::sym",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["sites"] == []
        assert payload["truncated"] is False
        # No repos contributed -> no SHA / no staleness anchor.
        assert payload["graph_sha"] is None
        assert payload["staleness_seconds"] is None

    def test_single_result(self, tables):
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
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {"symbol_id": target, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 1
        site = payload["sites"][0]
        assert site["file_path"] == "lambda/github-integration/handler.py"
        assert site["line"] == 137
        assert site["repo"] == REPO
        assert site["language"] == "python"
        assert site["source_symbol_id"] == (
            "python:lambda.github_integration.handler::lambda_handler"
        )
        assert payload["truncated"] is False

    def test_truncation(self, tables):
        target = "python:lambda.shared.foo::bar"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        limit = 5
        for i in range(limit + 3):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="calls",
                source_symbol_id=f"python:caller{i}::fn",
                target_symbol_id=target,
                file_path=f"src/caller{i}.py",
                line=100 + i,
                language="python",
            )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {
                    "symbol_id": target,
                    "workspace_id": WORKSPACE_ID,
                    "limit": limit,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == limit
        assert payload["truncated"] is True

    def test_symbol_graph_unavailable(self, tables):
        """SymbolGraphUnavailable -> JSON-RPC error, NOT a raised
        exception (which would crash the gateway thread)."""
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "callers",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable("ddb down"),
            ):
                result = mcp_module.handle_find_callers(
                    {
                        "symbol_id": "python:any::sym",
                        "workspace_id": WORKSPACE_ID,
                    },
                    USER_EMAIL,
                )
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    def test_workspace_auth_denied(self, tables):
        """An API key whose authorized workspaces do NOT include the
        requested workspace_id gets refused -- not silently leaks
        data from another workspace."""
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {
                    "symbol_id": "python:any::sym",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        assert "error" in result
        assert "access" in result["error"].lower()
        # Critically -- no data leaked.
        assert "text" not in result


# ===========================================================================
# find_importers
# ===========================================================================


class TestFindImporters:

    def test_empty_result(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_importers(
                {
                    "target_or_prefix": "typescript:@unknown/pkg",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["sites"] == []
        assert payload["truncated"] is False

    def test_single_result_exact(self, tables):
        target = "typescript:@hyatt-hpe/hpe-utils/http2"
        _put_fact(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="imports",
            source_symbol_id="typescript:apps/app0/src/index::default",
            target_symbol_id=target,
            file_path="apps/app0/src/index.ts",
            line=1,
            language="typescript",
        )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_importers(
                {"target_or_prefix": target, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 1
        assert payload["sites"][0]["file_path"] == "apps/app0/src/index.ts"

    def test_truncation(self, tables):
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        # Use distinct subpaths so each row hits the prefix GSI cleanly.
        target_prefix = "typescript:@hyatt-hpe/hpe-utils"
        limit = 4
        for i in range(limit + 3):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="imports",
                source_symbol_id=f"typescript:apps/app{i}/src/index::default",
                target_symbol_id=f"{target_prefix}/sub{i}",
                file_path=f"apps/app{i}/src/index.ts",
                line=1,
                language="typescript",
            )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_importers(
                {
                    "target_or_prefix": target_prefix,
                    "workspace_id": WORKSPACE_ID,
                    "limit": limit,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == limit
        assert payload["truncated"] is True

    def test_symbol_graph_unavailable(self, tables):
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "importers",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable("ddb down"),
            ):
                result = mcp_module.handle_find_importers(
                    {
                        "target_or_prefix": "ts:@x/y",
                        "workspace_id": WORKSPACE_ID,
                    },
                    USER_EMAIL,
                )
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    def test_workspace_auth_denied(self, tables):
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_importers(
                {
                    "target_or_prefix": "ts:@x/y",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        assert "error" in result
        assert "text" not in result


# ===========================================================================
# find_implementers
# ===========================================================================


class TestFindImplementers:

    def test_empty_result(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_implementers(
                {
                    "interface_id": "typescript:@org/types::INoOne",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["sites"] == []
        assert payload["truncated"] is False

    def test_single_result(self, tables):
        iface = "typescript:@org/types::IBookingClient"
        _put_fact(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            repo=REPO,
            commit_sha=SHA,
            kind="implements",
            source_symbol_id="typescript:apps/booking/src/client::BookingClient",
            target_symbol_id=iface,
            file_path="apps/booking/src/client.ts",
            line=22,
            language="typescript",
        )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_implementers(
                {"interface_id": iface, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 1
        assert payload["sites"][0]["source_symbol_id"] == (
            "typescript:apps/booking/src/client::BookingClient"
        )

    def test_truncation(self, tables):
        iface = "typescript:@org/types::IThing"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        limit = 3
        for i in range(limit + 2):
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="implements",
                source_symbol_id=f"typescript:apps/impl{i}/src::Impl{i}",
                target_symbol_id=iface,
                file_path=f"apps/impl{i}/src/index.ts",
                line=10 + i,
                language="typescript",
            )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_implementers(
                {
                    "interface_id": iface,
                    "workspace_id": WORKSPACE_ID,
                    "limit": limit,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == limit
        assert payload["truncated"] is True

    def test_symbol_graph_unavailable(self, tables):
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "implementers",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable("ddb down"),
            ):
                result = mcp_module.handle_find_implementers(
                    {
                        "interface_id": "ts:@org/types::IFoo",
                        "workspace_id": WORKSPACE_ID,
                    },
                    USER_EMAIL,
                )
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    def test_workspace_auth_denied(self, tables):
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_implementers(
                {
                    "interface_id": "ts:@org/types::IFoo",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        assert "error" in result
        assert "text" not in result


# ===========================================================================
# find_definitions
# ===========================================================================


class TestFindDefinitions:

    def test_empty_result(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {
                    "symbol": "python:nothing.here::nope",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["sites"] == []
        # definitions() returns list[CallSite] (not QueryResult);
        # the gateway normalises the JSON envelope so truncated/sha
        # are always present.
        assert payload["truncated"] is False
        assert payload["graph_sha"] is None
        assert payload["staleness_seconds"] is None

    def test_single_result(self, tables):
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
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"symbol": sym, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 1
        assert payload["sites"][0]["file_path"] == (
            "lambda/shared/outcomeops_license.py"
        )
        assert payload["sites"][0]["line"] == 42

    def test_signature_surfaces_when_persisted(self, tables):
        """A defines row with a stored signature comes back through the
        MCP envelope as a ``signature`` key on the site -- consumers
        (Claude Code, chat synthesis) read the verified shape directly
        instead of recalling it from source."""
        sym = "python:lambda.x.module::greet"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        table.put_item(Item={
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
        })
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"symbol": sym, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 1
        assert payload["sites"][0]["signature"] == {
            "params": [
                {"name": "name", "type": "str"},
                {"name": "loud", "type": "bool"},
            ],
            "returns": "str",
        }

    def test_signature_absent_when_row_has_none(self, tables):
        """A defines row predating signature extraction comes back with
        no ``signature`` key on the site -- consumers see absence rather
        than a fabricated empty value."""
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
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"symbol": sym, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert "signature" not in payload["sites"][0]

    def test_cross_repo_duplicates_surface(self, tables):
        """definitions(X) returns list[CallSite] so cross-repo duplicates
        all surface (chunk-04 open Q #2). The MCP tool MUST preserve
        that -- otherwise consumers can't reason about duplicate
        interface declarations."""
        sym = "typescript:@org/types::IBookingClient"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        for i, repo in enumerate([REPO, "owner/other-repo"]):
            _put_fact(
                table,
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
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"symbol": sym, "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert len(payload["sites"]) == 2
        repos = {s["repo"] for s in payload["sites"]}
        assert repos == {REPO, "owner/other-repo"}

    def test_symbol_graph_unavailable(self, tables):
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "definitions",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable("ddb down"),
            ):
                result = mcp_module.handle_find_definitions(
                    {"symbol": "py:x::y", "workspace_id": WORKSPACE_ID},
                    USER_EMAIL,
                )
        assert "error" in result
        assert "unavailable" in result["error"].lower()

    def test_workspace_auth_denied(self, tables):
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"symbol": "py:x::y", "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "text" not in result


# ===========================================================================
# Required-arg validation (no workspace_id / no symbol)
# ===========================================================================


class TestRequiredArgs:

    def test_find_callers_missing_symbol_id(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {"workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "symbol_id" in result["error"]

    def test_find_importers_missing_target(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_importers(
                {"workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "target" in result["error"].lower()

    def test_find_implementers_missing_interface_id(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_implementers(
                {"workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "interface_id" in result["error"]

    def test_find_definitions_missing_symbol(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_definitions(
                {"workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "symbol" in result["error"].lower()

    def test_find_callers_missing_workspace_id(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_callers(
                {"symbol_id": "py:x::y"},
                USER_EMAIL,
            )
        assert "error" in result


# ===========================================================================
# find_symbol_by_name — bare-name fuzzy resolution
# ===========================================================================


class TestFindSymbolByName:

    def test_empty_workspace_returns_empty_canonicals(self, tables):
        """No defines rows for the bare name -> empty list."""
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_by_name(
                {"bare_name": "nothing_here", "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload == {"canonicals": []}

    def test_overloaded_bare_name_returns_all_canonicals(self, tables):
        """Multiple Lambdas defining the same bare name -> every
        canonical surfaces. Matches the prd shape that motivated
        the chat-side fuzzy resolver."""
        auth = "python:lambda.auth.handler::require_auth"
        org = "python:lambda.org-management.handler::require_auth"
        table = tables.Table(SYMBOL_GRAPH_TABLE)
        for canonical, file_path, line in [
            (auth, "lambda/auth/handler.py", 588),
            (org, "lambda/org-management/handler.py", 660),
        ]:
            _put_fact(
                table,
                workspace_id=WORKSPACE_ID,
                repo=REPO,
                commit_sha=SHA,
                kind="defines",
                source_symbol_id=f"{canonical.rsplit('::', 1)[0]}::<module>",
                target_symbol_id=canonical,
                file_path=file_path,
                line=line,
                language="python",
            )

        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_by_name(
                {
                    "bare_name": "require_auth",
                    "workspace_id": WORKSPACE_ID,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert set(payload["canonicals"]) == {auth, org}

    def test_workspace_auth_denied(self, tables):
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_by_name(
                {"bare_name": "require_auth", "workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "text" not in result

    def test_missing_bare_name(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_by_name(
                {"workspace_id": WORKSPACE_ID},
                USER_EMAIL,
            )
        assert "error" in result
        assert "bare_name" in result["error"]

    def test_symbol_graph_unavailable(self, tables):
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "fuzzy_resolve_by_name",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable(
                    "ddb down"
                ),
            ):
                result = mcp_module.handle_find_symbol_by_name(
                    {
                        "bare_name": "require_auth",
                        "workspace_id": WORKSPACE_ID,
                    },
                    USER_EMAIL,
                )
        assert "error" in result
        assert "unavailable" in result["error"].lower()


# ===========================================================================
# tools/list advertises the five tools
# ===========================================================================


class TestToolsListAdvertisement:

    def test_all_tools_advertised(self):
        """``MCP_TOOLS`` must include every symbol-graph tool so
        external MCP clients can discover them via tools/list."""
        names = {t["name"] for t in mcp_module.MCP_TOOLS}
        assert {
            "find_callers",
            "find_importers",
            "find_implementers",
            "find_definitions",
            "find_symbol_by_name",
        }.issubset(names)

    def test_all_handlers_registered(self):
        """The tools/call dispatch table must contain a handler for each."""
        for name in (
            "find_callers",
            "find_importers",
            "find_implementers",
            "find_definitions",
            "find_symbol_by_name",
        ):
            assert name in mcp_module.TOOL_HANDLERS


# ===========================================================================
# End-to-end via _handle_tools_call: audit row emission
# ===========================================================================


def _make_handler(method="POST", path="/mcp/v1", body=None, headers=None):
    """Build a McpHandler instance without starting a real HTTP server.
    Same shape as ``_make_handler`` in test_mcp_server.py."""
    handler = mcp_module.McpHandler.__new__(mcp_module.McpHandler)
    handler.wfile = io.BytesIO()
    if body is not None:
        body_bytes = (
            json.dumps(body).encode("utf-8") if isinstance(body, dict) else body
        )
    else:
        body_bytes = b""
    handler.rfile = io.BytesIO(body_bytes)

    from http.client import HTTPMessage
    msg = HTTPMessage()
    if headers:
        for k, v in headers.items():
            msg[k] = v
    if body is not None:
        msg["Content-Length"] = str(len(body_bytes))
    handler.headers = msg

    handler.path = path
    handler._headers_buffer = []
    handler.request_version = "HTTP/1.1"
    handler._sent_status = []

    def mock_send_response(self, code, message=None):
        self._sent_status.append(code)

    def mock_send_header(self, keyword, value):
        self._headers_buffer.append((keyword, value))

    def mock_end_headers(self):
        pass

    handler.send_response = lambda code, msg=None: mock_send_response(handler, code, msg)
    handler.send_header = lambda k, v: mock_send_header(handler, k, v)
    handler.end_headers = lambda: mock_end_headers(handler)
    return handler


class TestAuditEmission:
    """Each symbol-graph tool invocation must emit
    ACTION_MCP_TOOL_INVOKED (via _emit_mcp_tool_invoked). The gateway
    already wires this in _handle_tools_call -- we assert it fires for
    each of the four new tool names."""

    def _setup_session(self):
        session_id = "test-session-symbol-graph"
        mcp_module._sessions[session_id] = {
            "email": USER_EMAIL,
            "last_active": time.time(),
        }
        return session_id

    @pytest.mark.parametrize(
        "tool_name,args",
        [
            (
                "find_callers",
                {"symbol_id": "py:x::y", "workspace_id": WORKSPACE_ID},
            ),
            (
                "find_importers",
                {
                    "target_or_prefix": "ts:@x/y",
                    "workspace_id": WORKSPACE_ID,
                },
            ),
            (
                "find_implementers",
                {"interface_id": "ts:@x::IFoo", "workspace_id": WORKSPACE_ID},
            ),
            (
                "find_definitions",
                {"symbol": "py:x::y", "workspace_id": WORKSPACE_ID},
            ),
        ],
    )
    def test_tool_invocation_emits_audit_row(self, tables, tool_name, args):
        """tools/call -> handler runs -> _emit_mcp_tool_invoked called
        with the tool name + success flag."""
        session_id = self._setup_session()

        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        }
        handler = _make_handler(
            path="/mcp/v1",
            body=body,
            headers={"Mcp-Session-Id": session_id},
        )

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                mcp_module, "_emit_mcp_tool_invoked"
            ) as mock_emit:
                handler.do_POST()

        assert mock_emit.called, (
            f"Expected _emit_mcp_tool_invoked to be called for {tool_name}"
        )
        # First positional arg is the tool name; second is server name
        # ("outcomeops-native" for native handlers).
        call = mock_emit.call_args
        # call.args may be empty if everything was keyword -- inspect both.
        observed_tool = (
            call.args[0]
            if call.args
            else call.kwargs.get("tool_name")
        )
        assert observed_tool == tool_name


# ===========================================================================
# find_symbol_at_line: line citation grounding (ADR-031)
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
) -> None:
    """Seed defines row using the LIVE v2 base-table PK
    (workspace_id#bare_repo, no commit_sha). symbol_at_line walks
    this partition directly -- the SHA-bearing PK in ``_put_fact``
    above is for GSI-backed tools and would not reach this query."""
    source = target_symbol_id.rsplit("::", 1)[0]
    table.put_item(Item={
        "PK": f"{workspace_id}#{bare_repo}",
        "SK": f"defines#{source}#{target_symbol_id}#{file_path}#{line}",
        "GSI1PK": f"{workspace_id}#{target_symbol_id}",
        "GSI1SK": f"defines#{bare_repo}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": bare_repo,
        "commit_sha": SHA,
        "kind": "defines",
        "source_symbol_id": source,
        "target_symbol_id": target_symbol_id,
        "file_path": file_path,
        "line": int(line),
        "language": language,
    })


class TestFindSymbolAtLine:

    BARE = "auth-service"
    FILE = "lambda/auth/handler.py"

    def _seed_audit_case(self, table):
        """Seed the magic_link audit shape: handle_verify at 1215,
        verify_magic_token at 531, _claim_magic_token_jti at 487.
        The 2026-06-02 failure cited handle_verify:544 when the real
        enclosing symbol at 544 is verify_magic_token."""
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

    def test_happy_path_returns_innermost_enclosing_symbol(self, tables):
        self._seed_audit_case(tables.Table(SYMBOL_GRAPH_TABLE))
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_at_line(
                {
                    "workspace_id": WORKSPACE_ID,
                    "repo": self.BARE,
                    "file_path": self.FILE,
                    "line": 544,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["symbol"] is not None
        # The audit fix: 544 -> verify_magic_token (NOT handle_verify).
        assert payload["symbol"]["target_symbol_id"] == (
            "python:lambda.auth.handler::verify_magic_token"
        )
        assert payload["symbol"]["line"] == 531
        assert payload["symbol"]["file_path"] == self.FILE
        assert payload["queried_line"] == 544
        assert payload["queried_file_path"] == self.FILE

    def test_owner_prefixed_repo_normalizes_to_bare(self, tables):
        """Caller passes 'bcarpio/eShop'; graph stores 'eShop'. The
        reader strips owner so the PK lookup hits."""
        _put_defines_v2(
            tables.Table(SYMBOL_GRAPH_TABLE),
            workspace_id=WORKSPACE_ID,
            bare_repo="eShop",
            target_symbol_id="csharp:eShop.ServiceDefaults::SignaturesDemo::Greet",
            file_path="src/eShop.ServiceDefaults/SignaturesDemo.cs",
            line=10,
            language="csharp",
        )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_at_line(
                {
                    "workspace_id": WORKSPACE_ID,
                    "repo": "bcarpio/eShop",
                    "file_path": "src/eShop.ServiceDefaults/SignaturesDemo.cs",
                    "line": 12,
                },
                USER_EMAIL,
            )
        payload = json.loads(result["text"])
        assert payload["symbol"]["target_symbol_id"] == (
            "csharp:eShop.ServiceDefaults::SignaturesDemo::Greet"
        )

    def test_no_enclosing_symbol_returns_null_not_error(self, tables):
        self._seed_audit_case(tables.Table(SYMBOL_GRAPH_TABLE))
        with _patch_user_workspaces([WORKSPACE_ID]):
            # Line 10 is BEFORE the earliest defines (line 487).
            result = mcp_module.handle_find_symbol_at_line(
                {
                    "workspace_id": WORKSPACE_ID,
                    "repo": self.BARE,
                    "file_path": self.FILE,
                    "line": 10,
                },
                USER_EMAIL,
            )
        assert "error" not in result
        payload = json.loads(result["text"])
        assert payload["symbol"] is None
        assert payload["queried_line"] == 10

    def test_access_denied_when_user_not_in_workspace(self, tables):
        self._seed_audit_case(tables.Table(SYMBOL_GRAPH_TABLE))
        # User has access only to a different workspace.
        with _patch_user_workspaces([OTHER_WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_at_line(
                {
                    "workspace_id": WORKSPACE_ID,
                    "repo": self.BARE,
                    "file_path": self.FILE,
                    "line": 544,
                },
                USER_EMAIL,
            )
        assert "error" in result

    def test_missing_required_params(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            for missing in ("workspace_id", "repo", "file_path", "line"):
                args = {
                    "workspace_id": WORKSPACE_ID,
                    "repo": self.BARE,
                    "file_path": self.FILE,
                    "line": 544,
                }
                del args[missing]
                result = mcp_module.handle_find_symbol_at_line(
                    args, USER_EMAIL
                )
                assert "error" in result, (
                    f"Expected error when {missing} missing"
                )

    def test_non_integer_line_returns_error(self, tables):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_find_symbol_at_line(
                {
                    "workspace_id": WORKSPACE_ID,
                    "repo": self.BARE,
                    "file_path": self.FILE,
                    "line": "not-an-int",
                },
                USER_EMAIL,
            )
        assert result.get("error") == "line must be an integer"

    def test_symbol_graph_unavailable(self, tables):
        from shared import symbol_graph_reader  # type: ignore

        with _patch_user_workspaces([WORKSPACE_ID]):
            with patch.object(
                symbol_graph_reader,
                "symbol_at_line",
                side_effect=symbol_graph_reader.SymbolGraphUnavailable(
                    "ddb throttled"
                ),
            ):
                result = mcp_module.handle_find_symbol_at_line(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "repo": self.BARE,
                        "file_path": self.FILE,
                        "line": 544,
                    },
                    USER_EMAIL,
                )
        assert result.get("error") == "Symbol graph unavailable"
