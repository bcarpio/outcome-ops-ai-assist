# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the ``read_source`` MCP tool on the OutcomeOps gateway.

Covers ``handle_read_source`` (plan-read-source-adjudicator ):
input validation, workspace-access gating, installation-id resolution
out of the workspaces table, GitHub App token mint + Contents API
fetch, and the window slicing around an anchor line.

The GitHub calls are mocked at ``urllib.request.urlopen``; the SSM
calls are mocked at ``boto3.client('ssm').get_parameters``. The
workspaces DDB row is created in a moto-backed table so the
installation-id lookup runs through real boto3 + moto rather than a
hand-stubbed DynamoDB call.
"""

from __future__ import annotations

import sys as _sys
for _name in list(_sys.modules):
    if _name.startswith("shared.symbol_graph_reader"):
        _sys.modules.pop(_name, None)

import importlib.util
import io
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws


_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

WORKSPACES_TABLE = "test-mcp-read-source-workspaces"
WORKSPACE_ID = "ws-rs-test"
USER_EMAIL = "user@example.com"
INSTALLATION_ID = "112760440"
REPO = "owner/eShop"
FILE_PATH = "src/eShop.ServiceDefaults/SignaturesDemo.cs"

os.environ["DYNAMODB_TABLE"] = WORKSPACES_TABLE
os.environ["S3_VECTORS_BUCKET"] = "test-vectors-bucket"
os.environ["S3_VECTORS_INDEX"] = "test-vectors-index"
os.environ["KB_BUCKET"] = "test-kb-bucket"
os.environ["ENVIRONMENT"] = "test"
os.environ["MCP_BASE_URL"] = "http://test-alb.example.com"
os.environ["APP_NAME"] = "outcome-ops-ai-assist"


def _load_mcp_module():
    mod_name = "mcp_server_handler_read_source"
    if mod_name in sys.modules:
        del sys.modules[mod_name]
    handler_path = os.path.join(
        os.path.dirname(__file__), "../../mcp-server/handler.py"
    )
    spec = importlib.util.spec_from_file_location(mod_name, handler_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


mcp_module = _load_mcp_module()

from shared import source_reader  # noqa: E402  (the reader the tool delegates to)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _aws_mocks():
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    monkeypatch.setenv("ENVIRONMENT", "test")
    monkeypatch.setenv("APP_NAME", "outcome-ops-ai-assist")
    # shared.source_reader reads the table name per call; other test
    # modules set DYNAMODB_TABLE at import, so pin it for these tests.
    monkeypatch.setenv("DYNAMODB_TABLE", WORKSPACES_TABLE)
    yield


@pytest.fixture(autouse=True)
def _reset_mcp_caches():
    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None
    source_reader._dynamodb = None
    source_reader._github_app_config_cache = None
    source_reader._ado_client_secret_cache = None
    yield


@pytest.fixture
def workspaces_table():
    """Create the workspaces DDB table and seed an installation row."""
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=WORKSPACES_TABLE,
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
    resource = boto3.resource("dynamodb", region_name="us-west-2")
    table = resource.Table(WORKSPACES_TABLE)
    table.put_item(Item={
        "PK": f"WS#{WORKSPACE_ID}",
        "SK": f"GITHUB_INSTALLATION#{INSTALLATION_ID}",
        "installation_id": INSTALLATION_ID,
    })
    return table


def _patch_user_workspaces(workspace_ids):
    """Stub the gateway's workspace-access gate to grant the requested ids."""
    return patch.object(
        mcp_module,
        "_check_symbol_graph_workspace_access",
        return_value=None if workspace_ids else {"error": "denied"},
    )


def _stub_github_success(file_text, app_id="123", private_key="-----BEGIN-FAKE-----"):
    """Patch SSM + JWT + urlopen so the read_source flow succeeds.

    Returns the mock urlopen so individual tests can assert on its call
    args (e.g., that the right ref made it into the URL).
    """
    def _fake_get_parameters(Names, WithDecryption=False):  # noqa: N803
        params = []
        for name in Names:
            tail = name.rsplit("/", 1)[-1]
            if tail == "app-id":
                params.append({"Name": name, "Value": app_id})
            elif tail == "private-key":
                params.append({"Name": name, "Value": private_key})
        return {"Parameters": params}

    ssm = MagicMock()
    ssm.get_parameters.side_effect = _fake_get_parameters

    call_log = []

    def _fake_urlopen(req, timeout=None):  # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        call_log.append(url)
        if "/access_tokens" in url:
            body = json.dumps({"token": "ghs_fake"}).encode()
        else:
            body = file_text.encode()
        resp = MagicMock()
        resp.read.return_value = body
        resp.__enter__ = lambda s: s
        resp.__exit__ = lambda s, *a: None
        return resp

    patches = [
        patch("boto3.client", return_value=ssm),
        patch.object(mcp_module, "urllib", new=__import__("urllib")),  # ensure real urllib
        patch("urllib.request.urlopen", side_effect=_fake_urlopen),
        patch("jwt.encode", return_value="fake.jwt.token"),
    ]
    return patches, call_log


def _enter(patches):
    """Apply a list of patch objects and return them so tests can stop them."""
    ctxs = [p.start() for p in patches]
    return ctxs


def _exit(patches):
    for p in patches:
        p.stop()


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


class TestInputValidation:

    def test_missing_required_fields_each_rejected(self):
        for missing in ("workspace_id", "repo", "file_path", "line"):
            params = {
                "workspace_id": WORKSPACE_ID, "repo": REPO,
                "file_path": FILE_PATH, "line": 10,
            }
            del params[missing]
            result = mcp_module.handle_read_source(params, USER_EMAIL)
            assert "error" in result
            assert "required" in result["error"]

    def test_line_zero_rejected(self):
        result = mcp_module.handle_read_source(
            {"workspace_id": WORKSPACE_ID, "repo": REPO,
             "file_path": FILE_PATH, "line": 0},
            USER_EMAIL,
        )
        assert "error" in result and "1-indexed" in result["error"]

    def test_context_lines_over_cap_rejected(self):
        result = mcp_module.handle_read_source(
            {"workspace_id": WORKSPACE_ID, "repo": REPO,
             "file_path": FILE_PATH, "line": 10, "context_lines": 9999},
            USER_EMAIL,
        )
        assert "error" in result and "context_lines" in result["error"]

    def test_repo_must_have_owner_slash_name(self):
        result = mcp_module.handle_read_source(
            {"workspace_id": WORKSPACE_ID, "repo": "noslash",
             "file_path": FILE_PATH, "line": 10},
            USER_EMAIL,
        )
        assert "error" in result and "owner/name" in result["error"]


# ---------------------------------------------------------------------------
# Access gating + installation lookup
# ---------------------------------------------------------------------------


class TestAccessGating:

    def test_workspace_access_denied_short_circuits(self, workspaces_table):
        with patch.object(
            mcp_module,
            "_check_symbol_graph_workspace_access",
            return_value={"error": "not a workspace member"},
        ):
            result = mcp_module.handle_read_source(
                {"workspace_id": WORKSPACE_ID, "repo": REPO,
                 "file_path": FILE_PATH, "line": 10},
                USER_EMAIL,
            )
        assert result == {"error": "not a workspace member"}

    def test_no_installation_linked_returns_clear_error(self):
        # Workspaces table exists but no installation row -> error.
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=WORKSPACES_TABLE,
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
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {"workspace_id": WORKSPACE_ID, "repo": REPO,
                 "file_path": FILE_PATH, "line": 10},
                USER_EMAIL,
            )
        assert "error" in result
        assert "code-hosting integration" in result["error"]


# ---------------------------------------------------------------------------
# Happy path + window math
# ---------------------------------------------------------------------------


class TestReadHappyPath:

    def test_returns_window_around_anchor(self, workspaces_table):
        file_text = "\n".join(f"line-{i:03d}" for i in range(1, 51))
        patches, call_log = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID, "repo": REPO,
                     "file_path": FILE_PATH, "line": 25, "context_lines": 3},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)

        assert "text" in result
        payload = json.loads(result["text"])
        assert payload["anchor_line"] == 25
        assert payload["window_start"] == 22
        assert payload["window_end"] == 28
        assert [r["line"] for r in payload["lines"]] == [22, 23, 24, 25, 26, 27, 28]
        assert payload["lines"][3] == {"line": 25, "text": "line-025"}
        assert payload["total_lines"] == 50
        assert payload["clipped_to_file_bounds"] is False

    def test_window_clipped_at_file_start(self, workspaces_table):
        file_text = "\n".join(f"L{i}" for i in range(1, 11))
        patches, _ = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID, "repo": REPO,
                     "file_path": FILE_PATH, "line": 2, "context_lines": 5},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        payload = json.loads(result["text"])
        assert payload["window_start"] == 1
        assert payload["clipped_to_file_bounds"] is True

    def test_ref_passed_through_to_contents_url(self, workspaces_table):
        patches, call_log = _stub_github_success("a\nb\nc\n")
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID, "repo": REPO,
                     "file_path": FILE_PATH, "line": 2,
                     "ref": "deadbeef"},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        contents_calls = [u for u in call_log if "/contents/" in u]
        assert contents_calls, "expected at least one contents fetch"
        assert "ref=deadbeef" in contents_calls[-1]


# ===========================================================================
# -- SHA-skew handling: graph resolution + window relocation.
# ===========================================================================


SYMBOL_GRAPH_TABLE = "test-mcp-read-source-symbol-graph"
CODE_MAPS_TABLE = "test-mcp-read-source-code-maps"


@pytest.fixture
def graph_tables(monkeypatch):
    """Create the symbol-graph + code-maps tables and wire env vars."""
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", SYMBOL_GRAPH_TABLE)
    monkeypatch.setenv("CODE_MAPS_TABLE", CODE_MAPS_TABLE)
    client = boto3.client("dynamodb", region_name="us-west-2")
    client.create_table(
        TableName=SYMBOL_GRAPH_TABLE,
        AttributeDefinitions=[
            {"AttributeName": "PK", "AttributeType": "S"},
            {"AttributeName": "SK", "AttributeType": "S"},
            {"AttributeName": "GSI1PK", "AttributeType": "S"},
            {"AttributeName": "GSI1SK", "AttributeType": "S"},
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
        ],
        BillingMode="PAY_PER_REQUEST",
    )
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
    # Inject the fresh resource into shared.symbol_graph_reader so it
    # uses moto rather than the cross-suite-contaminated boto3 import.
    from shared import symbol_graph_reader  # type: ignore
    real_resource = boto3.resource("dynamodb", region_name="us-west-2")
    symbol_graph_reader._reset_clients_for_tests()
    symbol_graph_reader._set_resource_for_tests(real_resource)
    yield real_resource
    symbol_graph_reader._reset_clients_for_tests()


def _seed_defines(resource, workspace_id, repo, symbol, file_path, line, graph_sha):
    """Seed one defines row in the symbol-graph table + the pointer."""
    sg = resource.Table(SYMBOL_GRAPH_TABLE)
    sg.put_item(Item={
        "PK": f"{workspace_id}#{repo}#{graph_sha}",
        "SK": f"defines#{symbol}#{symbol}#{file_path}#{line}",
        "GSI1PK": f"{workspace_id}#{symbol}",
        "GSI1SK": f"defines#{repo}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": repo,
        "commit_sha": graph_sha,
        "kind": "defines",
        "source_symbol_id": symbol,
        "target_symbol_id": symbol,
        "file_path": file_path,
        "line": int(line),
        "language": "python",
    })
    cm = resource.Table(CODE_MAPS_TABLE)
    repo_bare = repo.split("/", 1)[-1]
    cm.put_item(Item={
        "PK": f"SYMBOL_GRAPH#{workspace_id}#{repo_bare}",
        "SK": "POINTER",
        "graph_sha": graph_sha,
    })


class TestBareNameAndRelocate:

    def test_bare_name_extracts_python_tail(self):
        assert source_reader.bare_name(
            "python:lambda.auth.handler::require_auth"
        ) == "require_auth"

    def test_bare_name_extracts_namespaced_tail(self):
        assert source_reader.bare_name(
            "python:m::Service::run"
        ) == "run"

    def test_bare_name_empty_input(self):
        assert source_reader.bare_name("") == ""

    def test_relocate_finds_first_match(self):
        window = [
            {"line": 10, "text": "    pass"},
            {"line": 11, "text": "def greet(name):"},
            {"line": 12, "text": "    return name"},
        ]
        assert source_reader.relocate(window, "greet") == 11

    def test_relocate_word_boundary_rejects_substring(self):
        window = [
            {"line": 1, "text": "def my_require_auth(x):"},
            {"line": 2, "text": "    pass"},
        ]
        # `require_auth` must NOT match `my_require_auth` thanks to \b.
        assert source_reader.relocate(window, "require_auth") is None

    def test_relocate_returns_none_when_absent(self):
        window = [{"line": 1, "text": "nothing here"}]
        assert source_reader.relocate(window, "greet") is None


class TestAtGraphShaResolution:

    def test_at_graph_sha_without_symbol_rejected(self):
        result = mcp_module.handle_read_source(
            {"workspace_id": WORKSPACE_ID, "at_graph_sha": True},
            USER_EMAIL,
        )
        assert "error" in result and "at_graph_sha" in result["error"]

    def test_at_graph_sha_resolves_and_pins_ref(self, workspaces_table, graph_tables):
        graph_sha = "deadbeef" + "0" * 32
        sym = "python:m::greet"
        _seed_defines(
            graph_tables, WORKSPACE_ID, REPO, sym,
            "src/module.py", 42, graph_sha,
        )
        file_text = "\n".join(f"line-{i}" for i in range(1, 81))
        patches, call_log = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "symbol": sym,
                        "at_graph_sha": True,
                        "context_lines": 3,
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)

        payload = json.loads(result["text"])
        assert payload["repo"] == REPO
        assert payload["file_path"] == "src/module.py"
        assert payload["anchor_line"] == 42
        assert payload["ref"] == graph_sha
        assert payload["graph_sha"] == graph_sha
        # ref=<sha> made it into the Contents URL.
        contents = [u for u in call_log if "/contents/" in u]
        assert contents and f"ref={graph_sha}" in contents[-1]

    def test_at_graph_sha_missing_symbol_in_graph_errors(
        self, workspaces_table, graph_tables,
    ):
        # Tables exist but no defines row for this symbol -> "missing".
        # workspaces_table seeds a GitHub installation so platform-resolve
        # (which now runs before graph-resolve) doesn't short-circuit.
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {
                    "workspace_id": WORKSPACE_ID,
                    "symbol": "python:nothing::nope",
                    "at_graph_sha": True,
                },
                USER_EMAIL,
            )
        assert "error" in result and "no definition" in result["error"]

    def test_at_graph_sha_ambiguous_returns_sites(self, workspaces_table, graph_tables):
        sym = "python:m::greet"
        for repo, line in [(REPO, 10), ("owner/other", 99)]:
            _seed_defines(
                graph_tables, WORKSPACE_ID, repo, sym,
                "src/module.py", line, "sha-" + repo.split("/")[-1],
            )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {
                    "workspace_id": WORKSPACE_ID,
                    "symbol": sym,
                    "at_graph_sha": True,
                },
                USER_EMAIL,
            )
        assert "error" in result and "multiple definitions" in result["error"]
        assert isinstance(result.get("sites"), list) and len(result["sites"]) == 2


class TestSymbolRelocationInWindow:

    def test_anchor_stale_relocated_to_actual_line(self, workspaces_table):
        file_text = "\n".join([
            "header",
            "def other():",
            "    pass",
            "",
            "def greet(name):",   # actual position: line 5
            "    return name",
            "",
            "def trailer():",
            "    pass",
        ])
        patches, _ = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "repo": REPO,
                        "file_path": "src/module.py",
                        "line": 7,  # stale anchor; actual greet is at 5
                        "context_lines": 3,
                        "symbol": "python:m::greet",
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        payload = json.loads(result["text"])
        assert payload["anchor_line"] == 7
        assert payload["relocated_to_line"] == 5
        assert payload["relocated"] is True

    def test_anchor_correct_relocated_to_same_line(self, workspaces_table):
        file_text = "\n".join([
            "alpha", "beta", "def greet(name):", "    return name", "trailer",
        ])
        patches, _ = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "repo": REPO,
                        "file_path": "src/module.py",
                        "line": 3,
                        "context_lines": 2,
                        "symbol": "python:m::greet",
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        payload = json.loads(result["text"])
        # Same line: relocated_to_line set, but `relocated` flag stays False.
        assert payload["relocated_to_line"] == 3
        assert payload["relocated"] is False


# ===========================================================================
# -- integration-shaped: the "verified value beats a stale
# code-map claim" scenario the plan was written to close. End-to-end
# from the read_source tool's perspective (the chat synthesis loop is
# separately covered by the prompt-rules tests in test_shared_prompts.py).
# ===========================================================================


class TestReadSourceVerifiedValueWinsScenario:
    """
    Scenario: a code-map summary suggests the auth handler uses
    ``token_urlsafe(16)`` for one construct, but the actual source at
    the graph-pinned anchor uses ``token_urlsafe(32)``. read_source
    returns the actual line with a citable file:line so the
    synthesizer can quote and surface the disagreement (ADR-025
    "code wins"). Without this tool the synthesizer would ship the
    summary's recalled value silently (the USED_TOKEN# silent-fabrication
    class motivating the adjudicator plan).
    """

    def test_actual_source_line_returned_for_quotable_citation(
        self, workspaces_table, graph_tables,
    ):
        graph_sha = "abc12300000000000000000000000000abc12300"
        sym = "python:lambda.auth.handler::create_magic_token"
        _seed_defines(
            graph_tables, WORKSPACE_ID, REPO, sym,
            "lambda/auth/handler.py", 514, graph_sha,
        )
        # The actual source uses 32, NOT 16 -- this is the value the
        # code-map summary either omitted (ADR-027) or whose neighbor
        # the LLM recalled wrongly. read_source must surface the
        # truthful line so the synthesizer can quote it.
        source_at_graph_sha = "\n".join([
            "import secrets",
            "",
            "MAGIC_TOKEN_SK_PREFIX = 'MAGIC_TOKEN#'",
            "",
            *(f"# pad {i}" for i in range(1, 510)),
            "def create_magic_token():",
            "    token = secrets.token_urlsafe(32)",   # the real value
            "    return f'{MAGIC_TOKEN_SK_PREFIX}{token}'",
        ])

        patches, call_log = _stub_github_success(source_at_graph_sha)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "symbol": sym,
                        "at_graph_sha": True,
                        "context_lines": 4,
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)

        payload = json.loads(result["text"])
        # The read was pinned to the graph_sha -- no skew.
        assert payload["graph_sha"] == graph_sha
        assert payload["ref"] == graph_sha
        # The actual source line is in the returned window for the
        # synthesizer to quote.
        window_texts = [r["text"] for r in payload["lines"]]
        assert any("token_urlsafe(32)" in t for t in window_texts)
        # The synthesizer can cite file:line.
        assert payload["file_path"] == "lambda/auth/handler.py"
        assert payload["anchor_line"] == 514
        # And the bare name is the symbol it actually called for, not a
        # neighbor (no relocation drift).
        assert payload["symbol"] == sym
        assert payload["relocated"] is False


# ===========================================================================
# Bare-repo handling in at_graph_sha mode (post-fix surfaced by
# live eShop smoke test on 2026-06-02). The symbol-graph stores `repo`
# as the bare name; the GitHub Contents API needs 'owner/<bare>'.
# ===========================================================================


def _seed_workspace_repo_row(table, workspace_id, owner_repo):
    """Seed a GITHUB_REPO# row matching what the UI creates on repo-add."""
    table.put_item(Item={
        "PK": f"WS#{workspace_id}",
        "SK": f"GITHUB_REPO#{owner_repo}",
        "added_at": "2026-06-02T00:00:00+00:00",
    })


def _seed_defines_bare_repo(resource, workspace_id, repo_bare, symbol,
                             file_path, line, graph_sha):
    """Seed a defines row whose `repo` field is the BARE name (matches
    what the live graph rows actually carry)."""
    sg = resource.Table(SYMBOL_GRAPH_TABLE)
    sg.put_item(Item={
        "PK": f"{workspace_id}#{repo_bare}#{graph_sha}",
        "SK": f"defines#{symbol}#{symbol}#{file_path}#{line}",
        "GSI1PK": f"{workspace_id}#{symbol}",
        "GSI1SK": f"defines#{repo_bare}#{file_path}#{line}",
        "workspace_id": workspace_id,
        "repo": repo_bare,
        "commit_sha": graph_sha,
        "kind": "defines",
        "source_symbol_id": symbol,
        "target_symbol_id": symbol,
        "file_path": file_path,
        "line": int(line),
        "language": "csharp",
    })
    cm = resource.Table(CODE_MAPS_TABLE)
    cm.put_item(Item={
        "PK": f"SYMBOL_GRAPH#{workspace_id}#{repo_bare}",
        "SK": "POINTER",
        "graph_sha": graph_sha,
    })


class TestBareRepoLookup:

    def test_resolver_recovers_owner_from_workspace_repo_row(
        self, workspaces_table, graph_tables,
    ):
        """Live eShop smoke test surfaced this: graph.repo == 'eShop'
        (bare) but the GitHub fetch needs 'bcarpio/eShop'. The resolver
        must scan the workspace's GITHUB_REPO# rows and recover the
        owner-prefixed form."""
        _seed_workspace_repo_row(workspaces_table, WORKSPACE_ID, "bcarpio/eShop")
        graph_sha = "1234567890" * 4
        sym = "csharp:eShop.ServiceDefaults::SignaturesDemo::Greet"
        _seed_defines_bare_repo(
            graph_tables, WORKSPACE_ID, "eShop", sym,
            "src/eShop.ServiceDefaults/SignaturesDemo.cs", 10, graph_sha,
        )
        file_text = "\n".join(f"L{i}" for i in range(1, 25))
        patches, call_log = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": WORKSPACE_ID,
                        "symbol": sym,
                        "at_graph_sha": True,
                        "context_lines": 2,
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        payload = json.loads(result["text"])
        # The owner was recovered from the workspaces row.
        assert payload["repo"] == "bcarpio/eShop"
        # The graph SHA was looked up and pinned on the read.
        assert payload["graph_sha"] == graph_sha
        assert payload["ref"] == graph_sha
        # GitHub URL used the owner-prefixed name.
        contents = [u for u in call_log if "/contents/" in u]
        assert contents and "bcarpio/eShop" in contents[-1]

    def test_resolver_errors_when_no_workspace_repo_row_matches_bare(
        self, workspaces_table, graph_tables,
    ):
        """If the graph has a defines row whose bare repo doesn't match
        any GITHUB_REPO# row in the workspace, we cannot derive an owner
        and the resolver returns a clear error rather than passing the
        bare name to the existing 'owner/name' validator."""
        # No GITHUB_REPO# row seeded -- the lookup must fail explicitly.
        graph_sha = "deadbeef" * 5
        sym = "csharp:unknown::Foo::bar"
        _seed_defines_bare_repo(
            graph_tables, WORKSPACE_ID, "unknown", sym,
            "src/Unknown/Foo.cs", 5, graph_sha,
        )
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {
                    "workspace_id": WORKSPACE_ID,
                    "symbol": sym,
                    "at_graph_sha": True,
                },
                USER_EMAIL,
            )
        assert "error" in result
        assert "no GitHub repo row" in result["error"]


# ===========================================================================
# ADR-037 -- is_global workspaces are readable by any authenticated caller.
# The MCP per-tool gates must honor the flag, not just direct membership.
# ===========================================================================


def _seed_workspace_metadata(table, workspace_id, is_global=False):
    table.put_item(Item={
        "PK": f"WS#{workspace_id}",
        "SK": "METADATA",
        "workspace_id": workspace_id,
        "name": "Standards and ADRs",
        "is_global": bool(is_global),
    })


class TestIsGlobalPassThrough:
    """ADR-037 fourth per-surface fix: MCP per-tool access gates."""

    def test_helper_returns_true_for_is_global_metadata(self, workspaces_table):
        _seed_workspace_metadata(workspaces_table, "ws-global-1", is_global=True)
        assert mcp_module._workspace_is_global("ws-global-1") is True

    def test_helper_returns_false_when_flag_absent(self, workspaces_table):
        _seed_workspace_metadata(workspaces_table, "ws-normal-1", is_global=False)
        assert mcp_module._workspace_is_global("ws-normal-1") is False

    def test_helper_returns_false_when_row_missing(self, workspaces_table):
        assert mcp_module._workspace_is_global("ws-does-not-exist") is False

    def test_helper_empty_workspace_id_returns_false(self):
        assert mcp_module._workspace_is_global("") is False
        assert mcp_module._workspace_is_global(None) is False

    def test_symbol_graph_gate_allows_non_member_on_global(self, workspaces_table):
        _seed_workspace_metadata(workspaces_table, "ws-global-2", is_global=True)
        with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
            deny = mcp_module._check_symbol_graph_workspace_access(
                "ws-global-2", USER_EMAIL,
            )
        assert deny is None

    def test_symbol_graph_gate_denies_non_member_on_private(self, workspaces_table):
        _seed_workspace_metadata(workspaces_table, "ws-private-1", is_global=False)
        with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
            deny = mcp_module._check_symbol_graph_workspace_access(
                "ws-private-1", USER_EMAIL,
            )
        assert deny == {"error": "You do not have access to this workspace"}

    def test_symbol_graph_gate_still_allows_direct_member(self, workspaces_table):
        _seed_workspace_metadata(workspaces_table, "ws-private-2", is_global=False)
        with patch.object(
            mcp_module, "get_user_workspace_ids", return_value=["ws-private-2"],
        ):
            deny = mcp_module._check_symbol_graph_workspace_access(
                "ws-private-2", USER_EMAIL,
            )
        assert deny is None

    def test_read_source_flows_through_global_gate(self, workspaces_table):
        """End-to-end: non-member calling read_source on an is_global
        workspace passes the gate and reaches the GitHub fetch path."""
        _seed_workspace_metadata(workspaces_table, WORKSPACE_ID, is_global=True)
        file_text = "\n".join(f"line-{i:03d}" for i in range(1, 21))
        patches, _ = _stub_github_success(file_text)
        _enter(patches)
        try:
            with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID, "repo": REPO,
                     "file_path": FILE_PATH, "line": 10, "context_lines": 2},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["anchor_line"] == 10

    def test_list_workspace_documents_flows_through_global_gate(
        self, workspaces_table,
    ):
        """handle_list_workspace_documents uses its own inline check;
        verify it honors is_global the same way the symbol-graph gate does."""
        _seed_workspace_metadata(workspaces_table, "ws-doc-global", is_global=True)

        # Stub S3 Vectors so the gate is the only thing under test.
        stub_vectors = MagicMock()
        stub_vectors.query_vectors.return_value = {"vectors": []}
        with patch.object(
            mcp_module, "_get_s3vectors_client", return_value=stub_vectors,
        ), patch.object(
            mcp_module, "generate_embedding", return_value=[0.1] * 8,
        ), patch.object(
            mcp_module, "get_user_workspace_ids", return_value=[],
        ):
            result = mcp_module.handle_list_workspace_documents(
                {"workspace_id": "ws-doc-global"}, USER_EMAIL,
            )
        assert "error" not in result, f"gate denied non-member on global: {result}"
        assert "text" in result

    def test_list_workspace_documents_still_denies_non_member_on_private(
        self, workspaces_table,
    ):
        _seed_workspace_metadata(workspaces_table, "ws-doc-private", is_global=False)
        with patch.object(mcp_module, "get_user_workspace_ids", return_value=[]):
            result = mcp_module.handle_list_workspace_documents(
                {"workspace_id": "ws-doc-private"}, USER_EMAIL,
            )
        assert result == {"error": "You do not have access to this workspace"}


# ===========================================================================
# ADR-030 v1.1 -- platform-agnostic dispatch. read_source works against
# Azure DevOps as well as GitHub, driven by the workspace's connection.
# ===========================================================================


ADO_ORG = "contoso"
ADO_PROJECT = "Platform"
ADO_REPO_BARE = "Signatures"
ADO_TRIPLET = f"{ADO_ORG}/{ADO_PROJECT}/{ADO_REPO_BARE}"
ADO_CONNECTION_ID = "conn-ado-abc123"


def _seed_azdo_repo_row(
    table, workspace_id, org=ADO_ORG, project=ADO_PROJECT,
    repo=ADO_REPO_BARE, connection_id=ADO_CONNECTION_ID,
):
    slug = f"{org}/{project}/{repo}"
    table.put_item(Item={
        "PK": f"WS#{workspace_id}",
        "SK": f"AZDO_REPO#{slug}",
        "workspace_id": workspace_id,
        "provider": "azure_devops",
        "org": org,
        "project": project,
        "repo": repo,
        "slug": slug,
        "connection_id": connection_id,
        "default_branch": "main",
        "last_commit_sha": "d" * 40,
        "enabled": True,
    })


def _seed_github_installation_row(
    table, workspace_id, installation_id=INSTALLATION_ID,
):
    table.put_item(Item={
        "PK": f"WS#{workspace_id}",
        "SK": f"GITHUB_INSTALLATION#{installation_id}",
        "installation_id": installation_id,
    })


ADO_WORKSPACE_ID = "ws-rs-ado-test"


class TestReadSourceResolvePlatform:
    """platform resolver picks the right connection.

    Uses ``ADO_WORKSPACE_ID`` for ADO-only cases so the fixture-seeded
    GitHub row on ``WORKSPACE_ID`` doesn't shadow the ADO branch.
    """

    def test_github_short_circuit_when_only_github_row_present(
        self, workspaces_table,
    ):
        # workspaces_table fixture already seeded GITHUB_INSTALLATION# on
        # WORKSPACE_ID; the resolver must find it.
        info = source_reader.resolve_platform(WORKSPACE_ID)
        assert info == {
            "platform": "github",
            "installation_id": INSTALLATION_ID,
        }

    def test_ado_row_direct_key_when_repo_hint_is_triplet(
        self, workspaces_table,
    ):
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        info = source_reader.resolve_platform(
            ADO_WORKSPACE_ID, repo_hint=ADO_TRIPLET,
        )
        assert info == {
            "platform": "azure_devops",
            "connection_id": ADO_CONNECTION_ID,
            "org": ADO_ORG,
            "project": ADO_PROJECT,
            "repo": ADO_REPO_BARE,
        }

    def test_ado_query_fallback_when_no_repo_hint(
        self, workspaces_table,
    ):
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        info = source_reader.resolve_platform(ADO_WORKSPACE_ID)
        assert info["platform"] == "azure_devops"
        assert info["connection_id"] == ADO_CONNECTION_ID

    def test_github_wins_when_both_present(self, workspaces_table):
        """Documented behavior: GitHub short-circuits ADO. A workspace
        with both connections isn't supported today; the router will
        cross-check the repo shape and route accordingly."""
        # WORKSPACE_ID already has GITHUB_INSTALLATION# from fixture.
        _seed_azdo_repo_row(workspaces_table, WORKSPACE_ID)
        info = source_reader.resolve_platform(WORKSPACE_ID)
        assert info["platform"] == "github"

    def test_none_when_neither_row_present(self, workspaces_table):
        assert source_reader.resolve_platform(
            "ws-empty-neither-connection",
        ) is None

    def test_returns_none_on_ddb_error(self, workspaces_table):
        stub_table = MagicMock()
        stub_table.query.side_effect = _client_error("InternalServerError")
        stub_ddb = MagicMock()
        stub_ddb.Table.return_value = stub_table
        with patch.object(source_reader, "_get_dynamodb", return_value=stub_ddb):
            assert source_reader.resolve_platform(WORKSPACE_ID) is None

    def test_direct_key_miss_falls_through_to_query(self, workspaces_table):
        """A wrong-triplet hint shouldn't hide an existing ADO row --
        the fallback query still finds it. Guards against a router-caller
        bug where the LLM sent the wrong triplet shape."""
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        info = source_reader.resolve_platform(
            ADO_WORKSPACE_ID, repo_hint="wrong-org/wrong-project/wrong-repo",
        )
        assert info["platform"] == "azure_devops"


class TestReadSourceResolveFullAdoTriplet:
    """bare-name -> full triplet resolver for at_graph_sha path."""

    def test_passthrough_when_already_triplet(self, workspaces_table):
        result = source_reader.resolve_full_ado_triplet(
            WORKSPACE_ID, ADO_TRIPLET,
        )
        assert result == ADO_TRIPLET

    def test_resolves_bare_repo_to_triplet(self, workspaces_table):
        _seed_azdo_repo_row(workspaces_table, WORKSPACE_ID)
        result = source_reader.resolve_full_ado_triplet(
            WORKSPACE_ID, ADO_REPO_BARE,
        )
        assert result == ADO_TRIPLET

    def test_returns_none_when_bare_missing(self, workspaces_table):
        result = source_reader.resolve_full_ado_triplet(
            WORKSPACE_ID, "does-not-exist",
        )
        assert result is None

    def test_returns_none_when_ambiguous(self, workspaces_table):
        """Two ADO repos with the same bare name in different orgs must
        NOT silently resolve to one of them. Better to fail loudly than
        fetch from the wrong project."""
        _seed_azdo_repo_row(
            workspaces_table, WORKSPACE_ID, org="org-a", project="proj",
            repo=ADO_REPO_BARE,
        )
        _seed_azdo_repo_row(
            workspaces_table, WORKSPACE_ID, org="org-b", project="proj",
            repo=ADO_REPO_BARE, connection_id="conn-other",
        )
        result = source_reader.resolve_full_ado_triplet(
            WORKSPACE_ID, ADO_REPO_BARE,
        )
        assert result is None

    def test_returns_none_on_empty_input(self, workspaces_table):
        assert source_reader.resolve_full_ado_triplet(
            WORKSPACE_ID, "",
        ) is None


class TestReadSourceAdoAccessToken:
    """token-mint wrapper reaches shared.entra_id_oauth with
    the right constants and error surface."""

    def test_missing_connection_id_raises(self, monkeypatch):
        with pytest.raises(ValueError):
            source_reader.ado_access_token("")

    def test_missing_env_vars_raises_runtime(self, monkeypatch):
        monkeypatch.delenv("AZURE_DEVOPS_TENANT_ID", raising=False)
        monkeypatch.delenv("AZURE_DEVOPS_CLIENT_ID", raising=False)
        source_reader._ado_client_secret_cache = None
        with pytest.raises(RuntimeError, match="AZURE_DEVOPS"):
            source_reader.ado_access_token(ADO_CONNECTION_ID)

    def test_missing_ssm_param_raises_runtime(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_TENANT_ID", "tenant-abc")
        monkeypatch.setenv("AZURE_DEVOPS_CLIENT_ID", "client-abc")
        monkeypatch.delenv("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", raising=False)
        source_reader._ado_client_secret_cache = None
        with pytest.raises(RuntimeError, match="SSM_PARAM"):
            source_reader.ado_access_token(ADO_CONNECTION_ID)

    def test_returns_token_from_shared_oauth_helper(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_TENANT_ID", "tenant-abc")
        monkeypatch.setenv("AZURE_DEVOPS_CLIENT_ID", "client-abc")
        monkeypatch.setenv("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", "/prd/ado/secret")
        source_reader._ado_client_secret_cache = None

        fake_ssm = MagicMock()
        fake_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "super-secret"},
        }

        # Import target for the wrapper's deferred import so the patch
        # lands on the right module reference.
        import shared.entra_id_oauth  # noqa: WPS433
        captured = {}

        def _fake_refresh(**kwargs):
            captured.update(kwargs)
            return "azdo-access-token-value"

        with patch("boto3.client", return_value=fake_ssm), \
             patch.object(shared.entra_id_oauth, "refresh_access_token",
                          side_effect=_fake_refresh):
            token = source_reader.ado_access_token(ADO_CONNECTION_ID)

        assert token == "azdo-access-token-value"
        assert captured["connection_id"] == ADO_CONNECTION_ID
        assert captured["client_id"] == "client-abc"
        assert captured["client_secret"] == "super-secret"
        assert captured["tenant_id"] == "tenant-abc"
        assert captured["pk_prefix"] == "AZDO_TOKEN#"
        assert captured["scopes"].startswith(
            "499b84ac-1321-427f-aa17-267ca6975798/.default"
        )

    def test_refresh_failure_raises_runtime(self, monkeypatch):
        monkeypatch.setenv("AZURE_DEVOPS_TENANT_ID", "tenant-abc")
        monkeypatch.setenv("AZURE_DEVOPS_CLIENT_ID", "client-abc")
        monkeypatch.setenv("AZURE_DEVOPS_CLIENT_SECRET_SSM_PARAM", "/prd/ado/secret")
        source_reader._ado_client_secret_cache = None

        fake_ssm = MagicMock()
        fake_ssm.get_parameter.return_value = {
            "Parameter": {"Value": "super-secret"},
        }
        import shared.entra_id_oauth  # noqa: WPS433
        with patch("boto3.client", return_value=fake_ssm), \
             patch.object(shared.entra_id_oauth, "refresh_access_token",
                          return_value=None):
            with pytest.raises(RuntimeError, match="reauth"):
                source_reader.ado_access_token(ADO_CONNECTION_ID)


def _client_error(code):
    from botocore.exceptions import ClientError  # noqa: WPS433
    return ClientError({"Error": {"Code": code, "Message": ""}}, "operation")


class TestReadSourceFetchFileAdo:
    """ADO items API fetcher.

    Mocks urlopen; asserts the URL shape and cap enforcement.
    """

    def _stub_urlopen(self, body_bytes):
        calls = []

        def _fake(req, timeout=None):  # noqa: ARG002
            url = req.full_url if hasattr(req, "full_url") else str(req)
            calls.append(url)
            resp = MagicMock()
            resp.read.return_value = body_bytes
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        return _fake, calls

    def test_url_contains_required_params(self):
        fake, calls = self._stub_urlopen(b"line-a\nline-b\n")
        with patch("urllib.request.urlopen", side_effect=fake):
            source_reader.fetch_file_ado(
                ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                "src/Foo.cs", "azdo-token", ref=None,
            )
        assert len(calls) == 1
        url = calls[0]
        assert url.startswith(
            f"https://dev.azure.com/{ADO_ORG}/{ADO_PROJECT}"
            f"/_apis/git/repositories/{ADO_REPO_BARE}/items?"
        )
        # Order-insensitive param assertions:
        assert "path=src%2FFoo.cs" in url
        assert "api-version=7.1" in url
        # $ is quoted as %24; either representation is acceptable.
        assert ("%24format=text" in url) or ("$format=text" in url)

    def test_ref_passed_through_as_version_descriptor(self):
        fake, calls = self._stub_urlopen(b"x\n")
        sha = "deadbeef" * 5
        with patch("urllib.request.urlopen", side_effect=fake):
            source_reader.fetch_file_ado(
                ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                "src/Foo.cs", "azdo-token", ref=sha,
            )
        url = calls[0]
        assert f"versionDescriptor.version={sha}" in url
        assert "versionDescriptor.versionType=commit" in url

    def test_no_ref_omits_version_descriptor(self):
        fake, calls = self._stub_urlopen(b"x\n")
        with patch("urllib.request.urlopen", side_effect=fake):
            source_reader.fetch_file_ado(
                ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                "src/Foo.cs", "azdo-token", ref=None,
            )
        url = calls[0]
        assert "versionDescriptor.version=" not in url
        assert "versionDescriptor.versionType=" not in url

    def test_returns_raw_bytes(self):
        payload = b"one\ntwo\nthree\n"
        fake, _ = self._stub_urlopen(payload)
        with patch("urllib.request.urlopen", side_effect=fake):
            got = source_reader.fetch_file_ado(
                ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                "src/Foo.cs", "azdo-token",
            )
        assert got == payload

    def test_over_cap_raises_valueerror(self):
        oversize = b"x" * (source_reader.MAX_FILE_BYTES + 100)
        fake, _ = self._stub_urlopen(oversize)
        with patch("urllib.request.urlopen", side_effect=fake):
            with pytest.raises(ValueError, match="read-source cap"):
                source_reader.fetch_file_ado(
                    ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                    "src/Big.cs", "azdo-token",
                )

    def test_json_envelope_response_raises_valueerror(self):
        """If ADO returns a JSON envelope, the URL builder dropped
        $format=text or picked the wrong Accept header. Better to
        surface it as a distinct error than let splitlines see one
        giant line."""
        envelope = b'{"objectId": "..."}'
        fake, _ = self._stub_urlopen(envelope)
        with patch("urllib.request.urlopen", side_effect=fake):
            with pytest.raises(ValueError, match="JSON envelope"):
                source_reader.fetch_file_ado(
                    ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                    "src/Foo.cs", "azdo-token",
                )

    def test_authorization_header_set(self):
        captured_headers = {}

        def _fake(req, timeout=None):  # noqa: ARG002
            captured_headers.update(dict(req.header_items()))
            resp = MagicMock()
            resp.read.return_value = b"x\n"
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("urllib.request.urlopen", side_effect=_fake):
            source_reader.fetch_file_ado(
                ADO_ORG, ADO_PROJECT, ADO_REPO_BARE,
                "src/Foo.cs", "the-token",
            )
        # urllib normalizes header names to Title-Case.
        assert captured_headers.get("Authorization") == "Bearer the-token"
        assert captured_headers.get("Accept") == "text/plain"


class TestReadSourceRouter:
    """platform dispatch in handle_read_source.

    Verifies the auto-detected repo shape lines up with the workspace's
    connection, and that the platform field lands in every response.
    """

    def test_platform_from_repo_owner_name_is_github(self):
        assert source_reader.platform_from_repo("owner/name") == "github"

    def test_platform_from_repo_triplet_is_ado(self):
        assert (
            source_reader.platform_from_repo("org/project/repo")
            == "azure_devops"
        )

    def test_platform_from_repo_rejects_wrong_shape(self):
        assert source_reader.platform_from_repo("noslash") is None
        assert source_reader.platform_from_repo("a/b/c/d") is None
        assert source_reader.platform_from_repo("") is None
        assert source_reader.platform_from_repo("a//b") is None

    def test_github_workspace_owner_name_repo_dispatches_to_github(
        self, workspaces_table,
    ):
        file_text = "\n".join(f"line-{i}" for i in range(1, 21))
        patches, _ = _stub_github_success(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID, "repo": REPO,
                     "file_path": FILE_PATH, "line": 10, "context_lines": 2},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "github"
        assert payload["anchor_line"] == 10

    def test_ado_workspace_triplet_repo_dispatches_to_ado(
        self, workspaces_table,
    ):
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        file_text = "\n".join(f"line-{i}" for i in range(1, 21))

        def _fake_urlopen(req, timeout=None):  # noqa: ARG002
            resp = MagicMock()
            resp.read.return_value = file_text.encode()
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             patch.object(source_reader, "ado_access_token",
                          return_value="azdo-token"), \
             _patch_user_workspaces([ADO_WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {"workspace_id": ADO_WORKSPACE_ID, "repo": ADO_TRIPLET,
                 "file_path": "src/Foo.cs", "line": 10, "context_lines": 2},
                USER_EMAIL,
            )
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "azure_devops"
        assert payload["repo"] == ADO_TRIPLET
        assert payload["anchor_line"] == 10

    def test_github_workspace_ado_shape_repo_errors(self, workspaces_table):
        """Cross-platform sanity: an ADO-shaped repo on a GitHub-only
        workspace must NOT silently route to the GitHub fetcher."""
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {"workspace_id": WORKSPACE_ID, "repo": ADO_TRIPLET,
                 "file_path": "src/Foo.cs", "line": 10},
                USER_EMAIL,
            )
        assert "error" in result
        # Platform resolver sees GITHUB_INSTALLATION# (fixture seeded)
        # and returns platform=github; router then rejects the triplet.
        assert "azure_devops" in result["error"] and "github" in result["error"]

    def test_ado_workspace_github_shape_repo_errors(self, workspaces_table):
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        with _patch_user_workspaces([ADO_WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {"workspace_id": ADO_WORKSPACE_ID, "repo": REPO,
                 "file_path": "src/Foo.cs", "line": 10},
                USER_EMAIL,
            )
        assert "error" in result
        assert "github" in result["error"] and "azure_devops" in result["error"]

    def test_invalid_repo_shape_rejected_before_platform_lookup(
        self, workspaces_table,
    ):
        with _patch_user_workspaces([WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {"workspace_id": WORKSPACE_ID, "repo": "noslashhere",
                 "file_path": "src/Foo.cs", "line": 10},
                USER_EMAIL,
            )
        assert "error" in result
        # Message advertises both accepted shapes.
        assert "owner/name" in result["error"]
        assert "org/project/repo" in result["error"]

    def test_no_connection_returns_platform_agnostic_error(self):
        """A workspace with neither GitHub nor ADO rows returns the
        code-hosting-not-linked error, not the GitHub-specific one."""
        # Fresh table with no rows.
        client = boto3.client("dynamodb", region_name="us-west-2")
        client.create_table(
            TableName=WORKSPACES_TABLE,
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
        with _patch_user_workspaces(["ws-no-connections"]):
            result = mcp_module.handle_read_source(
                {"workspace_id": "ws-no-connections", "repo": REPO,
                 "file_path": FILE_PATH, "line": 10},
                USER_EMAIL,
            )
        assert "code-hosting integration" in result["error"]

    def test_ado_at_graph_sha_resolves_bare_to_triplet(
        self, workspaces_table, graph_tables,
    ):
        """+ integration: at_graph_sha on an ADO
        workspace must use the ADO triplet resolver, not the GitHub
        owner resolver, when the graph row is a bare repo name."""
        _seed_azdo_repo_row(workspaces_table, ADO_WORKSPACE_ID)
        graph_sha = "c" * 40
        sym = "csharp:sig::Sig::Do"
        _seed_defines_bare_repo(
            graph_tables, ADO_WORKSPACE_ID, ADO_REPO_BARE, sym,
            "src/Sig.cs", 5, graph_sha,
        )

        captured_urls = []

        def _fake_urlopen(req, timeout=None):  # noqa: ARG002
            url = req.full_url if hasattr(req, "full_url") else str(req)
            captured_urls.append(url)
            resp = MagicMock()
            resp.read.return_value = b"1\n2\n3\n4\n5\n6\n7\n"
            resp.__enter__ = lambda s: s
            resp.__exit__ = lambda s, *a: None
            return resp

        with patch("urllib.request.urlopen", side_effect=_fake_urlopen), \
             patch.object(source_reader, "ado_access_token",
                          return_value="azdo-token"), \
             _patch_user_workspaces([ADO_WORKSPACE_ID]):
            result = mcp_module.handle_read_source(
                {
                    "workspace_id": ADO_WORKSPACE_ID,
                    "symbol": sym,
                    "at_graph_sha": True,
                    "context_lines": 2,
                },
                USER_EMAIL,
            )
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "azure_devops"
        assert payload["repo"] == ADO_TRIPLET
        # graph_sha pinned to the ADO items URL as a commit versionType.
        contents = [u for u in captured_urls if "/_apis/git/" in u]
        assert contents
        assert f"versionDescriptor.version={graph_sha}" in contents[-1]
        assert "versionDescriptor.versionType=commit" in contents[-1]


# ===========================================================================
# ADR-050 -- GitLab as a first-class read_source platform. The load-bearing
# rule (Decision 6): GitLab supports nested subgroups, so a slug is
# group/{...subgroups}/project with a VARIABLE number of slashes -- slash
# count is AMBIGUOUS (a 2-slash slug looks exactly like an ADO triplet).
# The platform is therefore resolved by DDB lookup of the enrolled
# GITLAB_REPO# rows, NOT by shape. These tests prove that.
# ===========================================================================


GITLAB_WORKSPACE_ID = "ws-rs-gitlab-test"
GITLAB_CONNECTION_ID = "conn-gitlab-abc123"
# 2-slash slug (group/subgroup/project). source_reader.platform_from_repo
# would call this 'azure_devops' -- the whole point of Decision 6 is that
# the DDB lookup overrides that shape inference.
GITLAB_SLUG_NESTED = "acme/backend/api"
# Deeper nesting -- 3 slashes, which slash-count inference rejects outright.
GITLAB_SLUG_DEEP = "acme/platform/backend/api"


def _seed_gitlab_repo_row(
    table, workspace_id, slug=GITLAB_SLUG_NESTED,
    connection_id=GITLAB_CONNECTION_ID,
):
    """Seed a GITLAB_REPO# row matching what the GitLab enroll flow writes
    (ADR-050 Decision 7): SK = GITLAB_REPO#{slug}, own connection_id,
    provider:"gitlab", slug = URL-decoded full project path."""
    table.put_item(Item={
        "PK": f"WS#{workspace_id}",
        "SK": f"GITLAB_REPO#{slug}",
        "workspace_id": workspace_id,
        "provider": "gitlab",
        "slug": slug,
        "connection_id": connection_id,
        "default_branch": "main",
        "last_commit_sha": "e" * 40,
        "enabled": True,
    })


class TestReadSourceResolvePlatformGitLab:
    """Decision 6: platform resolved by enrolled-repo DDB lookup."""

    def test_gitlab_direct_key_when_repo_hint_is_slug(self, workspaces_table):
        _seed_gitlab_repo_row(workspaces_table, GITLAB_WORKSPACE_ID)
        info = source_reader.resolve_platform(
            GITLAB_WORKSPACE_ID, repo_hint=GITLAB_SLUG_NESTED,
        )
        assert info == {
            "platform": "gitlab",
            "connection_id": GITLAB_CONNECTION_ID,
            "slug": GITLAB_SLUG_NESTED,
        }

    def test_gitlab_query_fallback_when_no_repo_hint(self, workspaces_table):
        _seed_gitlab_repo_row(workspaces_table, GITLAB_WORKSPACE_ID)
        info = source_reader.resolve_platform(GITLAB_WORKSPACE_ID)
        assert info["platform"] == "gitlab"
        assert info["connection_id"] == GITLAB_CONNECTION_ID
        assert info["slug"] == GITLAB_SLUG_NESTED

    def test_nested_subgroup_slug_resolves_gitlab_not_ado_by_shape(
        self, workspaces_table,
    ):
        """LOAD-BEARING: a 2-slash slug (group/subgroup/project) has the
        SAME shape as an ADO org/project/repo triplet. Slash-count
        inference would misroute it to Azure DevOps. Because the ONLY
        enrolled row is a GITLAB_REPO# row (no AZDO_REPO#), the DDB
        lookup must resolve it to gitlab -- proving resolution is by
        enrolled-repo lookup, not slash count."""
        # Sanity: the bare shape inference disagrees with the truth.
        assert (
            source_reader.platform_from_repo(GITLAB_SLUG_NESTED)
            == "azure_devops"
        )
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug=GITLAB_SLUG_NESTED,
        )
        info = source_reader.resolve_platform(
            GITLAB_WORKSPACE_ID, repo_hint=GITLAB_SLUG_NESTED,
        )
        assert info["platform"] == "gitlab"
        assert info["slug"] == GITLAB_SLUG_NESTED

    def test_github_short_circuits_gitlab(self, workspaces_table):
        """Resolution order GitHub -> ADO -> GitLab: a workspace with both
        a GitHub install and a GitLab repo resolves to GitHub."""
        # WORKSPACE_ID already has GITHUB_INSTALLATION# from the fixture.
        _seed_gitlab_repo_row(workspaces_table, WORKSPACE_ID)
        info = source_reader.resolve_platform(WORKSPACE_ID)
        assert info["platform"] == "github"

    def test_provider_hint_pins_to_gitlab_over_github(self, workspaces_table):
        """An explicit provider='gitlab' hint bypasses the GitHub
        short-circuit and resolves the GitLab row directly."""
        # WORKSPACE_ID has a GitHub install; seed a GitLab row too.
        _seed_gitlab_repo_row(workspaces_table, WORKSPACE_ID)
        info = source_reader.resolve_platform(
            WORKSPACE_ID, repo_hint=GITLAB_SLUG_NESTED, provider="gitlab",
        )
        assert info["platform"] == "gitlab"
        assert info["connection_id"] == GITLAB_CONNECTION_ID

    def test_provider_hint_gitlab_returns_none_when_no_row(self, workspaces_table):
        info = source_reader.resolve_platform(
            "ws-no-gitlab", provider="gitlab",
        )
        assert info is None

    def test_gitlab_direct_key_miss_falls_through_to_query(self, workspaces_table):
        """A wrong-slug hint shouldn't hide an existing GitLab row -- the
        fallback query still finds it."""
        _seed_gitlab_repo_row(workspaces_table, GITLAB_WORKSPACE_ID)
        info = source_reader.resolve_platform(
            GITLAB_WORKSPACE_ID, repo_hint="wrong/group/project",
        )
        assert info["platform"] == "gitlab"

    def test_none_when_no_gitlab_row(self, workspaces_table):
        assert source_reader.resolve_platform(
            "ws-empty-no-gitlab",
        ) is None


class TestReadSourceResolveFullGitLabSlug:
    """Decision 6: bare project-name -> full nested slug for at_graph_sha."""

    def test_passthrough_when_already_has_slash(self, workspaces_table):
        assert source_reader.resolve_full_gitlab_slug(
            GITLAB_WORKSPACE_ID, GITLAB_SLUG_DEEP,
        ) == GITLAB_SLUG_DEEP

    def test_resolves_bare_to_full_nested_slug(self, workspaces_table):
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug=GITLAB_SLUG_DEEP,
        )
        # graph stores the bare project tail 'api'; must recover the full
        # variable-depth slug.
        result = source_reader.resolve_full_gitlab_slug(
            GITLAB_WORKSPACE_ID, "api",
        )
        assert result == GITLAB_SLUG_DEEP

    def test_returns_none_when_bare_missing(self, workspaces_table):
        assert source_reader.resolve_full_gitlab_slug(
            GITLAB_WORKSPACE_ID, "does-not-exist",
        ) is None

    def test_returns_none_when_ambiguous(self, workspaces_table):
        """Two GitLab projects with the same bare tail in different
        subgroups must not silently resolve to one."""
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug="acme/team-a/api",
        )
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug="acme/team-b/api",
            connection_id="conn-other",
        )
        assert source_reader.resolve_full_gitlab_slug(
            GITLAB_WORKSPACE_ID, "api",
        ) is None

    def test_returns_none_on_empty_input(self, workspaces_table):
        assert source_reader.resolve_full_gitlab_slug(
            GITLAB_WORKSPACE_ID, "",
        ) is None


class TestReadSourceGitLabAccessToken:
    """Token-mint wrapper reaches shared.oauth.gitlab with the right
    args + error surface."""

    def test_missing_connection_id_raises(self):
        with pytest.raises(ValueError):
            source_reader.gitlab_access_token("")

    def test_returns_token_from_shared_oauth_helper(self, monkeypatch):
        monkeypatch.setenv("OAUTH_KMS_KEY_ARN", "arn:aws:kms:us-west-2:1:key/abc")
        import shared.oauth.gitlab as gitlab_oauth  # noqa: WPS433
        captured = {}

        def _fake_get_valid(**kwargs):
            captured.update(kwargs)
            return "gitlab-access-token-value"

        # ADR-050 v1.1: read_source now routes through the cached,
        # single-flight get_valid_access_token (not raw refresh).
        # v2.0: resolves the connection's instance + returns its base_url.
        from shared.gitlab_instances import GitLabInstance
        _inst = GitLabInstance("default", "GitLab.com", "https://gitlab.com", "", "")
        with patch.object(gitlab_oauth, "get_valid_access_token",
                          side_effect=_fake_get_valid), \
             patch.object(gitlab_oauth, "instance_for_connection",
                          return_value=_inst):
            token, base_url = source_reader.gitlab_access_token(
                GITLAB_CONNECTION_ID,
            )
        assert token == "gitlab-access-token-value"
        assert base_url == "https://gitlab.com"
        assert captured["connection_id"] == GITLAB_CONNECTION_ID
        assert captured["workspaces_table"] == WORKSPACES_TABLE

    def test_refresh_failure_raises_runtime(self, monkeypatch):
        import shared.oauth.gitlab as gitlab_oauth  # noqa: WPS433
        from shared.gitlab_instances import GitLabInstance
        _inst = GitLabInstance("default", "GitLab.com", "https://gitlab.com", "", "")
        with patch.object(gitlab_oauth, "get_valid_access_token",
                          return_value=None), \
             patch.object(gitlab_oauth, "instance_for_connection",
                          return_value=_inst):
            with pytest.raises(RuntimeError, match="reauth"):
                source_reader.gitlab_access_token(GITLAB_CONNECTION_ID)


class TestReadSourceFetchFileGitLab:
    """GitLab raw-file fetcher: ref pinning, default-branch resolution
    when no ref, and the byte cap."""

    def test_uses_supplied_ref_and_skips_default_branch_lookup(self):
        import shared.gitlab_api as gitlab_api  # noqa: WPS433
        sha = "deadbeef" * 5
        with patch.object(gitlab_api, "get_default_branch") as mock_branch, \
             patch.object(gitlab_api, "get_file_content",
                          return_value=b"a\nb\nc\n") as mock_get:
            raw = source_reader.fetch_file_gitlab(
                GITLAB_SLUG_DEEP, "src/main.py", "gl-token", ref=sha,
            )
        assert raw == b"a\nb\nc\n"
        mock_branch.assert_not_called()
        assert mock_get.call_args.args == (
            "gl-token", GITLAB_SLUG_DEEP, "src/main.py", sha,
        )

    def test_resolves_default_branch_when_no_ref(self):
        import shared.gitlab_api as gitlab_api  # noqa: WPS433
        with patch.object(gitlab_api, "get_default_branch",
                          return_value="main") as mock_branch, \
             patch.object(gitlab_api, "get_file_content",
                          return_value=b"x\n") as mock_get:
            source_reader.fetch_file_gitlab(
                GITLAB_SLUG_NESTED, "src/main.py", "gl-token", ref=None,
            )
        mock_branch.assert_called_once_with(
            "gl-token", GITLAB_SLUG_NESTED, base_url=None)
        # default-branch tip used as the ref.
        assert mock_get.call_args.args[3] == "main"

    def test_over_cap_raises_valueerror(self):
        import shared.gitlab_api as gitlab_api  # noqa: WPS433
        oversize = b"x" * (source_reader.MAX_FILE_BYTES + 100)
        with patch.object(gitlab_api, "get_file_content", return_value=oversize):
            with pytest.raises(ValueError, match="read-source cap"):
                source_reader.fetch_file_gitlab(
                    GITLAB_SLUG_DEEP, "src/Big.py", "gl-token", ref="main",
                )


class TestReadSourceRouterGitLab:
    """handle_read_source dispatch: a GitLab-connected workspace fetches
    via the GitLab client, and the nested-subgroup slug is NOT rejected
    by the slash-count shape check (Decision 6)."""

    def _patch_gitlab_fetch(self, file_text, default_branch="main"):
        import shared.gitlab_api as gitlab_api  # noqa: WPS433
        return [
            patch.object(gitlab_api, "get_default_branch",
                         return_value=default_branch),
            patch.object(gitlab_api, "get_file_content",
                         return_value=file_text.encode()),
            patch.object(source_reader, "gitlab_access_token",
                         return_value=("gl-token", "https://gitlab.com")),
        ]

    def test_nested_slug_workspace_dispatches_to_gitlab(self, workspaces_table):
        """End-to-end: a 2-slash GitLab slug (ADO-shaped) on a
        GitLab-connected workspace fetches via GitLab and reports
        platform='gitlab' -- the shape check does NOT misroute it."""
        _seed_gitlab_repo_row(workspaces_table, GITLAB_WORKSPACE_ID)
        file_text = "\n".join(f"line-{i}" for i in range(1, 21))
        patches = self._patch_gitlab_fetch(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([GITLAB_WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": GITLAB_WORKSPACE_ID,
                     "repo": GITLAB_SLUG_NESTED,
                     "file_path": "src/main.py", "line": 10,
                     "context_lines": 2},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "gitlab"
        assert payload["repo"] == GITLAB_SLUG_NESTED
        assert payload["anchor_line"] == 10

    def test_deep_slug_workspace_dispatches_to_gitlab(self, workspaces_table):
        """A 3-slash slug (which slash-count inference rejects outright)
        still reads when resolved via the GITLAB_REPO# lookup."""
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug=GITLAB_SLUG_DEEP,
        )
        file_text = "\n".join(f"L{i}" for i in range(1, 21))
        patches = self._patch_gitlab_fetch(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([GITLAB_WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": GITLAB_WORKSPACE_ID,
                     "repo": GITLAB_SLUG_DEEP,
                     "file_path": "src/main.py", "line": 5,
                     "context_lines": 2},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "gitlab"
        assert payload["repo"] == GITLAB_SLUG_DEEP

    def test_provider_hint_forces_gitlab_on_mixed_workspace(self, workspaces_table):
        """A workspace with a GitHub install AND a GitLab row: an explicit
        provider='gitlab' hint routes the read to GitLab despite the
        GitHub short-circuit that would otherwise win."""
        _seed_gitlab_repo_row(workspaces_table, WORKSPACE_ID)  # + fixture GH row
        file_text = "\n".join(f"g{i}" for i in range(1, 21))
        patches = self._patch_gitlab_fetch(file_text)
        _enter(patches)
        try:
            with _patch_user_workspaces([WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {"workspace_id": WORKSPACE_ID,
                     "repo": GITLAB_SLUG_NESTED, "provider": "gitlab",
                     "file_path": "src/main.py", "line": 10,
                     "context_lines": 2},
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "gitlab"

    def test_gitlab_at_graph_sha_resolves_bare_to_full_slug(
        self, workspaces_table, graph_tables,
    ):
        """at_graph_sha on a GitLab workspace: the graph row carries the
        bare project tail; the resolver must recover the full nested slug
        via source_reader.resolve_full_gitlab_slug and pin the read to the
        graph_sha."""
        _seed_gitlab_repo_row(
            workspaces_table, GITLAB_WORKSPACE_ID, slug=GITLAB_SLUG_DEEP,
        )
        graph_sha = "f" * 40
        sym = "python:api.handler::create_token"
        # graph stores repo as the bare project tail 'api'.
        _seed_defines_bare_repo(
            graph_tables, GITLAB_WORKSPACE_ID, "api", sym,
            "src/handler.py", 5, graph_sha,
        )
        import shared.gitlab_api as gitlab_api  # noqa: WPS433
        captured = {}

        def _fake_get_file_content(token, slug, path, ref, base_url=None):
            captured["slug"] = slug
            captured["ref"] = ref
            return b"1\n2\n3\n4\n5\n6\n7\n"

        patches = [
            patch.object(gitlab_api, "get_file_content",
                         side_effect=_fake_get_file_content),
            patch.object(source_reader, "gitlab_access_token",
                         return_value=("gl-token", "https://gitlab.com")),
        ]
        _enter(patches)
        try:
            with _patch_user_workspaces([GITLAB_WORKSPACE_ID]):
                result = mcp_module.handle_read_source(
                    {
                        "workspace_id": GITLAB_WORKSPACE_ID,
                        "symbol": sym,
                        "at_graph_sha": True,
                        "context_lines": 2,
                    },
                    USER_EMAIL,
                )
        finally:
            _exit(patches)
        assert "text" in result, f"expected success, got {result}"
        payload = json.loads(result["text"])
        assert payload["platform"] == "gitlab"
        # Bare 'api' recovered to the full nested slug.
        assert payload["repo"] == GITLAB_SLUG_DEEP
        assert captured["slug"] == GITLAB_SLUG_DEEP
        # Pinned to the graph_sha (a commit SHA passed straight through as ref).
        assert payload["graph_sha"] == graph_sha
        assert captured["ref"] == graph_sha
