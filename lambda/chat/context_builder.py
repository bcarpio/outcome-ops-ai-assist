"""Chat-side context builder.

Renders structured retrieval results into the model's input context.
Three block shapes are produced:

- ``build_symbol_facts_block``  : graph caller/importer/implementer
                                  results from
                                  ``shared.symbol_graph_reader``.
- ``build_symbol_definition_block``: definition sites (output of
                                     ``definitions(...)``) — used both
                                     to anchor caller queries and as
                                     a "no symbol found" disclosure
                                     when the symbol candidate is
                                     ambiguous.
- ``build_graph_unavailable_block``: explicit fallback rendering when
                                     ``SymbolGraphUnavailable`` is
                                     raised, so the chat answer
                                     surfaces "structural retrieval
                                     unavailable" rather than silently
                                     dropping facts.

These blocks are stable, deterministic strings — no JSON, no
embeddings, no embedding of repo/path beyond what already exists in
the audit-pipeline path discipline. The renderer is intentionally
side-effect-free: callers (``chat.handler``) compose blocks into the
final system prompt + user-prompt context.
"""

from __future__ import annotations

import logging
from typing import Iterable, List, Optional

from shared.symbol_graph_reader import CallSite, QueryResult

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------


SYMBOL_FACTS_HEADER = "## Symbol facts (graph-retrieved)"

# Appended to the chat system prompt when at least one structural block
# is included in the user-prompt context. Tells the model to treat the
# blocks as ground truth and surface empty sets honestly.
GRAPH_GROUNDING_INSTRUCTION = (
    "When answering, prefer facts from the 'Symbol facts (graph-retrieved)' "
    "block over inference. The block is the ground truth for callers, "
    "importers, and implementers in this workspace. If the block lists no "
    "callers (or no importers / implementers), say so honestly — do not "
    "fabricate a list. If the block notes that structural retrieval was "
    "unavailable, disclose that to the user so they can re-run the query."
)


# ---------------------------------------------------------------------------
# Staleness rendering — rule-of-thumb humanization of the reader's
# staleness_seconds output.
# ---------------------------------------------------------------------------


def _staleness_phrase(staleness_seconds: Optional[int]) -> str:
    """Render staleness in human terms.

    Returns "" when the reader couldn't anchor on a single SHA
    (multi-repo answers) — the caller decides whether to omit the
    suffix entirely or keep it tacked on the header.
    """

    if staleness_seconds is None:
        return ""
    if staleness_seconds < 60:
        return f"committed {staleness_seconds} seconds ago"
    if staleness_seconds < 600:
        return f"committed {staleness_seconds // 60} minutes ago"
    if staleness_seconds < 24 * 3600:
        hours = staleness_seconds // 3600
        return f"committed {hours} hours ago"
    days = staleness_seconds // (24 * 3600)
    return f"committed {days} days ago (graph may be stale)"


def _sha_suffix(graph_sha: Optional[str], staleness_seconds: Optional[int]) -> str:
    """Build the "Graph as-of SHA: ..." trailer.

    For multi-repo answers ``graph_sha`` is None (no single SHA
    describes the answer) — the trailer omits the SHA but keeps the
    staleness phrase, matching the plan's "worst-case freshness across
    repos" semantics.
    """

    phrase = _staleness_phrase(staleness_seconds)
    if graph_sha and phrase:
        short = graph_sha[:7]
        return f"Graph as-of SHA: {short} ({phrase})"
    if graph_sha:
        short = graph_sha[:7]
        return f"Graph as-of SHA: {short}"
    if phrase:
        return f"Graph freshness: oldest contributing repo {phrase}"
    return ""


# ---------------------------------------------------------------------------
# Symbol facts block (callers / importers / implementers)
# ---------------------------------------------------------------------------


# Default relation labels for caller-shaped blocks. Renderer-level
# constants kept here so callers (handler.py) don't recompute them.
RELATION_LABELS = {
    "callers": "Direct callers",
    "importers": "Importers",
    "implementers": "Implementers",
}


def _sort_sites_stable(sites: Iterable[CallSite]) -> List[CallSite]:
    """Stable order so the rendered block is reproducible across
    invocations and test runs. The reader doesn't promise an order
    (results may come page-first), so we sort here.
    """

    return sorted(
        sites,
        key=lambda s: (s.repo or "", s.file_path or "", int(s.line or 0)),
    )


def build_symbol_facts_block(
    *,
    relation: str,
    target_symbol_id: str,
    result: QueryResult,
    definition_site: Optional[CallSite] = None,
    workspace_label: str = "this workspace",
) -> str:
    """Render a ``QueryResult`` as the "Symbol facts" block.

    ``relation`` must be one of ``RELATION_LABELS`` (callers /
    importers / implementers) — controls the heading and the
    zero-result phrasing.

    Empty results are rendered explicitly so the model doesn't claim
    callers/importers/implementers exist when the graph says none do.
    Truncation is disclosed so the model doesn't claim to have an
    exhaustive list when it doesn't.
    """

    label = RELATION_LABELS.get(relation, relation.capitalize())

    sites = _sort_sites_stable(result.sites)
    count = len(sites)

    lines: List[str] = [SYMBOL_FACTS_HEADER, ""]
    lines.append(f"Question target: {target_symbol_id}")

    if definition_site is not None:
        lines.append(
            f"Defined at: {definition_site.file_path}:{definition_site.line}"
        )

    if count == 0:
        lines.append("")
        lines.append(
            f"{label} in {workspace_label}: 0"
        )
    else:
        lines.append("")
        truncation_suffix = " (truncated)" if result.truncated else ""
        lines.append(
            f"{label} ({count} site{'s' if count != 1 else ''} "
            f"in {workspace_label}{truncation_suffix}):"
        )
        for site in sites:
            anchor = f"{site.file_path}:{site.line}"
            tag = f"  ({site.repo})" if site.repo else ""
            lines.append(f"- {anchor}{tag}")

    suffix = _sha_suffix(result.graph_sha, result.staleness_seconds)
    if suffix:
        lines.append("")
        lines.append(suffix)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Definition-only block — used when the question is structural but
# resolution turned up either zero or multiple defines.
# ---------------------------------------------------------------------------


def build_symbol_definition_block(
    *,
    symbol_candidate: str,
    definitions: List[CallSite],
    workspace_label: str = "this workspace",
) -> str:
    """Render the result of a ``definitions(symbol_candidate)`` lookup.

    Two modes for the user's question's symbol resolution:

    - **Zero matches**: "No symbol matching ``<name>`` found" — the
      chat handler still appends this block so the model surfaces the
      negative result honestly rather than fabricating.
    - **Multiple matches**: each candidate listed with its definition
      site so the model can disambiguate in its answer.

    Single-match case is normally folded into ``build_symbol_facts_block``
    via the ``definition_site`` parameter; calling this with a
    one-element list is supported but uncommon.
    """

    lines: List[str] = [SYMBOL_FACTS_HEADER, ""]
    lines.append(f"Question target (resolved candidate): {symbol_candidate}")

    if not definitions:
        lines.append("")
        lines.append(
            f"No symbol matching `{symbol_candidate}` found in {workspace_label}."
        )
        return "\n".join(lines)

    if len(definitions) == 1:
        d = definitions[0]
        lines.append(f"Defined at: {d.file_path}:{d.line}")
        return "\n".join(lines)

    lines.append("")
    lines.append(
        f"Multiple definitions matched in {workspace_label} ({len(definitions)}):"
    )
    for d in _sort_sites_stable(definitions):
        anchor = f"{d.file_path}:{d.line}"
        tag = f"  ({d.repo})" if d.repo else ""
        lines.append(f"- {anchor}{tag}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Unavailable / degraded block
# ---------------------------------------------------------------------------


def build_graph_unavailable_block(*, target_symbol_id: Optional[str] = None) -> str:
    """Render the fallback block when the symbol graph is unavailable.

    Emitted when ``shared.symbol_graph_reader`` raises
    ``SymbolGraphUnavailable`` (chunk-04 retry exhaustion) so the
    answer transparently degrades to RAG-only with a disclaimer. The
    grounding instruction (above) tells the model to surface the
    unavailable state to the user.
    """

    lines: List[str] = [SYMBOL_FACTS_HEADER, ""]
    if target_symbol_id:
        lines.append(f"Question target: {target_symbol_id}")
    lines.append("")
    lines.append(
        "Structural retrieval is currently unavailable (graph store "
        "transient failure). Answer using documentation context only "
        "and tell the user that structural facts could not be "
        "verified for this turn."
    )
    return "\n".join(lines)


__all__ = (
    "GRAPH_GROUNDING_INSTRUCTION",
    "RELATION_LABELS",
    "SYMBOL_FACTS_HEADER",
    "build_graph_unavailable_block",
    "build_symbol_definition_block",
    "build_symbol_facts_block",
)
