# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""Hybrid retrieval: symbol-graph augmentation on top of RAG (ADR-033).

Extracted from the chat handler into the shared retrieval seam (ADR-023) so
every retrieval surface -- chat, query-kb, mcp-server -- can fuse structural
symbol-graph facts with vector RAG, instead of only chat doing it.

The public entry is :func:`augment_with_symbol_graph`. Table names default to
the same ``os.environ`` reads the chat handler used, so a caller that passes
nothing behaves exactly as chat did; query-kb / mcp-server can inject their own
table names.
"""

from __future__ import annotations

import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional, Tuple

from shared.router import classify_intent
from shared.context_builder import (
    build_graph_unavailable_block,
    build_multi_canonical_facts_block,
    build_symbol_definition_block,
    build_symbol_facts_block,
)

logger = logging.getLogger(__name__)

__all__ = ("augment_with_symbol_graph", "code_map_definitions")


def _graph_retrieval_enabled() -> bool:
    """Env-gated kill-switch, read at CALL time.

    Operators can disable graph augmentation in the field without a
    redeploy (set to "false"); the caller then falls back to RAG-only.
    Read at call time (not import time) so an SSM flip takes effect
    without a redeploy, and so it reflects the current env regardless of
    import caching -- the shared module is imported once, unlike the chat
    handler which was re-imported per request under test.

    Naming: ``GRAPH_RETRIEVAL_ENABLED`` is the canonical kill-switch
    shared across chat / analyze-pr / generate-code; the previous name
    ``GRAPH_AUGMENT_ENABLED`` is still honored for older deploys.
    """
    return (
        os.environ.get(
            "GRAPH_RETRIEVAL_ENABLED",
            os.environ.get("GRAPH_AUGMENT_ENABLED", "true"),
        ).lower()
        == "true"
    )


def _site_to_dict(site) -> Dict[str, Any]:
    """Flatten a ``CallSite`` (or definition site) onto the dict shape
    the metadata frame expects.

    Used in both the callers/importers/implementers success path AND
    the multi-canonical disambiguation paths so the UI receives an
    identical structure regardless of which branch the augment took.
    """
    return {
        "file_path": getattr(site, "file_path", "") or "",
        "line": getattr(site, "line", 0) or 0,
        "repo": getattr(site, "repo", "") or "",
        "source_symbol_id": getattr(site, "source_symbol_id", "") or "",
        "language": getattr(site, "language", "") or "",
    }


_FUZZY_RESOLVE_LIMIT = 200
"""Enumeration-shaped consumer limit for fuzzy_resolve_by_name.

The reader's default is 25, sized for general MCP disambiguation
(LLM picking from a handful of canonicals). The chat router's
semantics are enumeration: every canonical for a bare name must be
walked so the downstream relation fan-out is complete. 200 covers
any realistic workspace -- the 2026-06-02 require_auth eval had 40
canonicals; the prior 25-cap silently lost 8 lambdas. ADR-032's
completeness signal catches the regression if a workspace ever
exceeds 200 (the chat synthesis prompt rule, ADR-032/,
binds the LLM to surface the truncation rather than ship a
silently-incomplete enumeration)."""


def _fuzzy_resolve_symbol(
    workspace_id: str,
    bare_name: str,
    symbol_graph_table_name: str,
):
    """Resolve a user-typed bare symbol name to its canonical ID(s).

    The user types ``"require_auth"``; the graph stores the canonical
    ``"python:lambda.auth.handler::require_auth"``. Backed by the
    ``target-name-index`` GSI on the symbol-graph table, partitioned
    by ``(workspace_id, bare_name)`` -- a single Query, bounded by
    actual matches.

    Returns the full ``ResolutionResult`` (canonicals + truncated +
    total_known) so the augment caller can surface a canonical-set
    truncation to the prompt context per ADR-032. An empty result is
    a first-class answer (complete with zero canonicals).
    """
    from shared.symbol_graph_reader import ResolutionResult  # type: ignore

    if not bare_name or not workspace_id or not symbol_graph_table_name:
        return ResolutionResult(
            canonicals=[], truncated=False, total_known=0
        )

    # If the user typed a canonical-looking ID (contains ``::``),
    # the exact-match path already handled it; don't re-resolve.
    if "::" in bare_name:
        return ResolutionResult(
            canonicals=[], truncated=False, total_known=0
        )

    try:
        from shared.symbol_graph_reader import fuzzy_resolve_by_name  # type: ignore
    except Exception as exc:  # pragma: no cover - import failure
        logger.warning("[chat.augment] reader import failed: %s", exc)
        return ResolutionResult(
            canonicals=[], truncated=False, total_known=None
        )

    try:
        resolution = fuzzy_resolve_by_name(
            workspace_id,
            bare_name,
            limit=_FUZZY_RESOLVE_LIMIT,
            symbol_graph_table_name=symbol_graph_table_name,
        )
    except Exception as exc:
        logger.warning(
            "[chat.augment] fuzzy resolve failed for %r: %s", bare_name, exc
        )
        return ResolutionResult(
            canonicals=[], truncated=False, total_known=None
        )

    # Observability: the live diagnosis on 2026-05-15 showed we had
    # no way to tell from CloudWatch whether fuzzy returned 0, 1, or
    # N canonicals. The 2026-06-02 require_auth eval surfaced the
    # silent-truncation half: cap=25 clipped 40 canonicals to the
    # alphabetical first 25; the failure was invisible until ground-
    # truth verification. Log both count and truncated so a future
    # regression surfaces in CloudWatch the moment it ships.
    logger.info(
        "[chat.augment] fuzzy resolve %r -> %d canonicals (truncated=%s)",
        bare_name,
        len(resolution.canonicals),
        resolution.truncated,
    )
    return resolution


# ---------------------------------------------------------------------------
# Deterministic backstop (ADR-059): when the router returns no symbol -- the
# LLM misclassified a structural question as pattern/dependency, OR classify
# degraded to pattern+None under throttle -- a question that NAMES a resolvable
# symbol would otherwise silently stay RAG-only. We extract identifier-shaped
# tokens from the question text (pure string work, no model call) and probe the
# graph for them. This is DDB-only, so it never re-adds Bedrock load on the
# throttle-degrade path the router's `pattern` default exists to protect.
# ---------------------------------------------------------------------------

_BACKSTOP_CANDIDATE_CAP = 3
"""Max identifier-shaped tokens the deterministic backstop probes.

Bounds the DDB cost: at most this many extracted candidates are resolved
before giving up. Ranking tries real symbol shapes (backticked, snake_case,
dotted) before weaker camelCase guesses so the true symbol is not crowded
out of the cap by prose noise."""

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")


def _looks_like_identifier(tok: str) -> bool:
    """True when ``tok`` is shaped like a code symbol rather than prose.

    Accepts snake_case (has ``_``), a dotted path (``a.b`` with >=2 segments
    of >=2 chars each), or camelCase/PascalCase (an internal lower->Upper
    transition). A plain lowercase or sentence-Capitalized word is prose and
    returns False, so the backstop never fans DDB probes across ordinary
    words. Minimum length 3 drops stray noise like ``e.g``.
    """
    if len(tok) < 3:
        return False
    if "." in tok:
        segs = tok.split(".")
        return len(segs) >= 2 and all(
            len(s) >= 2 and s.isidentifier() for s in segs
        )
    if "_" in tok:
        return tok.isidentifier()
    return bool(re.search(r"[a-z][A-Z]", tok))


def _extract_candidate_identifiers(question: str) -> List[str]:
    """Deterministically pull identifier-shaped tokens from ``question``.

    Pure string work -- no model call. Backticked spans rank first (the
    strongest author signal), then snake_case / dotted paths, then
    camelCase; ties break on length. Capped at ``_BACKSTOP_CANDIDATE_CAP``.
    """
    if not question:
        return []
    backticked = set()
    for m in re.finditer(r"`([^`]+)`", question):
        for piece in _WORD_RE.findall(m.group(1)):
            piece = piece.strip(".")
            if _looks_like_identifier(piece):
                backticked.add(piece)
    cands: Dict[str, None] = {}
    for m in _WORD_RE.finditer(question):
        tok = m.group(0).strip(".")
        if tok and tok not in cands and _looks_like_identifier(tok):
            cands[tok] = None

    def _rank(tok: str) -> Tuple[int, int, int]:
        bt = 0 if tok in backticked else 1
        shape = 0 if ("_" in tok or "." in tok) else 1
        return (bt, shape, -len(tok))

    return sorted(cands, key=_rank)[:_BACKSTOP_CANDIDATE_CAP]


def _deterministic_backstop_symbol(
    question: str,
    workspace_ids: List[str],
    symbol_graph_table_name: str,
) -> Optional[str]:
    """DDB-only probe: does ``question`` name a symbol that resolves?

    Returns the first identifier-shaped token from the question that
    resolves in some selected workspace's graph, else ``None``. Uses only
    ``fuzzy_resolve_by_name`` (a GSI query) -- NO model/Bedrock call -- so it
    is safe to run on the router's throttle-degrade path.
    """
    for cand in _extract_candidate_identifiers(question):
        for ws in workspace_ids:
            if _fuzzy_resolve_symbol(
                ws, cand, symbol_graph_table_name
            ).canonicals:
                return cand
    return None


def augment_with_symbol_graph(
    question: str,
    workspace_ids: Optional[List[str]],
    *,
    user_id: str = "",
    source_ip: str = "",
    user_agent: str = "",
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
    audit_table_name: Optional[str] = None,
) -> Dict[str, Any]:
    """Route the question + (optionally) query the symbol graph.

    Table names default to ``os.environ`` (``SYMBOL_GRAPH_TABLE`` /
    ``CODE_MAPS_TABLE`` / ``AUDIT_TABLE``) when a kwarg is ``None``, so a
    caller that passes nothing behaves exactly as the chat handler did;
    query-kb / mcp-server can inject their own table names.

    Returns a dict with two keys:

    - ``decision``      : the ``RouterDecision`` (or ``None`` if the
                          augmentation path was skipped via the
                          ``GRAPH_RETRIEVAL_ENABLED`` gate or empty
                          workspace scope).
    - ``graph_block``   : a rendered "Symbol facts" string ready to
                          concatenate onto the RAG context, or ``""``
                          when no block should be emitted (pattern /
                          dependency / unknown without a symbol).

    The function is intentionally fail-soft - any Bedrock / DynamoDB
    error degrades to ``("", None)`` so the chat answer still ships.
    """

    # Also surface routing telemetry so the caller can emit a
    # CHAT_ROUTING_DECISION audit row alongside the rag-chunk count
    # it owns. Keys are intentionally additive — existing consumers
    # that only read ``decision`` / ``graph_block`` are unaffected.
    out: Dict[str, Any] = {
        "decision": None,
        "graph_block": "",
        "retrievals_fired": [],
        "graph_sha": None,
        "graph_staleness_seconds": None,
        "graph_result_count": None,
        # Structured site list for the metadata frame so the UI can
        # render graph results in the sources panel alongside RAG hits.
        # Populated when callers / importers / implementers fire; left
        # empty for pattern / dependency / unresolvable-symbol paths.
        "graph_sites": [],
        "graph_relation": None,
        # Why the graph query did not fire, for the UI chip + audit row.
        # None = the graph fired (or fired via backstop). Distinct reasons
        # let an operator tell a routing-design skip ("intent_pattern")
        # from a deploy bug ("no_table") without grep'ing CloudWatch.
        "graph_skip_reason": None,
        # Definition sites of the resolved target symbol(s), one dict per
        # (workspace, canonical) with repo / file_path / line. Consumers
        # read the real source lines for these (ADR-030 v1.2). Empty when
        # the graph did not fire or found no definition.
        "graph_definitions": [],
    }

    if not _graph_retrieval_enabled():
        out["graph_skip_reason"] = "kill_switch"
        return out
    if not question or not question.strip():
        out["graph_skip_reason"] = "no_question"
        return out
    # No workspace = no graph anchor (the reader requires workspace_id).
    if not workspace_ids:
        out["graph_skip_reason"] = "no_workspace"
        return out

    # Lazy import - keeps the reader's boto3 top-level import out of
    # module-init for chat tests that don't need it.
    try:
        from shared.symbol_graph_reader import (  # type: ignore
            SymbolGraphUnavailable,
            callers,
            definitions,
            implementers,
            importers,
        )
    except Exception as exc:  # pragma: no cover - import-failure path
        logger.warning(
            "[chat.augment] symbol_graph_reader import failed (%s); skipping",
            exc,
        )
        out["graph_skip_reason"] = "import_failed"
        return out

    # 1. Classify the question.
    primary_ws = workspace_ids[0]
    decision = classify_intent(
        question,
        audit_table_name=(
            audit_table_name
            if audit_table_name is not None
            else os.environ.get("AUDIT_TABLE", "")
        ),
        user_email=user_id or "",
        workspace_id=primary_ws,
        source_ip=source_ip,
        user_agent=user_agent,
    )
    out["decision"] = decision

    logger.info(
        "[chat.augment] router decision: intent=%s symbol=%s model=%s dur_ms=%d",
        decision.intent,
        decision.symbol_candidate,
        decision.model_id,
        decision.duration_ms,
    )

    # 2. Resolve the graph tables up front — both the backstop
    # probe and the main query path need them. (routing-decision table:
    # structural / hybrid / unknown -> fire on intent; pattern /
    # dependency -> skip unless the backstop resolves the symbol.)
    intent = decision.intent
    fires_by_intent = intent in ("structural", "hybrid", "unknown")
    code_maps_table = (
        code_maps_table_name
        if code_maps_table_name is not None
        else os.environ.get("CODE_MAPS_TABLE", "")
    ) or None
    symbol_graph_table = (
        symbol_graph_table_name
        if symbol_graph_table_name is not None
        else os.environ.get("SYMBOL_GRAPH_TABLE", "")
    ) or None

    if not symbol_graph_table:
        # IAM and env var are wired in tf-chat (terraform/lambda-chat.tf
        # + terraform/iam_symbol_graph.tf). If we got here without the
        # env var, that's a deploy bug - degrade gracefully but log it
        # for the rollout dashboard.
        logger.warning(
            "[chat.augment] SYMBOL_GRAPH_TABLE not set; skipping graph augmentation"
        )
        out["graph_skip_reason"] = "no_table"
        return out

    # Deterministic backstop (ADR-059): the router gave NO symbol AND
    # classified the question as non-firing (pattern/dependency -- which
    # includes the throttle-degrade default of pattern+None). A structural
    # question that NAMES a resolvable symbol would otherwise silently stay
    # RAG-only. Probe the question text for identifier-shaped tokens and, if
    # one resolves in the graph, adopt it and fire. DDB-only (no extra
    # Bedrock call), so the router's throttle-degrade quota-safety holds.
    #
    # It also runs for FIRING intents (structural / hybrid / unknown) when
    # the router extracted no symbol: "unknown" + no symbol otherwise
    # returned early with no_symbol, so a question naming
    # verify_magic_token got no graph facts and no source lines. ADR-059
    # says the backstop fires regardless of the intent verdict.
    effective_symbol = decision.symbol_candidate
    if not effective_symbol:
        bs_symbol = _deterministic_backstop_symbol(
            question, workspace_ids, symbol_graph_table
        )
        if bs_symbol:
            effective_symbol = bs_symbol
            fires_by_intent = True
            out["retrievals_fired"].append("backstop:deterministic")
            logger.info(
                "[hybrid.backstop] fired symbol=%s intent=%s "
                "(deterministic; router returned no symbol)",
                bs_symbol,
                intent,
            )

    # Without a symbol candidate the graph has nothing to anchor on.
    # skip-intent + no symbol = a plain pattern/dependency skip;
    # fire-intent + no symbol = render an explicit "no symbol" block so
    # the model surfaces it rather than fabricating.
    if not effective_symbol:
        if not fires_by_intent:
            out["graph_skip_reason"] = (
                "intent_pattern" if intent == "pattern" else "intent_dependency"
            )
            return out
        out["graph_skip_reason"] = "no_symbol"
        if intent == "unknown":
            return out
        out["graph_block"] = build_symbol_definition_block(
            symbol_candidate="(none extracted from question)",
            definitions=[],
        )
        return out

    user_symbol = effective_symbol
    is_subpath = "/" in user_symbol
    is_interface_shaped = user_symbol.lower().startswith(("i", "abstract"))
    ws_label = (
        "this workspace" if len(workspace_ids) == 1 else "the selected workspaces"
    )

    def _query(relation_fn, ws, target):
        return relation_fn(
            ws,
            target,
            symbol_graph_table_name=symbol_graph_table,
            code_maps_table_name=code_maps_table,
        )

    def _resolve_in(ws):
        # Exact match first (user typed a canonical-looking id); else
        # fuzzy-resolve the bare name to its canonical id(s) via the
        # GSI3 bare-name index. Returns (canonicals, truncated) so the
        # main flow can record a canonical-set clip into the prompt
        # context per ADR-032.
        if definitions(
            ws,
            user_symbol,
            symbol_graph_table_name=symbol_graph_table,
            code_maps_table_name=code_maps_table,
        ).sites:
            return [user_symbol], False
        resolution = _fuzzy_resolve_symbol(
            ws, user_symbol, symbol_graph_table
        )
        return resolution.canonicals, bool(resolution.truncated)

    try:
        # 3a. Fan out symbol resolution across ALL selected workspaces.
        # RAG already queries every selected workspace; the graph must
        # too, or a portfolio-wide question silently misses every
        # workspace except the first. Subpath symbols (with "/") ARE
        # their own canonical id, so they're queried directly.
        targets: List[Tuple[str, str]] = []
        canonicals_truncated_in_any_ws = False
        if is_subpath:
            targets = [(ws, user_symbol) for ws in workspace_ids]
        else:
            for ws in workspace_ids:
                canonicals, was_truncated = _resolve_in(ws)
                if was_truncated:
                    canonicals_truncated_in_any_ws = True
                for canonical in canonicals:
                    targets.append((ws, canonical))
        # ADR-032: when the canonical resolution itself was clipped, the
        # downstream relation fan-out is built on an incomplete set.
        # Surface a structured signal the chat synthesis prompt rule
        # binds against -- the LLM must disclose or refuse
        # the enumeration rather than ship the partial answer.
        out["graph_canonicals_truncated"] = canonicals_truncated_in_any_ws
        out["graph_canonicals_limit"] = _FUZZY_RESOLVE_LIMIT

        # 3b. Fire decision + backstop. The router may say skip
        # (PATTERN/DEPENDENCY), but if the extracted bare symbol actually
        # resolves in some selected workspace's graph, that resolution is
        # a stronger signal than the intent guess — fire anyway. Subpath
        # symbols are dependency-shaped; trust the router on those.
        if not fires_by_intent:
            if targets and not is_subpath:
                out["retrievals_fired"].append("backstop:resolved_symbol")
                logger.info(
                    "[chat.augment] backstop fired: intent=%s symbol=%s "
                    "resolved to %d workspace-target(s); running graph",
                    intent,
                    user_symbol,
                    len(targets),
                )
            else:
                out["graph_skip_reason"] = (
                    "backstop_no_resolve"
                    if not is_subpath
                    else ("intent_pattern" if intent == "pattern" else "intent_dependency")
                )
                return out

        # 3c. Bare name that resolved in NO workspace but intent said
        # fire: run the bare name (returns empty) so we emit an explicit
        # "0 results" block per empty-set discipline rather than letting
        # the model fabricate.
        if not targets:
            empty = _query(callers, workspace_ids[0], user_symbol)
            out["graph_block"] = build_symbol_facts_block(
                relation="callers",
                target_symbol_id=user_symbol,
                result=empty,
                definition_site=None,
                workspace_label=ws_label,
            )
            if "graph" not in out["retrievals_fired"]:
                out["retrievals_fired"].insert(0, "graph")
            out["graph_relation"] = "callers"
            out["graph_sha"] = getattr(empty, "graph_sha", None)
            out["graph_staleness_seconds"] = getattr(empty, "staleness_seconds", None)
            out["graph_result_count"] = len(getattr(empty, "sites", []) or [])
            out["graph_sites"] = []
            return out

        # 3d. Run the directional relation per (workspace, target),
        # merging into one block. importers for package paths;
        # interface-shaped names run both implementers + callers and keep
        # whichever surfaced more; else callers.
        def _run(relation_fn):
            definition_sites.clear()
            per_canonical: List[Any] = []
            merged: List[Dict[str, Any]] = []
            worst = None
            for ws, target in targets:
                cand_defs = definitions(
                    ws,
                    target,
                    symbol_graph_table_name=symbol_graph_table,
                    code_maps_table_name=code_maps_table,
                )
                def_site = cand_defs.sites[0] if cand_defs.sites else None
                if def_site is not None:
                    definition_sites.append({
                        "workspace_id": ws,
                        "symbol": target,
                        "repo": getattr(def_site, "repo", ""),
                        "file_path": getattr(def_site, "file_path", ""),
                        "line": getattr(def_site, "line", None),
                    })
                result = _query(relation_fn, ws, target)
                per_canonical.append((target, def_site, result))
                merged.extend(_site_to_dict(s) for s in result.sites)
                s = getattr(result, "staleness_seconds", None)
                if s is not None:
                    worst = s if worst is None else max(worst, s)
            return per_canonical, merged, worst

        definition_sites: List[Dict[str, Any]] = []
        if is_subpath:
            relation = "importers"
            per_canonical, merged, worst = _run(importers)
        elif is_interface_shaped:
            impl_pc, impl_merged, impl_worst = _run(implementers)
            call_pc, call_merged, call_worst = _run(callers)
            if sum(len(r.sites) for _, _, r in impl_pc) >= sum(
                len(r.sites) for _, _, r in call_pc
            ):
                relation, per_canonical, merged, worst = (
                    "implementers", impl_pc, impl_merged, impl_worst,
                )
            else:
                relation, per_canonical, merged, worst = (
                    "callers", call_pc, call_merged, call_worst,
                )
        else:
            relation = "callers"
            per_canonical, merged, worst = _run(callers)

        out["graph_block"] = build_multi_canonical_facts_block(
            symbol_candidate=user_symbol,
            per_canonical=per_canonical,
            relation=relation,
            workspace_label=ws_label,
        )
        # ADR-032 surface: when the canonical resolution was clipped,
        # the relation fan-out above is built on a partial canonical
        # set. Prepend the disclosure so the LLM (per
        # LIST_COMPLETENESS_RULES) reports "showing N of at least
        # canonicals" or refuses, instead of confidently enumerating
        # the partial set.
        if out.get("graph_canonicals_truncated"):
            out["graph_block"] = (
                f"NOTE: the bare-name lookup for {user_symbol!r} was "
                f"clipped at {_FUZZY_RESOLVE_LIMIT} canonicals. The "
                f"facts below are based on the first "
                f"{_FUZZY_RESOLVE_LIMIT} matches; more canonicals "
                f"exist in the workspace that were not enumerated. "
                f"You MUST surface this incompleteness in your answer "
                f"(per ADR-032).\n\n"
            ) + out["graph_block"]
        if "graph" not in out["retrievals_fired"]:
            out["retrievals_fired"].insert(0, "graph")
        out["graph_relation"] = relation
        out["graph_definitions"] = list(definition_sites)
        out["graph_result_count"] = len(merged)
        out["graph_sites"] = merged
        out["graph_staleness_seconds"] = worst
    except SymbolGraphUnavailable as exc:
        logger.warning(
            "[chat.augment] SymbolGraphUnavailable for %s: %s; degrading",
            user_symbol,
            exc,
        )
        out["graph_skip_reason"] = "query_error"
        out["graph_block"] = build_graph_unavailable_block(
            target_symbol_id=user_symbol
        )
    except Exception:  # pragma: no cover - defensive
        logger.exception(
            "[chat.augment] unexpected error querying symbol graph for %s",
            user_symbol,
        )
        out["graph_skip_reason"] = "query_error"
        out["graph_block"] = build_graph_unavailable_block(
            target_symbol_id=user_symbol
        )

    return out


# ---------------------------------------------------------------------------
# Code-map hits -> definition sites (ADR-030 v1.3). When the question names no
# symbol, the graph lane has nothing to anchor on and chat would answer exact
# values from documentation alone. The top code-map hits, though, name the
# functions that implement the behavior being asked about. Pull those names
# out of the summary text, keep the ones that share words with the question,
# and resolve them to definition sites in the hit's own workspace and repo so
# chat can attach their real source lines. DynamoDB only, no model call.
# ---------------------------------------------------------------------------

_CODE_MAP_HIT_LIMIT = 10
"""How many of the top code-map hits to mine for symbol names. Wide on
purpose: the summary that names the function holding the answer is often not
in the top three, and ranking (shared-word count, then rarity) keeps the
extra names from crowding it out."""

_CODE_MAP_CANDIDATE_CAP = 6
"""Max candidate names resolved per turn; bounds the DynamoDB queries."""

_RERANK_POOL = 20
"""How many word-ranked candidates are sent to the reranker (per clause)."""

_RERANK_MAX_CLAUSES = 4
"""Max question clauses reranked separately; bounds rerank calls per turn."""

_CLAUSE_SPLIT_RE = re.compile(
    r"[?;]+|,\s*(?:and\s+|or\s+)?(?=(?:how|what|why|when|where|which|who|whose|is|are|does|do|did|can|should)\b)"
    r"|\s+(?:and|or)\s+(?=(?:how|what|why|when|where|which|who|whose)\b)",
    re.IGNORECASE,
)
"""Where a multi-part question splits: sentence ends, semicolons, and a comma
or "and" that starts a new question ("..., how long is it valid, and how is
single use enforced"). A comma inside a list ("create, verify and claim")
does not split, because no question word follows it."""

_RERANK_CONTEXT_CHARS = 600
"""Max summary text, per candidate, that the reranker judges it by."""

RerankFn = Callable[[str, List[Dict[str, Any]]], List[Dict[str, Any]]]
"""(question, docs with a ``text`` key) -> the docs reordered, each carrying
``rerank_score``. ``shared.retrieval.rerank`` has this shape once bound to a
client and model; on failure it returns the docs unscored."""

_CANONICALS_PER_CANDIDATE = 3
"""Max canonical ids checked for one bare name (overloads across modules)."""

_QUESTION_STOPWORDS = frozenset(
    "the and for are what how does do did is was were with from this that "
    "into when where which who why can could should would will about there "
    "their them they its our your you get set use used using app".split()
)

_TEST_PATH_RE = re.compile(r"(^|/)(tests?|__tests__|spec)(/|$)|(^|/)test_[^/]*$|_test\.[a-z]+$|\.(test|spec)\.[a-z]+$")


def _identifier_parts(name: str) -> List[str]:
    """Split ``create_magic_token`` / ``createMagicToken`` into lowercase words."""
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", name)
    return [p.lower() for p in re.split(r"[_\s]+", spaced) if p]


def _stem(word: str) -> str:
    """Crude plural fold so 'links' matches 'link' and 'tokens' matches 'token'."""
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def _question_words(question: str) -> set:
    words = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9]*", question or ""):
        for part in _identifier_parts(tok):
            if len(part) >= 3 and part not in _QUESTION_STOPWORDS:
                words.add(_stem(part))
    return words


def _summary_identifiers(text: str) -> List[str]:
    """Every snake_case / camelCase name in a code-map summary, in order.

    Unlike the question backstop this is uncapped and skips dotted module
    paths: code-map prose names functions by their bare name, and a dotted
    path would not resolve through the bare-name index anyway.
    """
    seen: Dict[str, None] = {}
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text or ""):
        tok = tok.strip("_") if tok.startswith("__") and tok.endswith("__") else tok
        if tok and "." not in tok and tok not in seen and _looks_like_identifier(tok):
            seen[tok] = None
    return list(seen)


def _is_code_map_hit(hit: Dict[str, Any]) -> bool:
    if hit.get("lane") == "code_map":
        return True
    if hit.get("lane") == "docs":
        return False
    try:
        from shared.retrieval import CODE_MAP_TYPES  # type: ignore
    except Exception:  # pragma: no cover - import failure
        return False
    return (hit.get("type") or "") in CODE_MAP_TYPES


def _bare_repo(repo: str) -> str:
    return (repo or "").rstrip("/").rsplit("/", 1)[-1]


def code_map_definitions(
    question: str,
    search_results: List[Dict[str, Any]],
    workspace_ids: Optional[List[str]],
    *,
    max_definitions: int = 3,
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
    rerank_fn: Optional[RerankFn] = None,
) -> List[Dict[str, Any]]:
    """Definition sites for the functions the top code-map hits describe.

    Returns dicts shaped like ``graph_definitions`` (``workspace_id``,
    ``symbol``, ``repo``, ``file_path``, ``line``) so they feed straight into
    ``shared.source_reader.read_definitions``. Never raises; an empty list
    means nothing resolvable was found.

    Scope: a hit is only mined when its workspace is one the caller searched,
    and a name only counts when it resolves to a non-test definition in the
    hit's own repo, so a summary that mentions another repo's helper cannot
    pull that repo's source in.

    ``rerank_fn``, when given, orders the candidates by how well the summary
    text around each name answers the question. Without it, or if it fails,
    the word ranking stands.
    """
    try:
        return _code_map_definitions(
            question,
            search_results,
            workspace_ids,
            max_definitions=max_definitions,
            symbol_graph_table_name=symbol_graph_table_name,
            code_maps_table_name=code_maps_table_name,
            rerank_fn=rerank_fn,
        )
    except Exception:  # noqa: BLE001 - fail-soft, chat must still answer
        logger.exception("[hybrid.code_map_defs] failed; no source lines")
        return []


def _name_context(text: str, name: str) -> str:
    """The summary sentences that mention ``name``, prefixed with it, capped
    at ``_RERANK_CONTEXT_CHARS``: what the reranker judges the name by."""
    pattern = re.compile(r"(?<![A-Za-z0-9_])" + re.escape(name) + r"(?![A-Za-z0-9_])")
    picked: List[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if pattern.search(sentence):
            picked.append(sentence.strip())
    return f"{name}: {' '.join(picked)}"[:_RERANK_CONTEXT_CHARS]


def _rerank_candidates(
    question: str,
    pool: List[Tuple[Any, ...]],
    contexts: Dict[Tuple[str, str, str], str],
    rerank_fn: RerankFn,
) -> Optional[List[Tuple[Any, ...]]]:
    """``pool`` reordered by the reranker, or None to keep the word order
    (rerank raised, returned nothing, or fell back to unscored docs)."""
    docs = [
        {"text": contexts.get((c[-3], c[-2], c[-1]), c[-1]), "_idx": i}
        for i, c in enumerate(pool)
    ]
    try:
        ranked = rerank_fn(question, docs)
    except Exception:  # noqa: BLE001 - fall back to the word ranking
        logger.exception("[hybrid.code_map_defs] candidate rerank failed")
        return None
    if not ranked or any("rerank_score" not in d for d in ranked):
        return None
    order = [d["_idx"] for d in ranked if isinstance(d.get("_idx"), int)]
    seen_idx = set(order)
    order += [i for i in range(len(pool)) if i not in seen_idx]
    return [pool[i] for i in order]


def _question_clauses(question: str) -> List[str]:
    """The separate asks in a multi-part question, each with at least one
    content word, capped at ``_RERANK_MAX_CLAUSES``. A single ask comes back
    as the whole question."""
    parts = [p.strip(" ,.") for p in _CLAUSE_SPLIT_RE.split(question or "")]
    clauses = [p for p in parts if p and _question_words(p)]
    if len(clauses) < 2:
        return [question]
    return clauses[:_RERANK_MAX_CLAUSES]


def _rerank_by_clause(
    question: str,
    pool: List[Tuple[Any, ...]],
    contexts: Dict[Tuple[str, str, str], str],
    rerank_fn: RerankFn,
) -> Tuple[Optional[List[Tuple[Any, ...]]], int]:
    """``pool`` ordered by the reranker, one rerank per question clause.

    Scored against a whole multi-part question, the clause that names the
    topic ("how do magic links work") dominates, and names about the other
    asks ("what's in the token", "how is single use enforced") sink below
    the read limit. So each clause ranks the pool on its own and the result
    takes the best remaining name of each clause in turn. Each later clause
    carries the first one in parentheses, since an ask like "what's in the
    token" alone matches any token (live, it picked ``access_token`` over
    ``create_magic_token``). A clause whose rerank fails is skipped;
    returns (None, 0) if every one failed.
    """
    clauses = _question_clauses(question)
    if len(clauses) == 1:
        return _rerank_candidates(question, pool, contexts, rerank_fn), 1
    queries = [clauses[0]] + [f"{c} ({clauses[0]})" for c in clauses[1:]]
    with ThreadPoolExecutor(max_workers=len(queries)) as executor:
        orders = list(executor.map(
            lambda query: _rerank_candidates(query, pool, contexts, rerank_fn),
            queries,
        ))
    orders = [o for o in orders if o is not None]
    if not orders:
        return None, 0
    merged: List[Tuple[Any, ...]] = []
    taken = set()
    for rank in range(len(pool)):
        for order in orders:
            item = order[rank]
            if item not in taken:
                taken.add(item)
                merged.append(item)
    return merged, len(orders)


def _code_map_definitions(
    question: str,
    search_results: List[Dict[str, Any]],
    workspace_ids: Optional[List[str]],
    *,
    max_definitions: int,
    symbol_graph_table_name: Optional[str],
    code_maps_table_name: Optional[str],
    rerank_fn: Optional[RerankFn] = None,
) -> List[Dict[str, Any]]:
    if max_definitions < 1 or not search_results or not workspace_ids:
        return []
    if not _graph_retrieval_enabled():
        return []
    symbol_graph_table = (
        symbol_graph_table_name
        if symbol_graph_table_name is not None
        else os.environ.get("SYMBOL_GRAPH_TABLE", "")
    ) or None
    code_maps_table = (
        code_maps_table_name
        if code_maps_table_name is not None
        else os.environ.get("CODE_MAPS_TABLE", "")
    ) or None
    if not symbol_graph_table:
        return []
    q_words = _question_words(question)
    if not q_words:
        return []
    allowed = set(workspace_ids)

    # 1. Candidate names from the top code-map hits that share at least one
    #    word with the question.
    matched: List[Tuple[int, str, str, str, set, int]] = []
    contexts: Dict[Tuple[str, str, str], str] = {}
    seen = set()
    order = 0
    hits = [h for h in search_results if _is_code_map_hit(h)][:_CODE_MAP_HIT_LIMIT]
    for hit in hits:
        meta = hit.get("metadata") or {}
        ws = hit.get("workspace_id") or meta.get("workspace_id") or ""
        repo = _bare_repo(meta.get("repo") or hit.get("repo") or "")
        if ws not in allowed or not repo:
            continue
        text = hit.get("content") or hit.get("text") or meta.get("content") or ""
        for name in _summary_identifiers(text):
            key = (ws, repo, name)
            if key in seen:
                continue
            seen.add(key)
            parts = {_stem(p) for p in _identifier_parts(name)}
            shared = parts & q_words
            if shared:
                matched.append((order, ws, repo, name, shared, len(parts)))
                contexts[key] = _name_context(text, name)
                order += 1

    # 2. Rank by the number of question words the name shares, so a name
    #    that matches one incidental word ("app") never outranks one that
    #    matches two. Among names sharing as many words, a word every
    #    candidate shares ("magic" in a magic-link question) says nothing
    #    about which function holds the answer; a word only a few share
    #    ("token") does. So break that tie by the sum of 1 / (number of
    #    candidates containing the word), then by the share of the name's
    #    words matched, then by earlier appearance.
    doc_freq: Dict[str, int] = {}
    for _o, _ws, _repo, _name, shared, _n in matched:
        for w in shared:
            doc_freq[w] = doc_freq.get(w, 0) + 1
    scored = sorted(
        (
            -len(shared),
            -sum(1.0 / doc_freq[w] for w in shared),
            -len(shared) / n_parts,
            o, ws, repo, name,
        )
        for o, ws, repo, name, shared, n_parts in matched
    )
    # 3. Rerank. Word overlap cannot tell the topic of a question ("magic
    #    link") from what is asked about it ("token", "how long"): in an
    #    auth summary "token" can be commoner than "link". So when a
    #    reranker is available, order the top of the word ranking by how
    #    well the summary text around each name answers the question, one
    #    rerank per clause of a multi-part question.
    ranking = "words"
    clauses_ranked = 0
    if rerank_fn is not None and len(scored) > 1:
        pool = scored[:_RERANK_POOL]
        reordered, clauses_ranked = _rerank_by_clause(question, pool, contexts, rerank_fn)
        if reordered is not None:
            scored = reordered + scored[_RERANK_POOL:]
            ranking = "rerank"
    candidates = scored[:_CODE_MAP_CANDIDATE_CAP]

    from shared.symbol_graph_reader import definitions  # type: ignore

    # 4. Resolve each name to a non-test definition in the hit's repo.
    out: List[Dict[str, Any]] = []
    resolved_names: List[str] = []
    for _count, _score, _ratio, _ord, ws, repo, name in candidates:
        if len(out) >= max_definitions:
            break
        resolution = _fuzzy_resolve_symbol(ws, name, symbol_graph_table)
        for canonical in list(resolution.canonicals)[:_CANONICALS_PER_CANDIDATE]:
            sites = definitions(
                ws,
                canonical,
                symbol_graph_table_name=symbol_graph_table,
                code_maps_table_name=code_maps_table,
            ).sites
            site = next(
                (
                    s for s in sites
                    if _bare_repo(getattr(s, "repo", "")) == repo
                    and not _TEST_PATH_RE.search(getattr(s, "file_path", "") or "")
                ),
                None,
            )
            if site is None:
                continue
            out.append({
                "workspace_id": ws,
                "symbol": canonical,
                "repo": getattr(site, "repo", ""),
                "file_path": getattr(site, "file_path", ""),
                "line": getattr(site, "line", None),
            })
            resolved_names.append(name)
            break
    print(
        f"[SOURCE] code-map fallback: hits={len(hits)} ranking={ranking} "
        f"clauses={clauses_ranked} "
        f"candidates={[c[6] for c in candidates]} resolved={resolved_names}"
    )
    return out
