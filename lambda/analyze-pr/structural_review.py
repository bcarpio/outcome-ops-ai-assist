"""Structural-impact block builder for analyze-pr (plan-gkg chunk 06).

Given the list of ``MutatedSymbol``s identified by
``diff_symbol_resolver.resolve_targets`` and the chunk-04 query API,
this module renders the Markdown block that gets posted on the PR
(and, downstream, folded into the reviewer-model's prompt by the
per-check workers).

The block format is documented verbatim in plan-gkg/06 "Structural-
impact block" and reproduced below as the expected output of
``build_impact_block``. Empty results are first-class -- a symbol with
zero callers renders as "no known callers" rather than being silently
omitted, matching the "honest empty-set" audit unlock that motivates
this whole layer (plan-gkg/overview.md "Why this exists").

Failure modes (mirrors plan-gkg/06 "Failure modes"):

- ``SymbolGraphUnavailable``           -> block omitted; the caller's
                                          comment-rendering function
                                          appends a "structural retrieval
                                          unavailable" disclaimer.
- Backfill not complete (no pointer)   -> block rendered with no
                                          staleness footer; caller can
                                          append the "backfill pending"
                                          note if it wants to.
- Stale graph                          -> block rendered AS-OF the
                                          older SHA with a disclaimer
                                          footer; the resolution of
                                          plan-gkg/overview.md open
                                          question #3 ("query as-of
                                          base SHA with a staleness
                                          disclaimer").
"""

from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# sys.path bootstrap so ``from shared.symbol_graph_reader import ...``
# resolves under the live Lambda layout (analyze-pr's deploy zip already
# includes shared/ via terraform source_path) AND the local test layout
# (where we put lambda/ on sys.path). Same approach as the handler.
# ---------------------------------------------------------------------------

_LAMBDA_DIR_GUESS = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _LAMBDA_DIR_GUESS not in sys.path:
    sys.path.insert(0, _LAMBDA_DIR_GUESS)


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolReviewImpact:
    """Aggregated reviewer-impact for one mutated symbol.

    ``mutated`` is the ``MutatedSymbol`` from
    ``diff_symbol_resolver.resolve_targets`` (the contract of "this
    symbol was changed in this PR"). ``callers`` is the reader's
    ``CallSite`` list (the contract of "these are the affected sites
    in the workspace's graph"). ``truncated`` mirrors the reader's
    ``QueryResult.truncated`` so the rendered block can disclose
    truncation.

    ``graph_sha`` / ``staleness_seconds`` describe the freshness of
    the graph slice that produced this impact. Both are
    per-symbol because the reader resolves the per-repo pointer
    individually; multi-repo answers in v1 collapse to None for
    ``graph_sha`` (matching the reader's contract).

    ``error`` carries a short string when the lookup failed for this
    specific symbol. v1 surfaces this in the block instead of failing
    the whole pass.
    """

    mutated: object  # MutatedSymbol -- avoid hard import to skip a cycle
    callers: List[object]  # list[CallSite]
    truncated: bool
    graph_sha: Optional[str]
    staleness_seconds: Optional[int]
    error: Optional[str]


# ---------------------------------------------------------------------------
# Limits -- mirror the chunk-04 reader defaults so this layer doesn't
# silently impose stricter caps. callers=200 / importers=500 /
# implementers=100 per plan-gkg/04. The analyze-pr block tends to be
# rendered to a PR comment (40k char ceiling) AND to a model prompt
# (token budget), so we trim each list at 50 in the rendered Markdown
# but query the reader for the full 200 so the audit log can carry the
# exact-count tally.
# ---------------------------------------------------------------------------

_CALLERS_QUERY_LIMIT = 200
_IMPORTERS_QUERY_LIMIT = 500
_IMPLEMENTERS_QUERY_LIMIT = 100

# Max caller sites rendered into the Markdown block per symbol. The
# audit row carries the full count; the block trims so a 200-caller
# blast radius doesn't blow the PR comment limit.
_RENDERED_CALLERS_CAP = 20


# ---------------------------------------------------------------------------
# Pointer-based base-SHA staleness check
# ---------------------------------------------------------------------------


def _staleness_phrase(staleness_seconds: Optional[int]) -> str:
    """Human-readable freshness phrase. Plan-gkg/04 "Staleness reporting"
    rule of thumb is the same shape as the chat context-builder's
    ``_staleness_phrase`` -- we re-implement here (instead of importing
    from ``chat.context_builder``) so analyze-pr's deploy zip doesn't
    have to ship the chat module.
    """

    if staleness_seconds is None:
        return ""
    if staleness_seconds < 60:
        return f"committed {staleness_seconds} seconds ago"
    if staleness_seconds < 600:
        return f"committed {staleness_seconds // 60} minutes ago"
    if staleness_seconds < 24 * 3600:
        hours = staleness_seconds // 3600
        return f"committed {hours} hour{'s' if hours != 1 else ''} ago"
    days = staleness_seconds // (24 * 3600)
    return f"committed {days} day{'s' if days != 1 else ''} ago"


def _sha_footer_line(graph_sha: Optional[str], staleness_seconds: Optional[int]) -> str:
    """Render the "Graph as-of base SHA: ..." footer.

    Mirrors plan-gkg/06's example output. For multi-repo answers
    ``graph_sha`` is None and we render only the staleness phrase.

    Resolution of plan-gkg/overview.md open question #3 (recorded in
    the chunk-06 plan and passed in by the agent runner): query
    as-of base SHA with a "staleness" disclaimer. This function is
    that disclaimer.
    """

    phrase = _staleness_phrase(staleness_seconds)
    if graph_sha and phrase:
        short = graph_sha[:7]
        return f"Graph as-of base SHA: {short} ({phrase})"
    if graph_sha:
        short = graph_sha[:7]
        return f"Graph as-of base SHA: {short}"
    if phrase:
        return f"Graph freshness: oldest contributing repo {phrase}"
    return ""


# ---------------------------------------------------------------------------
# Reviewer notes -- the short prompt-aligned blurb under each symbol
# ---------------------------------------------------------------------------


_REVIEWER_NOTE_SIGNATURE = (
    "Reviewer note: signature change. Verify each caller's invocation is "
    "updated, or that defaults preserve backward compatibility."
)

_REVIEWER_NOTE_BODY = (
    "Reviewer note: behavior change. No signature change; callers do not "
    "require updates but behavioral assumptions may."
)


# ---------------------------------------------------------------------------
# Symbol classifiers -- decide which reader query to fire
# ---------------------------------------------------------------------------


def _is_subpath_symbol(symbol_id: str) -> bool:
    """True when the symbol ID is a package/subpath rather than a
    function/method ID. Subpath shapes go through ``importers()``;
    function/method shapes go through ``callers()``. Same heuristic
    the generate-code impact_analysis uses (plan-gkg/07).
    """

    body = symbol_id.split(":", 1)[1] if ":" in symbol_id else symbol_id
    return "::" not in body and "/" in body


# ---------------------------------------------------------------------------
# Graph query orchestration
# ---------------------------------------------------------------------------


def analyze_structural_impact(
    *,
    workspace_id: str,
    mutated: Sequence[object],
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> List[SymbolReviewImpact]:
    """Run the reader queries for each mutated symbol.

    Resilient: a ``SymbolGraphUnavailable`` (or any other reader
    exception) for one symbol produces a ``SymbolReviewImpact`` with
    ``error`` set and the analysis continues with the rest. This
    matches the "graph unavailable -> block omitted, review still
    ships" failure-mode in plan-gkg/06.
    """

    if not workspace_id or not mutated:
        return []

    # Lazy import (matches generate-code.impact_analysis pattern). The
    # reader carries its own boto3 lazy-init + test-injection hooks
    # documented at the top of ``shared/symbol_graph_reader.py``.
    try:
        from shared.symbol_graph_reader import (  # type: ignore
            SymbolGraphUnavailable,
            callers as q_callers,
            importers as q_importers,
        )
    except Exception as exc:  # pragma: no cover - import path
        logger.warning(
            "[structural_review] symbol_graph_reader import failed: %s", exc
        )
        return []

    results: List[SymbolReviewImpact] = []
    for target in mutated:
        try:
            if _is_subpath_symbol(target.symbol_id):
                qr = q_importers(
                    workspace_id,
                    target.symbol_id,
                    limit=_IMPORTERS_QUERY_LIMIT,
                    symbol_graph_table_name=symbol_graph_table_name,
                    code_maps_table_name=code_maps_table_name,
                )
            else:
                qr = q_callers(
                    workspace_id,
                    target.symbol_id,
                    limit=_CALLERS_QUERY_LIMIT,
                    symbol_graph_table_name=symbol_graph_table_name,
                    code_maps_table_name=code_maps_table_name,
                )
            results.append(
                SymbolReviewImpact(
                    mutated=target,
                    callers=list(qr.sites),
                    truncated=bool(qr.truncated),
                    graph_sha=qr.graph_sha,
                    staleness_seconds=qr.staleness_seconds,
                    error=None,
                )
            )
        except SymbolGraphUnavailable as exc:
            logger.warning(
                "[structural_review] graph unavailable for %s: %s",
                target.symbol_id,
                exc,
            )
            results.append(
                SymbolReviewImpact(
                    mutated=target,
                    callers=[],
                    truncated=False,
                    graph_sha=None,
                    staleness_seconds=None,
                    error="graph_unavailable",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[structural_review] unexpected error querying %s: %s",
                target.symbol_id,
                exc,
            )
            results.append(
                SymbolReviewImpact(
                    mutated=target,
                    callers=[],
                    truncated=False,
                    graph_sha=None,
                    staleness_seconds=None,
                    error=f"unexpected:{type(exc).__name__}",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


# Header used at the top of the rendered impact block. Stable string
# so PR comment dedupers (and the per-check workers folding this into
# their prompts) can match on it.
STRUCTURAL_IMPACT_HEADER = "## Structural impact"

# Appended after the per-symbol section when the impact block ships
# with the review prompt. Tells the reviewer-model to use the block as
# ground truth for caller statements (graph-grounded, plan-gkg/06
# "ADR-aware reviewer prompt").
REVIEWER_GROUNDING_INSTRUCTION = (
    "When commenting on caller / blast-radius impact, prefer the "
    "'Structural impact' block as ground truth -- never invent callers "
    "that are not listed here. If a symbol's block reports zero "
    "callers, say so honestly. If the block is absent or the structural "
    "retrieval was unavailable, disclose that to the reviewer."
)


def _sort_callers_stable(callers: Iterable[object]) -> List[object]:
    """Stable order so the Markdown block is reproducible across runs.

    The reader doesn't promise an order (plan-gkg/04 "Performance"
    notes results may come page-first), so we sort here.
    """

    return sorted(
        callers,
        key=lambda s: (getattr(s, "repo", "") or "", getattr(s, "file_path", "") or "", int(getattr(s, "line", 0) or 0)),
    )


def _render_symbol_section(impact: SymbolReviewImpact) -> List[str]:
    """Render one symbol's section. Returns a list of lines."""

    mut = impact.mutated
    change_kind = getattr(mut, "change_kind", "body")
    symbol_id = getattr(mut, "symbol_id", "<unknown>")

    header = f"### `{symbol_id}` ({change_kind}-changed)"
    lines: List[str] = [header, ""]
    lines.append(
        f"Definition: {getattr(mut, 'file_path', '?')}:{getattr(mut, 'definition_line', 0)}"
    )

    # Error path -- structural retrieval failed for this symbol only.
    if impact.error == "graph_unavailable":
        lines.append("")
        lines.append(
            "Structural retrieval unavailable for this symbol (graph store "
            "transient failure). The reviewer should treat caller-impact "
            "claims as unverified."
        )
        return lines
    if impact.error:
        lines.append("")
        lines.append(
            f"Structural retrieval error: {impact.error}; treat as best-effort."
        )
        return lines

    callers = _sort_callers_stable(impact.callers)
    count = len(callers)
    is_subpath = _is_subpath_symbol(symbol_id)
    label = "Importers" if is_subpath else "Callers affected"

    lines.append("")
    if count == 0:
        lines.append(f"{label}: none in this workspace.")
    else:
        truncation_suffix = " (truncated; more exist)" if impact.truncated else ""
        rendered_cap = min(count, _RENDERED_CALLERS_CAP)
        if rendered_cap < count or impact.truncated:
            lines.append(
                f"{label} ({count}{truncation_suffix}, showing first {rendered_cap}):"
            )
        else:
            lines.append(f"{label} ({count}):")
        for site in callers[:rendered_cap]:
            anchor = f"{getattr(site, 'file_path', '?')}:{getattr(site, 'line', 0)}"
            repo_tag = ""
            repo = getattr(site, "repo", "") or ""
            if repo:
                repo_tag = f" ({repo})"
            lines.append(f"- {anchor}{repo_tag}")
        if rendered_cap < count:
            lines.append(f"- ... [{count - rendered_cap} more]")

    # Reviewer note tailored to the change kind. Body-changes get the
    # softer language; signature changes get the "verify each caller"
    # language per the plan example.
    lines.append("")
    if change_kind == "signature":
        lines.append(_REVIEWER_NOTE_SIGNATURE)
    else:
        lines.append(_REVIEWER_NOTE_BODY)

    return lines


def _aggregate_freshness(
    impacts: Sequence[SymbolReviewImpact],
) -> tuple[Optional[str], Optional[int]]:
    """Compute the worst-case SHA + staleness across symbols.

    Used for the single footer at the bottom of the block. Mirrors the
    reader's worst-case-pointer choice (plan-gkg/04 "Staleness reporting"
    + the reader's ``_pointer_for_workspace`` semantics).

    Returns ``(graph_sha_or_None, max_staleness_or_None)``. If multiple
    SHAs contribute we drop the SHA (consistent with the reader: "no
    single SHA describes a multi-repo answer") and surface only the
    staleness phrase, which still anchors freshness.
    """

    shas: List[str] = []
    max_stale: Optional[int] = None
    for impact in impacts:
        if impact.graph_sha:
            shas.append(impact.graph_sha)
        if impact.staleness_seconds is not None:
            if max_stale is None or impact.staleness_seconds > max_stale:
                max_stale = impact.staleness_seconds

    unique_shas = list(dict.fromkeys(shas))
    if len(unique_shas) == 1:
        return (unique_shas[0], max_stale)
    return (None, max_stale)


def build_impact_block(
    impacts: Sequence[SymbolReviewImpact],
    *,
    base_sha: Optional[str] = None,
) -> str:
    """Render the full Markdown structural-impact block.

    Returns an empty string when ``impacts`` is empty -- the caller
    should NOT append anything to the PR review in that case (the
    block would just be a heading with no content). Matches the
    "structural section omitted" failure-mode language.

    The ``base_sha`` parameter records the PR's base SHA for the
    footer line. When the reader could not anchor on a single
    contributing SHA we still report the base SHA in the footer so
    the human reviewer knows which commit the analysis was performed
    against -- the staleness phrase covers the "is the graph behind
    base?" disclaimer separately.
    """

    if not impacts:
        return ""

    lines: List[str] = [STRUCTURAL_IMPACT_HEADER, ""]
    for impact in impacts:
        lines.extend(_render_symbol_section(impact))
        lines.append("")  # blank line between symbol sections

    aggregated_sha, max_stale = _aggregate_freshness(impacts)
    # Prefer the explicit base_sha for the footer SHA -- the reader's
    # graph_sha is the SHA the writer indexed against, which is the
    # graph SHA, not the PR base SHA. When they're the same the
    # footer reads cleanly; when they're not, the staleness phrase
    # already discloses it.
    footer_sha = base_sha or aggregated_sha
    footer = _sha_footer_line(footer_sha, max_stale)
    if footer:
        lines.append(footer)

    # Trim trailing blank lines (a tidy block, easier to diff in tests).
    while lines and lines[-1] == "":
        lines.pop()

    return "\n".join(lines)


def build_unavailable_block(*, base_sha: Optional[str] = None) -> str:
    """Render the fallback "structural retrieval unavailable" block.

    Emitted when ``analyze_structural_impact`` returned an empty list
    AND the symbol-graph reader had been expected to answer (e.g. when
    we *had* mutated symbols but the reader raised
    ``SymbolGraphUnavailable`` across the board). The PR comment
    should still inform the human reviewer that structural facts could
    not be verified for this turn.
    """

    lines: List[str] = [STRUCTURAL_IMPACT_HEADER, ""]
    lines.append(
        "Structural retrieval was unavailable for this PR (graph store "
        "transient failure or backfill pending). The ADR / convention "
        "checks below proceed as normal, but caller-impact statements in "
        "the review should be treated as unverified."
    )
    if base_sha:
        lines.append("")
        lines.append(f"Graph as-of base SHA: {base_sha[:7]} (lookup failed)")
    return "\n".join(lines)


__all__ = (
    "REVIEWER_GROUNDING_INSTRUCTION",
    "STRUCTURAL_IMPACT_HEADER",
    "SymbolReviewImpact",
    "analyze_structural_impact",
    "build_impact_block",
    "build_unavailable_block",
)
