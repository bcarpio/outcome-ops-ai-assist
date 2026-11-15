"""Shared knowledge-base retrieval: vector query + rerank.

One implementation behind BOTH retrieval surfaces:

- ``lambda/query-kb`` — the chat-side retrieval Lambda.
- ``lambda/mcp-server`` — the external-agent (Claude Code et al.) surface.

These two had drifted: ``query-kb`` oversampled and reranked; the MCP
server did neither and returned every chunk verbatim (the 60KB-for-one-
question failure). Consolidating here means the rerank pass, score
normalization, and repo-derivation cannot diverge again. The retrieval
contract is captured in the corresponding ADR.

Clients, bucket/index names, and rerank config are **injected** by the
caller rather than read from the environment here, because the two
callers wire them differently (``VECTOR_BUCKET`` vs ``S3_VECTORS_BUCKET``,
a ``repo`` filter vs a ``workspace_id`` filter). The module owns the
retrieval mechanics; each caller owns its config.
"""
from __future__ import annotations

import concurrent.futures
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Dual-channel lane taxonomy (ADR-033). Single source of truth: chat,
# query-kb, and mcp-server import these from here instead of each keeping a
# private copy. Two lanes always fire, intent-agnostic -- code-maps carry the
# structural facts, docs carry the authored narrative, and per-lane rerank
# keeps each lane's slots so prose cannot starve the code-map lane out of the
# synthesis context (the 2026-06-03 magic_link starvation this fixes).
# --------------------------------------------------------------------------- #
CODE_MAP_TYPES: Tuple[str, ...] = (
    "handler-group-summary",
    "source-module-summary",
    "csharp-project-summary",
    "tests-summary",
    "code-map",
    "csharp-services-summary",
    "docs-summary",
    "frontend-tests-summary",
    "frontend-components-summary",
    "frontend-utils-summary",
    "infrastructure-summary",
    "frontend-pages-summary",
    "csharp-models-summary",
    "build-summary",
    "csharp-views-summary",
    "csharp-repositories-summary",
    "kubernetes-summary",
    "schemas-summary",
    "shared-summary",
)

DOC_TYPES: Tuple[str, ...] = (
    "adr",
    "markdown",
    "html",
    "pdf",
    "yaml",
    "pptx",
    "docx",
    "text",   # .txt files -- NOTES, CHANGELOG, TODO, etc.
    "json",   # package.json / composer.json / TS configs / API schemas
    "xlsx",   # Excel spreadsheets (extracted-to-text via process-documents)
    "csv",    # tabular data (extracted-to-text via process-documents)
)

# Rough token estimate: ~4 chars/token for English prose + code. Good
# enough for response-size budgeting; we are bounding output, not billing.
_CHARS_PER_TOKEN = 4

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")
_WORD = re.compile(r"[a-zA-Z0-9_]+")


def estimate_tokens(text: str) -> int:
    """Approximate token count of ``text`` (4 chars/token heuristic)."""
    return len(text) // _CHARS_PER_TOKEN


def make_snippet(text: str, query: str, *, max_tokens: int = 500) -> str:
    """Return a query-relevant, sentence-bounded window of ``text``.

    The vector match is at the chunk granularity — we do not get a
    sub-chunk offset of *where* the embedding matched — so we approximate
    "the relevant part" by scoring sentences on query-term overlap and
    returning the highest-density consecutive window that fits in
    ``max_tokens``. Falls back to a sentence-bounded prefix when no term
    overlaps (e.g. a semantic-only match). Keeps whole sentences so the
    snippet reads cleanly rather than cutting mid-thought.
    """
    if not text:
        return ""
    budget_chars = max_tokens * _CHARS_PER_TOKEN
    if len(text) <= budget_chars:
        return text

    sentences = [s for s in _SENTENCE_SPLIT.split(text) if s.strip()]
    if not sentences:
        return text[:budget_chars].rstrip()

    query_terms = {w.lower() for w in _WORD.findall(query)}

    # Score each sentence by distinct query-term overlap.
    scores = []
    for s in sentences:
        if not query_terms:
            scores.append(0)
            continue
        words = {w.lower() for w in _WORD.findall(s)}
        scores.append(len(words & query_terms))

    # Find the best starting sentence (highest score), then grow the
    # window forward until the token budget is hit. No term overlap →
    # start at the top (prefix behavior).
    best_start = max(range(len(sentences)), key=lambda i: scores[i]) if any(scores) else 0

    window: List[str] = []
    used = 0
    for s in sentences[best_start:]:
        cost = len(s) + 1
        if used + cost > budget_chars and window:
            break
        window.append(s)
        used += cost

    snippet = " ".join(window).strip()
    # Hard char-level ceiling. Boundary-less content — JSON, minified
    # assets, a single very long line — can be one giant "sentence" the
    # loop above appends whole (the first sentence is always taken). Cap
    # it so one chunk can't blow the per-result budget and starve the
    # rest of the response. This is the guard that keeps a package-lock
    # chunk from eating the whole token budget.
    if len(snippet) > budget_chars:
        snippet = snippet[:budget_chars].rstrip()
        suffix = " …"
    else:
        suffix = " …" if best_start + len(window) < len(sentences) else ""
    prefix = "… " if best_start > 0 else ""
    return f"{prefix}{snippet}{suffix}"


def derive_repo(metadata: Dict[str, Any]) -> str:
    """Best-effort repo label from a vector's metadata.

    Prefers the explicit ``repo`` field; otherwise reconstructs a label
    from ``source`` + ``folder_path`` the same way both retrieval
    surfaces did independently before consolidation.
    """
    repo = metadata.get("repo", "")
    if repo:
        return repo
    source_type = metadata.get("source", "")
    folder_path = metadata.get("folder_path", "")
    if source_type == "github":
        return folder_path.split("/")[0] if folder_path else ""
    if source_type and folder_path:
        space = folder_path.split("/")[-1] if "/" in folder_path else folder_path
        return f"{source_type.capitalize()}/{space}"
    if source_type:
        return source_type.capitalize()
    return ""


def query_vectors(
    *,
    s3vectors_client,
    vector_bucket: str,
    vector_index: str,
    query_embedding: List[float],
    top_k: int,
    filter_dict: Optional[Dict[str, Any]] = None,
    doc_types: Optional[List[str]] = None,
    text_truncate: int = 20000,
) -> List[Dict[str, Any]]:
    """Query S3 Vectors and return a normalized result list.

    Each result is a dict with a stable shape both callers project from:

    - ``score``     — cosine similarity (``1 - distance``), rounded to 4
                      places, higher = more relevant.
    - ``distance``  — the raw S3 Vectors distance (lower = more similar).
    - ``text``      — chunk content, truncated to ``text_truncate`` chars
                      (0 disables truncation).
    - ``type`` / ``repo`` / ``file_path`` / ``workspace_id`` / ``key``
    - ``metadata``  — the raw metadata dict, for caller-specific
                      formatting (e.g. query-kb's ``format_source``).

    ``filter_dict`` is passed straight through as the S3 Vectors
    ``filter`` (callers build their own — ``{"repo": {"equals": ...}}``
    for chat, ``{"workspace_id": {"$eq": ...}}`` for MCP). ``doc_types``
    is filtered client-side. Results are sorted by similarity and capped
    at ``top_k``.
    """
    params: Dict[str, Any] = {
        "vectorBucketName": vector_bucket,
        "indexName": vector_index,
        "queryVector": {"float32": query_embedding},
        "topK": top_k,
        "returnMetadata": True,
        "returnDistance": True,
    }
    if filter_dict:
        params["filter"] = filter_dict

    response = s3vectors_client.query_vectors(**params)

    results: List[Dict[str, Any]] = []
    for vector in response.get("vectors", []):
        metadata = vector.get("metadata", {})
        if doc_types and metadata.get("type") not in doc_types:
            continue
        distance = vector.get("distance", 0)
        similarity = round(1 - distance, 4)
        content = metadata.get("content", "")
        if text_truncate:
            content = content[:text_truncate]
        results.append(
            {
                "key": vector.get("key", ""),
                "score": similarity,
                "distance": distance,
                "text": content,
                "type": metadata.get("type", ""),
                "repo": derive_repo(metadata),
                "file_path": metadata.get("file_path", ""),
                "workspace_id": metadata.get("workspace_id", ""),
                "metadata": metadata,
            }
        )

    results.sort(key=lambda x: x["score"], reverse=True)
    return results[:top_k]


def rerank(
    *,
    bedrock_agent_runtime_client,
    rerank_model_id: str,
    query: str,
    documents: List[Dict[str, Any]],
    top_n: Optional[int] = None,
    lambda_name: str = "retrieval",
    audit_table_name: str = "",
    user_email: str = "",
    workspace_id: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> List[Dict[str, Any]]:
    """Rerank ``documents`` with the Bedrock Rerank API (Cohere 3.5 by
    default) for cross-attention precision the cosine score alone lacks.

    Adds a ``rerank_score`` to each returned doc and reorders by it.
    Returns at most ``top_n`` results (``None`` = all, reordered). Falls
    back to the original cosine order (``documents[:top_n]``) on any
    Bedrock error so a rerank hiccup degrades gracefully rather than
    failing the query — important because Cohere Rerank is not available
    in every region (e.g. us-east-2), so the passive region must degrade
    to cosine, not error.
    """
    if not documents:
        return documents

    try:
        sources = [
            {
                "type": "INLINE",
                "inlineDocumentSource": {
                    "type": "TEXT",
                    "textDocument": {"text": doc.get("text", "")[:4096]},
                },
            }
            for doc in documents
        ]

        rerank_start = time.time()
        region = bedrock_agent_runtime_client.meta.region_name
        rerank_params: Dict[str, Any] = {
            "queries": [{"type": "TEXT", "textQuery": {"text": query}}],
            "sources": sources,
            "rerankingConfiguration": {
                "type": "BEDROCK_RERANKING_MODEL",
                "bedrockRerankingConfiguration": {
                    "modelConfiguration": {
                        # ADR-016 v1.2: accept a full ARN so callers can
                        # pass a workload-scoped application inference
                        # profile ARN (from BEDROCK_<WL>_COHERE_RERANK_
                        # PROFILE_ARN) for cost attribution. Falls back
                        # to constructing the foundation-model ARN from
                        # a bare model id for pre-chunk-04 callers.
                        "modelArn": (
                            rerank_model_id
                            if rerank_model_id.startswith("arn:aws:bedrock:")
                            else f"arn:aws:bedrock:{region}::foundation-model/{rerank_model_id}"
                        )
                    }
                },
            },
        }
        if top_n is not None:
            rerank_params["rerankingConfiguration"][
                "bedrockRerankingConfiguration"
            ]["numberOfResults"] = top_n

        response = bedrock_agent_runtime_client.rerank(**rerank_params)
        rerank_duration_ms = int((time.time() - rerank_start) * 1000)
        logger.info(
            f"[{lambda_name}] Reranked {len(documents)} documents in "
            f"{rerank_duration_ms}ms"
        )

        reranked: List[Dict[str, Any]] = []
        for result in response.get("results", []):
            idx = result.get("index", 0)
            if idx < len(documents):
                doc = documents[idx].copy()
                doc["rerank_score"] = result.get("relevanceScore", 0)
                reranked.append(doc)

        if audit_table_name:
            try:
                from shared.audit import log_audit_entry  # lazy

                log_audit_entry(
                    audit_table_name=audit_table_name,
                    lambda_name=lambda_name,
                    model_id=rerank_model_id,
                    input_text=f"rerank:{query}",
                    output_text=(
                        f"reranked {len(reranked)} of {len(documents)} docs"
                    ),
                    duration_ms=rerank_duration_ms,
                    # Attribution so the rerank audit row lands on the
                    # causing workspace/user instead of WS#SYSTEM (where the
                    # analytics aggregator skips it). Empty by default, so
                    # pre-existing callers keep their current behavior.
                    user_email=user_email,
                    workspace_id=workspace_id,
                    source_ip=source_ip,
                    user_agent=user_agent,
                )
            except Exception as exc:  # pragma: no cover - audit is best-effort
                logger.warning(f"[{lambda_name}] rerank audit log failed: {exc}")

        return reranked

    except ClientError as e:
        logger.warning(
            f"[{lambda_name}] Reranking failed, falling back to cosine order: {e}"
        )
        return documents[:top_n] if top_n else documents
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[{lambda_name}] Unexpected rerank error, falling back: {e}")
        return documents[:top_n] if top_n else documents


# --------------------------------------------------------------------------- #
# Dual-channel retrieval (ADR-033), as a shared primitive.
#
# Until now this lived only in chat/handler.py, above the ADR-023 shared seam,
# so query-kb and mcp-server ran single-channel -- the exact pre-ADR-033 config
# that produced the magic_link file:line mis-attribution. These functions move
# the two-lane orchestration below the seam so every retrieval surface can call
# it. Clients, bucket/index, and the embedding function are injected (the module
# stays free of per-Lambda env coupling); the lane split preserves chat's
# server-side type filtering so each lane fills from its own taxonomy rather
# than losing its budget to the other lane in a single top-K.
# --------------------------------------------------------------------------- #
def _lane_filter(
    workspace_ids: Optional[Sequence[str]],
    types: Sequence[str],
) -> Dict[str, Any]:
    """Build the S3 Vectors metadata filter for one lane.

    Composes the workspace scope (``$eq`` for one, ``$in`` for many) AND the
    lane's ``type`` taxonomy (``$in``) into an ``$and`` -- the same server-side
    filter chat builds, so the lane fills with up to ``top_k`` vectors OF THAT
    TYPE rather than being post-filtered down from a mixed top-K.
    """
    clauses: List[Dict[str, Any]] = []
    if workspace_ids:
        ws = list(workspace_ids)
        clauses.append(
            {"workspace_id": {"$eq": ws[0]}}
            if len(ws) == 1
            else {"workspace_id": {"$in": ws}}
        )
    clauses.append({"type": {"$in": list(types)}})
    if len(clauses) == 1:
        return clauses[0]
    return {"$and": clauses}


def search_kb_dual_channel(
    query: str,
    fetch_k: int,
    *,
    s3vectors_client,
    vector_bucket: str,
    vector_index: str,
    embed_fn: Callable[[str], List[float]],
    workspace_ids: Optional[Sequence[str]] = None,
    code_map_types: Sequence[str] = CODE_MAP_TYPES,
    doc_types: Sequence[str] = DOC_TYPES,
    text_truncate: int = 20000,
    max_workers: int = 2,
) -> List[Dict[str, Any]]:
    """Two-lane retrieval (ADR-033): a code-map lane and a docs lane.

    Embeds ``query`` once (via the injected ``embed_fn``) and fires two
    ``query_vectors`` calls concurrently -- one filtered to ``code_map_types``,
    one to ``doc_types`` -- each taking half of ``fetch_k``. Every result is
    stamped with its ``lane`` (``"code_map"`` / ``"docs"``) and the combined
    pool is returned for a downstream per-lane rerank (``rerank_dual_channel``).

    Both lanes always fire, intent-agnostic: code-maps carry the structural
    facts, docs carry the authored narrative, and rerank decides who keeps a
    slot. Reuses the single ``query_vectors`` S3 implementation (ADR-023
    consolidation) -- no private S3 query here.
    """
    embedding = embed_fn(query)
    if not embedding:
        logger.warning("[retrieval.dual_channel] empty embedding; returning []")
        return []

    # Each lane gets half the candidate budget. Floor at 1 so even a very
    # small fetch_k still queries both lanes.
    code_map_k = max(1, fetch_k // 2)
    doc_k = max(1, fetch_k - code_map_k)

    def _run_lane(lane_label: str, top_k: int, types: Sequence[str]):
        hits = query_vectors(
            s3vectors_client=s3vectors_client,
            vector_bucket=vector_bucket,
            vector_index=vector_index,
            query_embedding=embedding,
            top_k=top_k,
            filter_dict=_lane_filter(workspace_ids, types),
            text_truncate=text_truncate,
        )
        for hit in hits:
            hit["lane"] = lane_label
        return hits

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        code_map_future = pool.submit(_run_lane, "code_map", code_map_k, code_map_types)
        doc_future = pool.submit(_run_lane, "docs", doc_k, doc_types)
        code_map_hits = code_map_future.result()
        doc_hits = doc_future.result()

    combined = list(code_map_hits) + list(doc_hits)
    logger.info(
        "[retrieval.dual_channel] code_map_lane=%d docs_lane=%d combined=%d",
        len(code_map_hits), len(doc_hits), len(combined),
    )
    return combined


def split_rerank_budget(
    top_k: int, code_map_count: int, doc_count: int,
) -> Tuple[int, int]:
    """Adaptive per-lane rerank top_n split.

    Start from an even 50/50 (odd budget goes to docs -- the larger pool in any
    non-thin-ingest workspace), then cap each lane at its actual candidate count
    and redistribute unused slots to the other lane so the final context is not
    left with empty slots when one lane is thin. Returns
    ``(code_map_top_n, doc_top_n)``; both non-negative; their sum is
    ``min(top_k, code_map_count + doc_count)``.
    """
    target_code = max(1, top_k // 2)
    target_docs = max(1, top_k - target_code)
    code_map_top_n = min(target_code, code_map_count)
    doc_top_n = min(target_docs, doc_count)
    leftover = top_k - code_map_top_n - doc_top_n
    if leftover > 0:
        extra_docs = min(leftover, doc_count - doc_top_n)
        doc_top_n += extra_docs
        leftover -= extra_docs
        if leftover > 0:
            extra_code = min(leftover, code_map_count - code_map_top_n)
            code_map_top_n += extra_code
    return code_map_top_n, doc_top_n


def rerank_dual_channel(
    *,
    bedrock_agent_runtime_client,
    rerank_model_id: str,
    query: str,
    documents: List[Dict[str, Any]],
    top_k: int,
    lambda_name: str = "retrieval",
    audit_table_name: str = "",
    user_email: str = "",
    workspace_id: str = "",
    source_ip: str = "",
    user_agent: str = "",
) -> List[Dict[str, Any]]:
    """Per-lane rerank for a dual-channel pool (ADR-033).

    Splits ``documents`` by their ``lane`` stamp and reranks each lane against
    its own adaptive budget (``split_rerank_budget``), then concatenates. This
    protects the code-map lane's slots from doc-lane prose that out-scores it on
    a combined-pool rerank (the magic_link starvation). When either lane is
    empty there is no fairness issue, so it falls back to a single combined-pool
    rerank at ``top_k``.

    ``rerank_model_id`` may be a bare foundation-model id or a full ARN -- the
    caller resolves its own ADR-016 workload profile ARN and passes it in, so
    this stays free of ``model_client`` coupling (Bedrock Rerank takes bare
    foundation-model ARNs only; a workload profile ARN is passed through as-is).
    """
    if not documents:
        return documents

    code_map_hits = [d for d in documents if d.get("lane") == "code_map"]
    doc_hits = [d for d in documents if d.get("lane") == "docs"]

    rerank_kwargs = dict(
        bedrock_agent_runtime_client=bedrock_agent_runtime_client,
        rerank_model_id=rerank_model_id,
        query=query,
        lambda_name=lambda_name,
        audit_table_name=audit_table_name,
        user_email=user_email,
        workspace_id=workspace_id,
        source_ip=source_ip,
        user_agent=user_agent,
    )

    # Empty-lane case: one side returned nothing (thin-ingest workspace, or the
    # lane filter matched nothing). No rerank-fairness issue to protect, so
    # rerank the combined pool at the full budget.
    if not code_map_hits or not doc_hits:
        return rerank(documents=documents, top_n=top_k, **rerank_kwargs)

    code_map_top_n, doc_top_n = split_rerank_budget(
        top_k, len(code_map_hits), len(doc_hits),
    )
    reranked_code_maps = rerank(
        documents=code_map_hits, top_n=code_map_top_n, **rerank_kwargs,
    )
    reranked_docs = rerank(
        documents=doc_hits, top_n=doc_top_n, **rerank_kwargs,
    )
    combined = list(reranked_code_maps) + list(reranked_docs)
    logger.info(
        "[retrieval.dual_channel] per-lane rerank code_maps=%d/%d docs=%d/%d final=%d",
        len(reranked_code_maps), len(code_map_hits),
        len(reranked_docs), len(doc_hits), len(combined),
    )
    return combined
