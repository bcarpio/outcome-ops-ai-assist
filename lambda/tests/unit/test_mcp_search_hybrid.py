# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the fused ``search_hybrid`` MCP tool.

``search_hybrid`` runs dual-channel RAG AND symbol-graph augmentation in one
call, returning both in a single ADR-058-grounded response so a client that
does not orchestrate two tool calls still gets the hybrid. The RAG lane and the
graph lane are each fail-soft: a failure in one must not drop the other, and the
``symbol_graph`` section always carries the grounding envelope.

Isolation mirrors ``test_mcp_server_symbol_graph_tools.py``: a fresh module load,
autouse ``mock_aws`` so no test touches real AWS. The RAG + augment lanes are
patched, so these tests exercise the fusion + fail-soft contract, not retrieval.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

USER_EMAIL = "user@example.com"
WORKSPACE_ID = "ws-hybrid-test"
OTHER_WORKSPACE_ID = "ws-hybrid-other"

# Required env so the mcp-server handler module loads.
os.environ["DYNAMODB_TABLE"] = "test-table"
os.environ["S3_VECTORS_BUCKET"] = "test-vectors-bucket"
os.environ["S3_VECTORS_INDEX"] = "test-vectors-index"
os.environ["KB_BUCKET"] = "test-kb-bucket"
os.environ["ENVIRONMENT"] = "test"
os.environ["MCP_BASE_URL"] = "http://test-alb.example.com"


def _load_mcp_module():
    mod_name = "mcp_server_handler_search_hybrid"
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


@pytest.fixture(autouse=True)
def _aws_mocks():
    boto3.DEFAULT_SESSION = None
    with mock_aws():
        yield
    boto3.DEFAULT_SESSION = None


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch):
    monkeypatch.setenv("SYMBOL_GRAPH_TABLE", "test-symbol-graph")
    monkeypatch.setenv("CODE_MAPS_TABLE", "test-code-maps")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    yield


def _patch_user_workspaces(workspace_ids):
    return patch.object(
        mcp_module, "get_user_workspace_ids", return_value=workspace_ids
    )


# A representative augment() out-dict (structural question that resolved).
def _augment_result(sites=None, skip_reason=None):
    return {
        "decision": SimpleNamespace(
            intent="structural", symbol_candidate="validate_input"
        ),
        "graph_block": "rendered block (unused by MCP)",
        "graph_relation": "callers",
        "graph_sites": sites
        if sites is not None
        else [
            {
                "file_path": "lambda/auth/handler.py",
                "line": 672,
                "repo": "owner/repo",
                "source_symbol_id": "python:lambda.auth.handler::verify_magic_token",
                "language": "python",
            }
        ],
        "graph_result_count": 1,
        "graph_sha": "sha-abc",
        "graph_staleness_seconds": 100,
        "graph_canonicals_truncated": False,
        "graph_skip_reason": skip_reason,
    }


_RAG_DOCS = [
    {
        "text": "ADR-033 dual-channel ...",
        "score": 0.81,
        "rerank_score": 0.64,
        "repo": "owner/repo",
        "type": "adr",
        "lane": "docs",
        "file_path": "docs/adr/ADR-033.md",
    },
    {
        "text": "# auth handler overview ...",
        "score": 0.26,
        "rerank_score": 0.52,
        "repo": "owner/repo",
        "type": "handler-group-summary",
        "lane": "code_map",
        "file_path": "lambda/auth/handler.py",
    },
]


class TestSearchHybrid:
    def test_missing_query(self):
        assert mcp_module.handle_search_hybrid({}, USER_EMAIL) == {
            "error": "query is required"
        }

    def test_returns_both_sections(self):
        with _patch_user_workspaces([WORKSPACE_ID]), patch.object(
            mcp_module, "search_knowledge_base", return_value=list(_RAG_DOCS)
        ), patch.object(
            mcp_module, "format_results", return_value="RAG BODY"
        ), patch(
            "shared.hybrid_augment.augment_with_symbol_graph",
            return_value=_augment_result(),
        ):
            out = mcp_module.handle_search_hybrid(
                {"query": "who calls validate_input"}, USER_EMAIL
            )
        payload = json.loads(out["text"])
        # Both lanes present.
        assert payload["rag_text"] == "RAG BODY"
        assert {s["lane"] for s in payload["rag"]} == {"docs", "code_map"}
        assert {s["type"] for s in payload["rag"]} == {
            "adr",
            "handler-group-summary",
        }
        sg = payload["symbol_graph"]
        assert sg["relation"] == "callers"
        assert sg["sites"][0]["file_path"] == "lambda/auth/handler.py"
        assert sg["sites"][0]["line"] == 672
        assert sg["intent"] == "structural"
        assert sg["symbol"] == "validate_input"

    def test_grounding_envelope_on_symbol_graph(self):
        with _patch_user_workspaces([WORKSPACE_ID]), patch.object(
            mcp_module, "search_knowledge_base", return_value=[]
        ), patch.object(
            mcp_module, "format_results", return_value=""
        ), patch(
            "shared.hybrid_augment.augment_with_symbol_graph",
            return_value=_augment_result(),
        ):
            out = mcp_module.handle_search_hybrid(
                {"query": "who calls validate_input"}, USER_EMAIL
            )
        sg = json.loads(out["text"])["symbol_graph"]
        assert sg["grounding"] == mcp_module._SYMBOL_GRAPH_GROUNDING
        assert sg["grounding"]["cite_as"] == "file_path:line"

    def test_rag_failsoft_keeps_graph(self):
        """RAG lane raises -> graph facts still returned."""
        with _patch_user_workspaces([WORKSPACE_ID]), patch.object(
            mcp_module,
            "search_knowledge_base",
            side_effect=RuntimeError("vector index down"),
        ), patch(
            "shared.hybrid_augment.augment_with_symbol_graph",
            return_value=_augment_result(),
        ):
            out = mcp_module.handle_search_hybrid(
                {"query": "who calls validate_input"}, USER_EMAIL
            )
        payload = json.loads(out["text"])
        assert payload["rag"] == []
        assert payload["rag_text"] == ""
        assert payload["symbol_graph"]["sites"][0]["line"] == 672
        assert payload["symbol_graph"]["grounding"]  # envelope intact

    def test_graph_failsoft_keeps_rag(self):
        """Graph lane raises -> RAG still returned, graph marked errored but
        still carries the grounding envelope."""
        with _patch_user_workspaces([WORKSPACE_ID]), patch.object(
            mcp_module, "search_knowledge_base", return_value=list(_RAG_DOCS)
        ), patch.object(
            mcp_module, "format_results", return_value="RAG BODY"
        ), patch(
            "shared.hybrid_augment.augment_with_symbol_graph",
            side_effect=RuntimeError("ddb down"),
        ):
            out = mcp_module.handle_search_hybrid(
                {"query": "who calls validate_input"}, USER_EMAIL
            )
        payload = json.loads(out["text"])
        assert payload["rag_text"] == "RAG BODY"
        assert len(payload["rag"]) == 2
        assert payload["symbol_graph"]["skip_reason"] == "augment_error"
        assert payload["symbol_graph"]["grounding"]

    def test_explicit_workspace_auth_denied(self):
        """An explicit workspace the caller cannot read is refused, and no
        retrieval runs."""
        with _patch_user_workspaces([]), patch.object(
            mcp_module, "search_knowledge_base"
        ) as sk, patch(
            "shared.hybrid_augment.augment_with_symbol_graph"
        ) as aug:
            out = mcp_module.handle_search_hybrid(
                {"query": "q", "workspace_id": OTHER_WORKSPACE_ID}, USER_EMAIL
            )
        assert "error" in out
        sk.assert_not_called()
        aug.assert_not_called()

    def test_advertised_and_registered(self):
        names = {t["name"] for t in mcp_module.MCP_TOOLS}
        assert "search_hybrid" in names
        assert "search_hybrid" in mcp_module.TOOL_HANDLERS
        # Schema shape.
        tool = next(t for t in mcp_module.MCP_TOOLS if t["name"] == "search_hybrid")
        assert tool["inputSchema"]["required"] == ["query"]
        assert "min_score" in tool["inputSchema"]["properties"]
