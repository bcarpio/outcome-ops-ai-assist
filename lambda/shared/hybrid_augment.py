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
from typing import Any, Dict, List, Optional, Tuple

from shared.router import classify_intent
from shared.context_builder import (
    build_graph_unavailable_block,
    build_multi_canonical_facts_block,
    build_symbol_definition_block,
    build_symbol_facts_block,
)

logger = logging.getLogger(__name__)

__all__ = ("augment_with_symbol_graph",)


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
