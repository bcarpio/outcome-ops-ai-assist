# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
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
import math
import os
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
    # A platform-authored summary OF documentation is still documentation:
    # it carries the docs' claims, not the code's, so it ranks in the docs
    # lane and never receives the code-map score boost (ADR-033 v1.1).
    "docs-summary",
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

# --------------------------------------------------------------------------- #
# Code-over-docs ranking (ADR-033 v1.1). Code is the source of truth for what
# the system does (ADR-025), so the final selection multiplies code-map scores
# by CODE_MAP_SCORE_BOOST before the two lanes compete for slots, and each lane
# keeps a floor of LANE_FLOOR_FRACTION of the slots (when it has candidates) so
# neither lane can be shut out entirely.
# --------------------------------------------------------------------------- #
DEFAULT_CODE_MAP_SCORE_BOOST = 1.5
DEFAULT_LANE_FLOOR_FRACTION = 0.25


def ranking_config_from_env() -> Tuple[float, float]:
    """``(code_map_score_boost, lane_floor_fraction)`` from the environment.

    ``CODE_MAP_SCORE_BOOST`` (>= 1.0) and ``RETRIEVAL_LANE_FLOOR`` (0.0-0.5)
    come from tfvars. Unset or invalid values fall back to the defaults, and
    out-of-range values are clamped, so a typo can never invert the rule
    (boost < 1 would favour docs) or starve a lane.
    """
    def _read(name: str, default: float) -> float:
        raw = os.environ.get(name, "")
        try:
            return float(raw) if raw else default
        except ValueError:
            logger.warning("[retrieval] invalid %s=%r; using %s", name, raw, default)
            return default

    boost = max(1.0, _read("CODE_MAP_SCORE_BOOST", DEFAULT_CODE_MAP_SCORE_BOOST))
    floor = min(0.5, max(0.0, _read("RETRIEVAL_LANE_FLOOR", DEFAULT_LANE_FLOOR_FRACTION)))
    return boost, floor


def dedupe_by_content_hash(hits: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Drop repeat copies of the same chunk, keeping the first (best) one.

    The same repo ingested into several workspaces (or under several SCM
    remotes) produces identical chunks under different keys; each copy would
    otherwise take its own result slot. Hits without a ``content_hash`` are
    never treated as duplicates of each other.
    """
    seen: set = set()
    unique: List[Dict[str, Any]] = []
    for hit in hits:
        digest = (hit.get("metadata") or {}).get("content_hash")
        if digest:
            if digest in seen:
                continue
            seen.add(digest)
        unique.append(hit)
    return unique


def select_with_code_map_boost(
    code_map_hits: List[Dict[str, Any]],
    doc_hits: List[Dict[str, Any]],
    top_k: int,
    *,
    boost: float = DEFAULT_CODE_MAP_SCORE_BOOST,
    floor_fraction: float = DEFAULT_LANE_FLOOR_FRACTION,
) -> List[Dict[str, Any]]:
    """Merge the two lanes into ``top_k`` results with code-maps boosted.

    Base score is the rerank ``relevanceScore`` when every hit has one, else
    the cosine ``score`` for all hits (mixing the two scales would make the
    comparison meaningless, e.g. when one lane's rerank call fell back).
    Code-map hits score ``base * boost``. Each lane first gets its floor
    (``ceil(top_k * floor_fraction)``, capped at its candidate count); the
    remaining slots go to the highest boosted scores from either lane. The
    result is sorted by boosted score (code-map first on a tie) and every hit
    is stamped with ``boosted_score``, so "earlier = stronger" in the
    synthesis context also means "code before docs".
    """
    if top_k <= 0:
        return []
    everything = list(code_map_hits) + list(doc_hits)
    use_rerank = bool(everything) and all("rerank_score" in h for h in everything)

    def _scored(hits: List[Dict[str, Any]], lane_boost: float) -> List[Dict[str, Any]]:
        out = []
        for hit in hits:
            base = hit.get("rerank_score", 0) if use_rerank else hit.get("score", 0)
            stamped = dict(hit)
            stamped["boosted_score"] = round(float(base or 0) * lane_boost, 6)
            out.append(stamped)
        out.sort(key=lambda h: h["boosted_score"], reverse=True)
        return out

    code = _scored(code_map_hits, boost)
    docs = _scored(doc_hits, 1.0)

    floor_n = math.ceil(top_k * floor_fraction) if floor_fraction > 0 else 0
    code_floor = min(floor_n, len(code))
    doc_floor = min(floor_n, len(docs), max(0, top_k - code_floor))
    chosen = code[:code_floor] + docs[:doc_floor]
    rest = code[code_floor:] + docs[doc_floor:]
    rest.sort(key=lambda h: (h["boosted_score"], h.get("lane") == "code_map"), reverse=True)
    chosen += rest[: max(0, top_k - len(chosen))]
    chosen.sort(key=lambda h: (h["boosted_score"], h.get("lane") == "code_map"), reverse=True)
    return chosen


def merge_dual_channel(
    documents: List[Dict[str, Any]],
    top_k: int,
    *,
    boost: Optional[float] = None,
    floor_fraction: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Boosted lane merge for an already-scored dual-channel pool.

    Used directly when rerank is disabled (cosine scores) and by
    ``rerank_dual_channel`` after per-lane rerank. ``boost`` /
    ``floor_fraction`` default to ``ranking_config_from_env()``.
    """
    env_boost, env_floor = ranking_config_from_env()
    return select_with_code_map_boost(
        [d for d in documents if d.get("lane") == "code_map"],
        [d for d in documents if d.get("lane") != "code_map"],
        top_k,
        boost=env_boost if boost is None else boost,
        floor_fraction=env_floor if floor_fraction is None else floor_fraction,
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
                        # a bare model id for pre-callers.
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
def _workspace_filter(
    workspace_ids: Optional[Sequence[str]],
) -> Optional[Dict[str, Any]]:
    """Workspace-scope clause: ``$eq`` for one, ``$in`` for many, None for
    no scope. The common base filter for chat and mcp-server."""
    if not workspace_ids:
        return None
    ws = list(workspace_ids)
    return (
        {"workspace_id": {"$eq": ws[0]}}
        if len(ws) == 1
        else {"workspace_id": {"$in": ws}}
    )


def _compose_lane_filter(
    base_filter: Optional[Dict[str, Any]],
    types: Sequence[str],
) -> Dict[str, Any]:
    """AND the caller's scope (``base_filter`` -- workspace for chat/mcp, repo +
    workspace for query-kb) with the lane's ``type`` taxonomy (``$in``), so the
    lane fills with up to ``top_k`` vectors OF THAT TYPE server-side rather than
    being post-filtered down from a mixed top-K. No base filter means a
    type-only lane filter."""
    type_clause = {"type": {"$in": list(types)}}
    if not base_filter:
        return type_clause
    return {"$and": [base_filter, type_clause]}


def _lane_filter(
    workspace_ids: Optional[Sequence[str]],
    types: Sequence[str],
) -> Dict[str, Any]:
    """Workspace-scoped lane filter (chat/mcp convenience). Equivalent to
    ``_compose_lane_filter(_workspace_filter(workspace_ids), types)``."""
    return _compose_lane_filter(_workspace_filter(workspace_ids), types)


def search_kb_dual_channel(
    query: str,
    fetch_k: int,
    *,
    s3vectors_client,
    vector_bucket: str,
    vector_index: str,
    embed_fn: Optional[Callable[[str], List[float]]] = None,
    query_embedding: Optional[List[float]] = None,
    workspace_ids: Optional[Sequence[str]] = None,
    base_filter: Optional[Dict[str, Any]] = None,
    code_map_types: Sequence[str] = CODE_MAP_TYPES,
    doc_types: Sequence[str] = DOC_TYPES,
    text_truncate: int = 20000,
    max_workers: int = 2,
) -> List[Dict[str, Any]]:
    """Two-lane retrieval (ADR-033): a code-map lane and a docs lane.

    Fires two ``query_vectors`` calls concurrently -- one filtered to
    ``code_map_types``, one to ``doc_types`` -- each taking half of ``fetch_k``.
    Every result is stamped with its ``lane`` (``"code_map"`` / ``"docs"``) and
    the combined pool is returned for a downstream per-lane rerank
    (``rerank_dual_channel``).

    Embedding: pass ``query_embedding`` when the caller already has it (query-kb,
    mcp-server), else pass ``embed_fn`` and the query is embedded once here (chat).

    Scope: pass ``base_filter`` for a caller-specific scope (query-kb's
    ``repo``/``workspace`` filter), or ``workspace_ids`` for the common
    workspace-only scope (chat, mcp-server). The lane's ``type`` filter is ANDed
    onto whichever is given.

    Both lanes always fire, intent-agnostic: code-maps carry the structural
    facts, docs carry the authored narrative, and rerank decides who keeps a
    slot. Reuses the single ``query_vectors`` S3 implementation (ADR-023
    consolidation) -- no private S3 query here.
    """
    embedding = (
        query_embedding if query_embedding is not None
        else (embed_fn(query) if embed_fn is not None else None)
    )
    if not embedding:
        logger.warning("[retrieval.dual_channel] empty embedding; returning []")
        return []

    effective_base = (
        base_filter if base_filter is not None
        else _workspace_filter(workspace_ids)
    )

    # Each lane gets half the candidate budget. Floor at 1 so even a very
    # small fetch_k still queries both lanes.
    code_map_k = max(1, fetch_k // 2)
    doc_k = max(1, fetch_k - code_map_k)

    def _run_lane(lane_label: str, top_k: int, types: Sequence[str]):
        # Degrade a failing lane to empty rather than failing the whole
        # retrieval: one lane's S3 error (or a region without the index)
        # must still let the other lane's results through. Mirrors the
        # per-lane error handling chat had before consolidation.
        try:
            hits = query_vectors(
                s3vectors_client=s3vectors_client,
                vector_bucket=vector_bucket,
                vector_index=vector_index,
                query_embedding=embedding,
                top_k=top_k,
                filter_dict=_compose_lane_filter(effective_base, types),
                text_truncate=text_truncate,
            )
        except ClientError as exc:
            logger.warning(
                "[retrieval.dual_channel] lane=%s query failed, degrading to "
                "empty: %s", lane_label, exc,
            )
            return []
        for hit in hits:
            hit["lane"] = lane_label
        return hits

    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
        code_map_future = pool.submit(_run_lane, "code_map", code_map_k, code_map_types)
        doc_future = pool.submit(_run_lane, "docs", doc_k, doc_types)
        code_map_hits = code_map_future.result()
        doc_hits = doc_future.result()

    raw_count = len(code_map_hits) + len(doc_hits)
    combined = dedupe_by_content_hash(list(code_map_hits) + list(doc_hits))
    logger.info(
        "[retrieval.dual_channel] code_map_lane=%d docs_lane=%d combined=%d "
        "duplicates_dropped=%d",
        len(code_map_hits), len(doc_hits), len(combined), raw_count - len(combined),
    )
    return combined


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
    boost: Optional[float] = None,
    floor_fraction: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Per-lane rerank for a dual-channel pool (ADR-033), then the boosted
    code-over-docs merge (ADR-033 v1.1, ``select_with_code_map_boost``).

    Splits ``documents`` by their ``lane`` stamp, reranks each lane against
    the query, then merges them with ``merge_dual_channel``: code-map scores
    are multiplied by the boost and each lane keeps a floor of slots, so code
    outranks docs without either lane being shut out (the magic_link
    starvation a combined-pool rerank caused). When either lane is empty there
    is nothing to balance, so it falls back to a single combined-pool rerank
    at ``top_k``.

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

    # Rerank each lane against the same query (so the scores are on one
    # scale), keeping up to top_k per lane so the boosted merge, not a fixed
    # split, decides how many slots each lane wins.
    reranked_code_maps = rerank(
        documents=code_map_hits, top_n=min(top_k, len(code_map_hits)), **rerank_kwargs,
    )
    reranked_docs = rerank(
        documents=doc_hits, top_n=min(top_k, len(doc_hits)), **rerank_kwargs,
    )
    for hit in reranked_code_maps:
        hit.setdefault("lane", "code_map")
    for hit in reranked_docs:
        hit.setdefault("lane", "docs")
    combined = merge_dual_channel(
        list(reranked_code_maps) + list(reranked_docs), top_k,
        boost=boost, floor_fraction=floor_fraction,
    )
    log_lane_mix("retrieval.dual_channel", combined, len(code_map_hits), len(doc_hits))
    return combined


def log_lane_mix(
    tag: str, selected: List[Dict[str, Any]], code_candidates: int, doc_candidates: int,
) -> None:
    """One log line showing how many slots each lane won after the boost."""
    code_won = sum(1 for d in selected if d.get("lane") == "code_map")
    logger.info(
        "[%s] boosted merge code_maps=%d/%d docs=%d/%d final=%d",
        tag, code_won, code_candidates, len(selected) - code_won, doc_candidates,
        len(selected),
    )
