"""Unit tests for the ``read_source`` MCP tool on the OutcomeOps gateway.

Covers ``handle_read_source`` (plan-read-source-adjudicator chunk 01):
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
    yield


@pytest.fixture(autouse=True)
def _reset_mcp_caches():
    mcp_module._dynamodb = None
    mcp_module._s3vectors_client = None
    mcp_module._bedrock_client = None
    mcp_module._ssm_client = None
    mcp_module._github_app_config_cache = None
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
        assert "installation" in result["error"]


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
# Chunk 02 -- SHA-skew handling: graph resolution + window relocation.
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
        assert mcp_module._read_source_bare_name(
            "python:lambda.auth.handler::require_auth"
        ) == "require_auth"

    def test_bare_name_extracts_namespaced_tail(self):
        assert mcp_module._read_source_bare_name(
            "python:m::Service::run"
        ) == "run"

    def test_bare_name_empty_input(self):
        assert mcp_module._read_source_bare_name("") == ""

    def test_relocate_finds_first_match(self):
        window = [
            {"line": 10, "text": "    pass"},
            {"line": 11, "text": "def greet(name):"},
            {"line": 12, "text": "    return name"},
        ]
        assert mcp_module._read_source_relocate(window, "greet") == 11

    def test_relocate_word_boundary_rejects_substring(self):
        window = [
            {"line": 1, "text": "def my_require_auth(x):"},
            {"line": 2, "text": "    pass"},
        ]
        # `require_auth` must NOT match `my_require_auth` thanks to \b.
        assert mcp_module._read_source_relocate(window, "require_auth") is None

    def test_relocate_returns_none_when_absent(self):
        window = [{"line": 1, "text": "nothing here"}]
        assert mcp_module._read_source_relocate(window, "greet") is None


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

    def test_at_graph_sha_missing_symbol_in_graph_errors(self, graph_tables):
        # Tables exist but no defines row for this symbol -> "missing".
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
# Chunk 04 -- integration-shaped: the "verified value beats a stale
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
