"""Unit tests for the shared KB retrieval module (lambda/shared/retrieval.py).

Covers the consolidation contract: vector-query normalization,
snippet windowing, token estimation, and the rerank delegation +
graceful fallback. The chat (query-kb) and MCP regressions live in
their own files; this file pins the shared mechanics directly.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

_LAMBDA_ROOT = Path(__file__).resolve().parents[2]
if str(_LAMBDA_ROOT) not in sys.path:
    sys.path.insert(0, str(_LAMBDA_ROOT))

from shared import retrieval  # noqa: E402


# ---------------------------------------------------------------------------
# estimate_tokens
# ---------------------------------------------------------------------------


def test_estimate_tokens_rough_4_chars_per_token():
    assert retrieval.estimate_tokens("") == 0
    assert retrieval.estimate_tokens("a" * 400) == 100


# ---------------------------------------------------------------------------
# make_snippet
# ---------------------------------------------------------------------------


def test_make_snippet_returns_full_text_when_under_budget():
    text = "Short doc. Two sentences."
    assert retrieval.make_snippet(text, "doc", max_tokens=500) == text


def test_make_snippet_windows_long_text_and_stays_sentence_bounded():
    # ~ 4000 chars => well over a 50-token (200 char) budget.
    body = ". ".join(f"Sentence number {i} about widgets" for i in range(200))
    out = retrieval.make_snippet(body, "widgets", max_tokens=50)
    assert len(out) < len(body)
    # Sentence-bounded: the snippet should not end mid-word relative to
    # the source (we join whole sentences). It should contain the term.
    assert "widgets" in out


def test_make_snippet_targets_query_relevant_window():
    text = (
        "Intro about nothing in particular. "
        "More filler text here. "
        "The MAGIC_LINK_EXPIRY constant is 3600 seconds. "
        "Closing filler unrelated to anything."
    )
    out = retrieval.make_snippet(text, "MAGIC_LINK_EXPIRY expiry", max_tokens=12)
    assert "MAGIC_LINK_EXPIRY" in out


def test_make_snippet_prefix_fallback_when_no_query_overlap():
    text = " ".join(f"alpha{i} beta{i} gamma{i}." for i in range(100))
    out = retrieval.make_snippet(text, "zzz-no-overlap", max_tokens=10)
    # No overlap => start at the top; first token should appear.
    assert out.startswith("alpha0")


# ---------------------------------------------------------------------------
# query_vectors
# ---------------------------------------------------------------------------


def _fake_s3vectors(vectors):
    client = MagicMock()
    client.query_vectors.return_value = {"vectors": vectors}
    return client


def test_query_vectors_normalizes_sorts_and_caps():
    client = _fake_s3vectors(
        [
            {"key": "k1", "distance": 0.6, "metadata": {"content": "low", "repo": "r1", "type": "doc"}},
            {"key": "k2", "distance": 0.1, "metadata": {"content": "high", "repo": "r2", "type": "adr"}},
            {"key": "k3", "distance": 0.3, "metadata": {"content": "mid", "repo": "r3", "type": "doc"}},
        ]
    )
    out = retrieval.query_vectors(
        s3vectors_client=client,
        vector_bucket="b",
        vector_index="i",
        query_embedding=[0.0] * 4,
        top_k=2,
    )
    # Sorted by similarity (1 - distance) desc, capped to top_k=2.
    assert [d["key"] for d in out] == ["k2", "k3"]
    assert out[0]["score"] == 0.9
    assert out[0]["text"] == "high"
    assert out[0]["repo"] == "r2"


def test_query_vectors_client_side_doc_type_filter():
    client = _fake_s3vectors(
        [
            {"key": "k1", "distance": 0.1, "metadata": {"content": "a", "type": "adr"}},
            {"key": "k2", "distance": 0.2, "metadata": {"content": "b", "type": "readme"}},
        ]
    )
    out = retrieval.query_vectors(
        s3vectors_client=client,
        vector_bucket="b",
        vector_index="i",
        query_embedding=[0.0] * 4,
        top_k=10,
        doc_types=["adr"],
    )
    assert [d["key"] for d in out] == ["k1"]


def test_query_vectors_derives_github_repo_from_folder_path():
    client = _fake_s3vectors(
        [
            {"key": "k1", "distance": 0.1, "metadata": {"content": "x", "source": "github", "folder_path": "owner/repo/sub"}},
        ]
    )
    out = retrieval.query_vectors(
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        query_embedding=[0.0] * 4, top_k=5,
    )
    assert out[0]["repo"] == "owner"


def test_query_vectors_text_truncate_disabled_with_zero():
    big = "z" * 50000
    client = _fake_s3vectors(
        [{"key": "k1", "distance": 0.1, "metadata": {"content": big}}]
    )
    out = retrieval.query_vectors(
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        query_embedding=[0.0] * 4, top_k=5, text_truncate=0,
    )
    assert len(out[0]["text"]) == 50000


# ---------------------------------------------------------------------------
# rerank
# ---------------------------------------------------------------------------


def _fake_bedrock(region="us-west-2"):
    client = MagicMock()
    client.meta = SimpleNamespace(region_name=region)
    return client


def test_rerank_reorders_and_adds_score():
    client = _fake_bedrock()
    client.rerank.return_value = {
        "results": [
            {"index": 1, "relevanceScore": 0.95},
            {"index": 0, "relevanceScore": 0.40},
        ]
    }
    docs = [{"text": "first"}, {"text": "second"}]
    out = retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id="cohere.rerank-v3-5:0",
        query="q",
        documents=docs,
        top_n=2,
        lambda_name="test",
    )
    assert [d["text"] for d in out] == ["second", "first"]
    assert out[0]["rerank_score"] == 0.95


def test_rerank_falls_back_to_cosine_order_on_client_error():
    client = _fake_bedrock()
    client.rerank.side_effect = ClientError(
        {"Error": {"Code": "ValidationException", "Message": "no reranker here"}},
        "Rerank",
    )
    docs = [{"text": "a"}, {"text": "b"}, {"text": "c"}]
    out = retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id="cohere.rerank-v3-5:0",
        query="q",
        documents=docs,
        top_n=2,
        lambda_name="test",
    )
    # Graceful degrade: original order, truncated to top_n. (us-east-2
    # has no Cohere Rerank — this path must not raise.)
    assert out == docs[:2]


def test_rerank_empty_docs_noop():
    client = _fake_bedrock()
    assert retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id="m",
        query="q",
        documents=[],
    ) == []
    client.rerank.assert_not_called()
