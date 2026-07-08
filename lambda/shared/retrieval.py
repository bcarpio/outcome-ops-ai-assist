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

import logging
import re
import time
from typing import Any, Dict, List, Optional

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

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
