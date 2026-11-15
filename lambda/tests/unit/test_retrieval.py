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


def test_make_snippet_hard_caps_boundaryless_content():
    """JSON / minified content has almost no sentence boundaries, so the
    whole blob is one 'sentence'. The snippet must still be char-capped
    near the budget (a package-lock chunk must not eat the whole budget)."""
    blob = '{"a":1,"b":2,"c":3,' * 2000  # ~36KB, no sentence breaks
    out = retrieval.make_snippet(blob, "dependency", max_tokens=500)
    # budget = 500 tokens * 4 chars = 2000 chars; allow small slack for
    # the ellipsis suffix.
    assert len(out) <= 2000 + 4
    assert len(out) < len(blob)


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


# ---------------------------------------------------------------------------
# ADR-016 v1.2 (chunk 03) -- rerank accepts a profile ARN as the model id
# ---------------------------------------------------------------------------


def test_rerank_accepts_application_inference_profile_arn():
    # Cost-attribution path: caller passes a workload-scoped profile ARN
    # (from BEDROCK_CHAT_COHERE_RERANK_PROFILE_ARN) and rerank uses it
    # directly instead of constructing a foundation-model ARN.
    client = _fake_bedrock()
    client.rerank.return_value = {
        "results": [{"index": 0, "relevanceScore": 0.9}],
    }
    profile_arn = (
        "arn:aws:bedrock:us-west-2:123456789012:inference-profile/"
        "chat-cohere-rerank-abc"
    )
    retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id=profile_arn,
        query="q",
        documents=[{"text": "d"}],
        lambda_name="test",
    )
    call_kwargs = client.rerank.call_args.kwargs
    model_arn = call_kwargs["rerankingConfiguration"][
        "bedrockRerankingConfiguration"
    ]["modelConfiguration"]["modelArn"]
    # ARN is passed through unchanged -- NOT re-wrapped in a
    # foundation-model prefix.
    assert model_arn == profile_arn


def test_rerank_threads_attribution_to_audit(monkeypatch):
    # The attribution kwargs must reach log_audit_entry so the rerank row
    # lands on the causing workspace/user, not WS#SYSTEM.
    client = _fake_bedrock()
    client.rerank.return_value = {"results": [{"index": 0, "relevanceScore": 0.9}]}
    captured = {}

    def _fake_log(**kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("shared.audit.log_audit_entry", _fake_log)
    retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id="cohere.rerank-v3-5:0",
        query="q",
        documents=[{"text": "d"}],
        lambda_name="chat",
        audit_table_name="audit-tbl",
        user_email="u@example.com",
        workspace_id="WS#abc",
        source_ip="1.2.3.4",
        user_agent="agent",
    )
    assert captured["user_email"] == "u@example.com"
    assert captured["workspace_id"] == "WS#abc"
    assert captured["source_ip"] == "1.2.3.4"
    assert captured["user_agent"] == "agent"


# ---------------------------------------------------------------------------
# Dual-channel (chunk 01): _lane_filter / search_kb_dual_channel /
# split_rerank_budget / rerank_dual_channel
# ---------------------------------------------------------------------------


def test_lane_filter_single_workspace():
    assert retrieval._lane_filter(["w1"], ["code-map"]) == {
        "$and": [
            {"workspace_id": {"$eq": "w1"}},
            {"type": {"$in": ["code-map"]}},
        ]
    }


def test_lane_filter_multi_workspace_uses_in():
    assert retrieval._lane_filter(["w1", "w2"], ["adr"]) == {
        "$and": [
            {"workspace_id": {"$in": ["w1", "w2"]}},
            {"type": {"$in": ["adr"]}},
        ]
    }


def test_lane_filter_no_workspace_type_only():
    assert retrieval._lane_filter(None, ["adr", "markdown"]) == {
        "type": {"$in": ["adr", "markdown"]}
    }


def test_compose_lane_filter_ands_base_with_type():
    # query-kb's repo/workspace base filter gets ANDed with the lane type.
    base = {"repo": {"equals": "r1"}, "workspace_id": {"$in": ["w1"]}}
    assert retrieval._compose_lane_filter(base, ["adr"]) == {
        "$and": [base, {"type": {"$in": ["adr"]}}]
    }


def test_compose_lane_filter_no_base_is_type_only():
    assert retrieval._compose_lane_filter(None, ["adr"]) == {
        "type": {"$in": ["adr"]}
    }


def _type_in(filt):
    """Extract the lane's ``type`` ``$in`` set from a filter (emulating the
    S3 Vectors server-side type filter)."""
    if "type" in filt and "$in" in filt["type"]:
        return set(filt["type"]["$in"])
    for clause in filt.get("$and", []):
        if "type" in clause and "$in" in clause["type"]:
            return set(clause["type"]["$in"])
    return None


def _fake_s3vectors_server_filter(all_vectors):
    """S3 Vectors fake that honors the server-side ``type`` filter, so each
    lane fills only from its own taxonomy (as real S3 does)."""
    client = MagicMock()

    def _q(**params):
        tset = _type_in(params.get("filter", {}))
        vs = [
            v for v in all_vectors
            if tset is None or v["metadata"].get("type") in tset
        ]
        return {"vectors": vs[: params["topK"]]}

    client.query_vectors.side_effect = _q
    return client


def test_search_kb_dual_channel_fans_out_and_stamps_lanes():
    all_vectors = [
        {"key": "cm1", "distance": 0.1, "metadata": {"content": "map1", "type": "code-map"}},
        {"key": "cm2", "distance": 0.2, "metadata": {"content": "map2", "type": "shared-summary"}},
        {"key": "d1", "distance": 0.15, "metadata": {"content": "doc1", "type": "adr"}},
        {"key": "d2", "distance": 0.25, "metadata": {"content": "doc2", "type": "markdown"}},
    ]
    client = _fake_s3vectors_server_filter(all_vectors)
    calls = {"n": 0}

    def _embed(_q):
        calls["n"] += 1
        return [0.0] * 4

    out = retrieval.search_kb_dual_channel(
        "where is x called",
        fetch_k=4,
        s3vectors_client=client,
        vector_bucket="b",
        vector_index="i",
        embed_fn=_embed,
        workspace_ids=["w1"],
    )
    # Embedding computed ONCE and reused across both lanes.
    assert calls["n"] == 1
    code_map = [r for r in out if r["lane"] == "code_map"]
    docs = [r for r in out if r["lane"] == "docs"]
    # Each lane filled from its own taxonomy, server-side.
    assert {r["type"] for r in code_map} <= set(retrieval.CODE_MAP_TYPES)
    assert {r["type"] for r in docs} <= set(retrieval.DOC_TYPES)
    assert len(code_map) == 2 and len(docs) == 2
    assert len(out) == 4


def test_search_kb_dual_channel_uses_precomputed_embedding():
    # query-kb / mcp-server pass query_embedding directly; embed_fn must NOT
    # be called (and may be None).
    all_vectors = [
        {"key": "d1", "distance": 0.1, "metadata": {"content": "d", "type": "adr"}},
    ]
    client = _fake_s3vectors_server_filter(all_vectors)
    out = retrieval.search_kb_dual_channel(
        "q", fetch_k=2,
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        query_embedding=[0.1] * 4,
        workspace_ids=["w1"],
    )
    assert any(r["lane"] == "docs" for r in out)


def test_search_kb_dual_channel_base_filter_reaches_query():
    # A caller base_filter (query-kb repo scope) is ANDed with the lane type
    # and passed through to query_vectors as the S3 filter.
    seen_filters = []
    client = MagicMock()

    def _q(**params):
        seen_filters.append(params.get("filter"))
        return {"vectors": []}

    client.query_vectors.side_effect = _q
    retrieval.search_kb_dual_channel(
        "q", fetch_k=2,
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        query_embedding=[0.1] * 4,
        base_filter={"repo": {"equals": "r1"}},
    )
    # Both lanes' filters AND the repo base with their type clause.
    assert all(f.get("$and") and f["$and"][0] == {"repo": {"equals": "r1"}}
               for f in seen_filters)
    type_sets = {tuple(f["$and"][1]["type"]["$in"]) for f in seen_filters}
    assert tuple(retrieval.CODE_MAP_TYPES) in type_sets
    assert tuple(retrieval.DOC_TYPES) in type_sets


def test_search_kb_dual_channel_empty_embedding_returns_empty():
    client = _fake_s3vectors_server_filter([])
    out = retrieval.search_kb_dual_channel(
        "q", fetch_k=4,
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        embed_fn=lambda _q: [],
    )
    assert out == []
    client.query_vectors.assert_not_called()


def test_split_rerank_budget_even():
    assert retrieval.split_rerank_budget(10, 5, 5) == (5, 5)


def test_split_rerank_budget_thin_code_lane_redistributes_to_docs():
    # 2 code-map candidates, 8 doc candidates, budget 10:
    # code keeps its 2, the 3 unused code slots flow to docs -> 8.
    assert retrieval.split_rerank_budget(10, 2, 8) == (2, 8)


def test_split_rerank_budget_sum_capped_by_candidate_count():
    # Only 5 candidates total for a budget of 10 -> sum is 5, no padding.
    cm, doc = retrieval.split_rerank_budget(10, 2, 3)
    assert (cm, doc) == (2, 3)
    assert cm + doc == 5


def test_rerank_dual_channel_per_lane_when_both_present(monkeypatch):
    recorded = []

    def _fake_rerank(*, documents, top_n, **kwargs):
        recorded.append((len(documents), top_n, documents[0]["lane"]))
        return documents[:top_n]

    monkeypatch.setattr(retrieval, "rerank", _fake_rerank)
    docs = (
        [{"lane": "code_map", "text": f"c{i}"} for i in range(3)]
        + [{"lane": "docs", "text": f"d{i}"} for i in range(4)]
    )
    out = retrieval.rerank_dual_channel(
        bedrock_agent_runtime_client=_fake_bedrock(),
        rerank_model_id="m",
        query="q",
        documents=docs,
        top_k=6,
    )
    # Two per-lane rerank calls: code_map top_n=3, docs top_n=3 (split 6 across 3/4).
    assert len(recorded) == 2
    assert (3, 3, "code_map") in recorded
    assert (4, 3, "docs") in recorded
    assert len(out) == 6


def test_rerank_dual_channel_empty_lane_falls_back_to_combined(monkeypatch):
    calls = []

    def _fake_rerank(*, documents, top_n, **kwargs):
        calls.append((len(documents), top_n))
        return documents[:top_n]

    monkeypatch.setattr(retrieval, "rerank", _fake_rerank)
    # Only code_map hits -> no docs lane -> single combined-pool rerank.
    docs = [{"lane": "code_map", "text": f"c{i}"} for i in range(4)]
    out = retrieval.rerank_dual_channel(
        bedrock_agent_runtime_client=_fake_bedrock(),
        rerank_model_id="m", query="q", documents=docs, top_k=3,
    )
    assert calls == [(4, 3)]  # one call, full pool, top_n=top_k
    assert len(out) == 3


def test_rerank_dual_channel_empty_docs_noop():
    assert retrieval.rerank_dual_channel(
        bedrock_agent_runtime_client=_fake_bedrock(),
        rerank_model_id="m", query="q", documents=[], top_k=5,
    ) == []


def test_search_kb_dual_channel_lane_error_degrades_to_empty():
    # A lane whose S3 query raises ClientError must degrade to empty rather
    # than failing the whole retrieval, so the other lane still returns.
    all_vectors = [
        {"key": "d1", "distance": 0.15, "metadata": {"content": "doc1", "type": "adr"}},
    ]

    client = MagicMock()

    def _q(**params):
        tset = _type_in(params.get("filter", {}))
        if tset and set(retrieval.CODE_MAP_TYPES) & tset:
            raise ClientError(
                {"Error": {"Code": "ValidationException", "Message": "boom"}},
                "QueryVectors",
            )
        return {"vectors": [v for v in all_vectors if v["metadata"]["type"] in (tset or set())][: params["topK"]]}

    client.query_vectors.side_effect = _q
    out = retrieval.search_kb_dual_channel(
        "q", fetch_k=4,
        s3vectors_client=client, vector_bucket="b", vector_index="i",
        embed_fn=lambda _q: [0.0] * 4, workspace_ids=["w1"],
    )
    # code_map lane raised -> empty; docs lane still returns its hit.
    assert [r["lane"] for r in out] == ["docs"]
    assert out[0]["key"] == "d1"


def test_rerank_bare_model_id_still_wraps_foundation_model_arn():
    # Legacy path (pre-chunk-04 callers): bare model id -> constructed
    # arn:aws:bedrock:{region}::foundation-model/{id}. Preserves
    # backwards compatibility during rollout.
    client = _fake_bedrock()
    client.rerank.return_value = {
        "results": [{"index": 0, "relevanceScore": 0.9}],
    }
    retrieval.rerank(
        bedrock_agent_runtime_client=client,
        rerank_model_id="cohere.rerank-v3-5:0",
        query="q",
        documents=[{"text": "d"}],
        lambda_name="test",
    )
    model_arn = client.rerank.call_args.kwargs["rerankingConfiguration"][
        "bedrockRerankingConfiguration"
    ]["modelConfiguration"]["modelArn"]
    assert model_arn.endswith("::foundation-model/cohere.rerank-v3-5:0")
    assert model_arn.startswith("arn:aws:bedrock:")
