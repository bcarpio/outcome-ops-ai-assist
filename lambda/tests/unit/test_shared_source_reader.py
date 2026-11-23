# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``shared.source_reader`` additions used by chat
(ADR-030 v1.2): repo / commit resolution helpers, the window read, the
definition read, and the SOURCE LINES context block.

The provider dispatch and token minting are covered through the MCP
``read_source`` tool in ``test_mcp_server_read_source.py``; this file
covers the functions chat calls directly.
"""

from __future__ import annotations

import io
import sys
import urllib.error
from pathlib import Path
from unittest.mock import MagicMock, patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

from shared import source_reader  # noqa: E402

WS = "ws-src"
GITHUB_INFO = {"platform": "github", "installation_id": "42"}
FILE_TEXT = "\n".join(f"line {i}" for i in range(1, 201)) + "\n"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    for name in ("CHAT_SOURCE_LINES_ENABLED", "CHAT_SOURCE_LINES_PER_SYMBOL",
                 "CHAT_SOURCE_MAX_SYMBOLS", "CODE_MAPS_TABLE"):
        monkeypatch.delenv(name, raising=False)
    source_reader._dynamodb = None
    yield
    source_reader._dynamodb = None


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


class TestSourceLinesConfig:
    def test_defaults(self):
        cfg = source_reader.source_lines_config_from_env()
        assert cfg == {"enabled": True, "max_lines": 80, "max_definitions": 3}

    @pytest.mark.parametrize("value", ["false", "0", "No", "off"])
    def test_kill_switch(self, monkeypatch, value):
        monkeypatch.setenv("CHAT_SOURCE_LINES_ENABLED", value)
        assert source_reader.source_lines_config_from_env()["enabled"] is False

    def test_values_are_clamped(self, monkeypatch):
        monkeypatch.setenv("CHAT_SOURCE_LINES_PER_SYMBOL", "100000")
        monkeypatch.setenv("CHAT_SOURCE_MAX_SYMBOLS", "0")
        cfg = source_reader.source_lines_config_from_env()
        assert cfg["max_lines"] == 300
        assert cfg["max_definitions"] == 1

    def test_invalid_values_fall_back(self, monkeypatch):
        monkeypatch.setenv("CHAT_SOURCE_LINES_PER_SYMBOL", "lots")
        assert source_reader.source_lines_config_from_env()["max_lines"] == 80


# ---------------------------------------------------------------------------
# Repo + commit resolution
# ---------------------------------------------------------------------------


class TestFullRepoForPlatform:
    def test_full_slugs_pass_through(self):
        assert source_reader.full_repo_for_platform(WS, "o/r", "github") == ("o/r", None)
        assert source_reader.full_repo_for_platform(WS, "a/b/c", "azure_devops") == ("a/b/c", None)
        assert source_reader.full_repo_for_platform(WS, "g/s/p", "gitlab") == ("g/s/p", None)

    def test_bare_names_resolve(self):
        with patch.object(source_reader, "resolve_full_repo_name", return_value="o/r"):
            assert source_reader.full_repo_for_platform(WS, "r", "github") == ("o/r", None)
        with patch.object(source_reader, "resolve_full_ado_triplet", return_value="a/b/r"):
            assert source_reader.full_repo_for_platform(WS, "r", "azure_devops") == ("a/b/r", None)
        with patch.object(source_reader, "resolve_full_gitlab_slug", return_value="g/r"):
            assert source_reader.full_repo_for_platform(WS, "r", "gitlab") == ("g/r", None)

    @pytest.mark.parametrize("platform,fn,host", [
        ("github", "resolve_full_repo_name", "GitHub"),
        ("azure_devops", "resolve_full_ado_triplet", "Azure DevOps"),
        ("gitlab", "resolve_full_gitlab_slug", "GitLab"),
    ])
    def test_unresolvable_bare_name_is_an_error(self, platform, fn, host):
        with patch.object(source_reader, fn, return_value=None):
            repo, err = source_reader.full_repo_for_platform(WS, "r", platform)
        assert repo is None
        assert host in err


class TestGraphShaForRepo:
    def test_no_table_configured(self):
        assert source_reader.graph_sha_for_repo(WS, "o/r") is None

    def test_reads_pointer_by_bare_name(self, monkeypatch):
        monkeypatch.setenv("CODE_MAPS_TABLE", "cm")
        with mock_aws():
            client = boto3.client("dynamodb", region_name="us-west-2")
            client.create_table(
                TableName="cm",
                AttributeDefinitions=[{"AttributeName": "PK", "AttributeType": "S"},
                                      {"AttributeName": "SK", "AttributeType": "S"}],
                KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"},
                           {"AttributeName": "SK", "KeyType": "RANGE"}],
                BillingMode="PAY_PER_REQUEST",
            )
            boto3.resource("dynamodb", region_name="us-west-2").Table("cm").put_item(
                Item={"PK": f"SYMBOL_GRAPH#{WS}#r", "SK": "POINTER", "graph_sha": "abc123"})
            assert source_reader.graph_sha_for_repo(WS, "owner/r") == "abc123"

    def test_ddb_error_is_none(self, monkeypatch):
        monkeypatch.setenv("CODE_MAPS_TABLE", "cm")
        table = MagicMock()
        table.get_item.side_effect = ClientError({"Error": {"Code": "Boom"}}, "GetItem")
        ddb = MagicMock()
        ddb.Table.return_value = table
        with patch.object(source_reader, "_get_dynamodb", return_value=ddb):
            assert source_reader.graph_sha_for_repo(WS, "r") is None


# ---------------------------------------------------------------------------
# Window read
# ---------------------------------------------------------------------------


class TestFetchRaw:
    def test_dispatches_per_platform(self):
        with patch.object(source_reader, "installation_token", return_value="t"), \
                patch.object(source_reader, "fetch_file_github", return_value=b"gh") as gh:
            assert source_reader.fetch_raw(GITHUB_INFO, "o/r", "f.py", ref="s") == b"gh"
        gh.assert_called_once_with("o/r", "f.py", "t", ref="s")
        with patch.object(source_reader, "gitlab_access_token", return_value=("t", "https://gl")), \
                patch.object(source_reader, "fetch_file_gitlab", return_value=b"gl") as gl:
            info = {"platform": "gitlab", "connection_id": "c"}
            assert source_reader.fetch_raw(info, "g/p", "f.py") == b"gl"
        gl.assert_called_once_with("g/p", "f.py", "t", ref=None, base_url="https://gl")
        with patch.object(source_reader, "ado_access_token", return_value="t"), \
                patch.object(source_reader, "fetch_file_ado", return_value=b"ado") as ado:
            info = {"platform": "azure_devops", "connection_id": "c"}
            assert source_reader.fetch_raw(info, "a/b/c", "f.py") == b"ado"
        ado.assert_called_once_with("a", "b", "c", "f.py", "t", ref=None)


class TestReadWindow:
    def test_success_with_relocation(self):
        text = "a\nb\ndef target():\n    pass\n"
        with patch.object(source_reader, "fetch_raw", return_value=text.encode()):
            out = source_reader.read_window(
                GITHUB_INFO, "o/r", "f.py", 2, ref="s", context_lines=5,
                symbol="python:m::target")
        assert out["lines"][0] == {"line": 1, "text": "a"}
        assert out["relocated_to_line"] == 3
        assert out["relocated"] is True
        assert out["clipped_to_file_bounds"] is True
        assert out["platform"] == "github"

    def test_no_symbol_means_no_relocation(self):
        with patch.object(source_reader, "fetch_raw", return_value=b"x\n"):
            out = source_reader.read_window(GITHUB_INFO, "o/r", "f.py", 1, context_lines=0)
        assert out["relocated_to_line"] is None
        assert out["relocated"] is False

    @pytest.mark.parametrize("exc,expect", [
        (urllib.error.HTTPError("u", 404, "nf", {}, io.BytesIO()), "GitHub returned 404"),
        (urllib.error.URLError("dns"), "GitHub fetch failed"),
        (ValueError("too big"), "too big"),
        (RuntimeError("no app"), "no app"),
        (KeyError("x"), "read_source failed (see logs)"),
    ])
    def test_errors_become_error_dicts(self, exc, expect):
        with patch.object(source_reader, "fetch_raw", side_effect=exc):
            out = source_reader.read_window(GITHUB_INFO, "o/r", "f.py", 1)
        assert expect in out["error"]

    def test_host_labels(self):
        assert source_reader._host_label("gitlab") == "GitLab"
        assert source_reader._host_label("azure_devops") == "Azure DevOps"


# ---------------------------------------------------------------------------
# Definition read
# ---------------------------------------------------------------------------


def _definition(**over):
    d = {"workspace_id": WS, "symbol": "python:m::f", "repo": "r",
         "file_path": "m.py", "line": 50}
    d.update(over)
    return d


class TestReadDefinition:
    @pytest.mark.parametrize("over", [{"line": 0}, {"line": "x"}, {"repo": ""}])
    def test_incomplete_site(self, over):
        out = source_reader.read_definition(_definition(**over))
        assert out["error"] == "definition site is incomplete"

    def test_no_platform(self):
        with patch.object(source_reader, "resolve_platform", return_value=None):
            out = source_reader.read_definition(_definition())
        assert "no code-hosting connection" in out["error"]

    def test_repo_resolution_error(self):
        with patch.object(source_reader, "resolve_platform", return_value=GITHUB_INFO), \
                patch.object(source_reader, "full_repo_for_platform", return_value=(None, "no row")):
            assert source_reader.read_definition(_definition())["error"] == "no row"

    def test_window_error(self):
        with patch.object(source_reader, "resolve_platform", return_value=GITHUB_INFO), \
                patch.object(source_reader, "full_repo_for_platform", return_value=("o/r", None)), \
                patch.object(source_reader, "graph_sha_for_repo", return_value="sha"), \
                patch.object(source_reader, "read_window", return_value={"error": "GitHub returned 404"}):
            assert source_reader.read_definition(_definition())["error"] == "GitHub returned 404"

    def test_success_pins_graph_sha_and_starts_at_definition(self):
        with patch.object(source_reader, "resolve_platform", return_value=GITHUB_INFO), \
                patch.object(source_reader, "full_repo_for_platform", return_value=("o/r", None)), \
                patch.object(source_reader, "graph_sha_for_repo", return_value="sha1"), \
                patch.object(source_reader, "fetch_raw", return_value=FILE_TEXT.encode()) as fetch:
            out = source_reader.read_definition(_definition(), max_lines=10)
        fetch.assert_called_once_with(GITHUB_INFO, "o/r", "m.py", ref="sha1")
        assert out["ref"] == "sha1"
        assert out["lines"][0]["line"] == 48  # two leading lines
        assert out["lines"][-1]["line"] == 59
        assert out["truncated"] is True

    def test_relocated_anchor_moves_the_window(self):
        text = "\n".join(["x"] * 9 + ["def f():", "    return 1"]) + "\n"
        with patch.object(source_reader, "resolve_platform", return_value=GITHUB_INFO), \
                patch.object(source_reader, "full_repo_for_platform", return_value=("o/r", None)), \
                patch.object(source_reader, "graph_sha_for_repo", return_value=None), \
                patch.object(source_reader, "fetch_raw", return_value=text.encode()):
            out = source_reader.read_definition(_definition(line=3), max_lines=20)
        assert out["lines"][0]["line"] == 8
        assert out["lines"][-1]["text"] == "    return 1"
        assert out["truncated"] is False


class TestReadDefinitions:
    def test_dedupes_and_caps(self):
        defs = [_definition(), _definition(), _definition(line=60), _definition(line=70)]
        with patch.object(source_reader, "read_definition",
                          side_effect=lambda d, max_lines: {"symbol": d["line"]}) as rd:
            out = source_reader.read_definitions(defs, max_definitions=2, max_lines=5)
        assert [w["symbol"] for w in out] == [50, 60]
        assert rd.call_count == 2

    def test_empty(self):
        assert source_reader.read_definitions([]) == []


# ---------------------------------------------------------------------------
# Context block
# ---------------------------------------------------------------------------


def _window(**over):
    w = {"symbol": "python:m::f", "repo": "o/r", "file_path": "m.py",
         "ref": "0123456789abcdef", "lines": [{"line": 7, "text": "def f():"},
                                              {"line": 8, "text": "    raise E('x')"}],
         "truncated": False}
    w.update(over)
    return w


class TestBuildSourceLinesBlock:
    def test_empty_inputs(self):
        assert source_reader.build_source_lines_block([]) == ""
        assert source_reader.build_source_lines_block([_window(lines=[])]) == ""

    def test_renders_window_with_citation_header(self):
        block = source_reader.build_source_lines_block([_window()])
        assert block.startswith("SOURCE LINES")
        assert "### python:m::f -- o/r:m.py:7-8 @ 0123456789ab" in block
        assert "    8 |     raise E('x')" in block
        assert "continues" not in block

    def test_truncated_and_unpinned(self):
        block = source_reader.build_source_lines_block([_window(ref=None, truncated=True)])
        assert "@ default branch" in block
        assert "the definition continues" in block

    def test_unreadable_window_is_listed(self):
        block = source_reader.build_source_lines_block([{"symbol": "s", "error": "GitHub returned 404"}])
        assert "Source could not be read: GitHub returned 404" in block

    def test_budget_cap(self):
        big = _window(lines=[{"line": i, "text": "x" * 100} for i in range(1, 50)])
        block = source_reader.build_source_lines_block([big, big], max_chars=6000)
        assert block.count("### ") == 1
        assert "further source windows omitted" in block
