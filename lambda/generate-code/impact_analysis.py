"""Pre-PR caller-impact analysis for generate-code (plan-gkg chunk 07).

This module is invoked on the **modify-path** only. Greenfield generation
(new files / new symbols with no existing callers) is intentionally a
no-op here: the symbol graph cannot have callers for code that doesn't
exist yet, and querying it would just round-trip empty results.

The flow, per plan-gkg/07-codegen-integration.md:

  1. The existing plan-generation path produces a list of files to be
     created/modified.
  2. ``classify_task_type`` decides greenfield vs modify based on the
     Jira/GitHub issue verbiage and the plan's file mix.
  3. On modify, ``extract_modify_targets`` parses the plan to identify
     which existing symbols are being changed.
  4. ``analyze_caller_impact`` queries the symbol graph (chunk-04 reader)
     for callers of each modified symbol.
  5. ``build_impact_prompt_section`` formats the result for inclusion
     in the model's prompt, so the next codegen pass updates caller
     sites alongside the primary change.

The graph is **best-effort**: if the reader raises
``SymbolGraphUnavailable``, if the workspace_id is missing, or if the
plan can't be parsed, the impact analysis is skipped and codegen
proceeds as before — the failure mode is "miss the early warning,"
not "block the PR." That matches the failure-modes table in chunk 07.

Test-isolation note: This module imports ``shared.symbol_graph_reader``
which itself uses ``boto3`` at module load time. The reader has its own
``_set_resource_for_tests`` injection; this module's tests reuse that
hook (see ``test_generate_code_impact.py``) so we do NOT need to mock
boto3 here. We also import the reader **lazily** inside the public
functions so import-time failures (missing layer, env mis-config) don't
break the entire generate-code module load.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Task-type classification
# ---------------------------------------------------------------------------


# Verbs the issue title/body tends to use for "modify existing code"
# work. Order matters only for documentation; matching is set-based.
_MODIFY_VERBS: Tuple[str, ...] = (
    "change",
    "update",
    "rename",
    "refactor",
    "fix",
    "modify",
    "remove",
    "delete",
    "deprecate",
    "migrate",
    "replace",
    "tweak",
    "adjust",
    "patch",
)

# Verbs that indicate greenfield work. "Add a new..." / "create a new..."
# patterns are highly specific; we additionally treat plain "add" /
# "create" / "implement" as greenfield-leaning so the heuristic stays
# closer to the way real issues are written.
_GREENFIELD_VERBS: Tuple[str, ...] = (
    "add",
    "create",
    "implement",
    "build",
    "introduce",
    "scaffold",
    "generate",
)


@dataclass(frozen=True)
class TaskClassification:
    """Result of ``classify_task_type``.

    ``task_type`` is one of ``"modify"`` or ``"greenfield"``.

    ``reason`` is a short human-readable string. We log this so a
    reviewer can audit why impact analysis fired (or didn't) without
    re-running the heuristic.

    Per plan-gkg/07: when ambiguous, default to ``"modify"`` because
    impact analysis is additive overhead, not destructive.
    """

    task_type: str
    reason: str


def _word_set(text: str) -> set:
    """Lowercase word tokens. Cheap; we don't need a real tokenizer."""

    if not text:
        return set()
    return set(re.findall(r"[a-z][a-z0-9_-]*", text.lower()))


def classify_task_type(
    *,
    issue_title: str,
    issue_description: Optional[str],
    files_to_create: Sequence[str] = (),
    files_to_modify: Sequence[str] = (),
) -> TaskClassification:
    """Decide whether the issue + plan is greenfield or modify.

    Detection (per plan-gkg/07 "Greenfield vs modify dispatch"):

    - Plan explicitly modifies existing files -> ``modify`` regardless
      of issue verbiage. This is the strongest signal.
    - Issue uses a modify-verb ("change", "update", "rename", ...) and
      the plan creates files but doesn't modify any -> ``modify``
      (the model is going to touch existing call sites even if it
      structures the work as "create new file"; impact analysis is
      cheap, run it).
    - Issue uses ONLY greenfield verbs ("add new", "create new") AND
      no file-modifications are planned -> ``greenfield``.
    - Ambiguous -> ``modify`` (additive overhead, follows the
      conservative default in chunk 07).

    Note: ``files_to_modify`` is currently empty for OutcomeOps because
    the plan schema doesn't separate create vs modify yet. The parameter
    is plumbed through so later schema changes can take advantage of it
    without changing this signature.
    """

    if files_to_modify:
        return TaskClassification(
            task_type="modify",
            reason=f"plan modifies {len(files_to_modify)} existing files",
        )

    words = _word_set(issue_title) | _word_set(issue_description or "")
    has_modify_verb = any(v in words for v in _MODIFY_VERBS)
    has_greenfield_verb = any(v in words for v in _GREENFIELD_VERBS)

    if has_modify_verb:
        return TaskClassification(
            task_type="modify",
            reason="issue language signals modification",
        )

    if has_greenfield_verb and not has_modify_verb:
        return TaskClassification(
            task_type="greenfield",
            reason="issue language signals greenfield only",
        )

    # Ambiguous: lean modify (impact analysis is additive, not destructive).
    return TaskClassification(
        task_type="modify",
        reason="ambiguous issue language; defaulting to modify",
    )


# ---------------------------------------------------------------------------
# Symbol extraction from plan output
# ---------------------------------------------------------------------------


# Match qualified symbol IDs in plan/issue text. Two recognized shapes:
#
#   - "module.path::name"   (Python-style, what the chunk-01 extractor emits)
#   - "@scope/pkg/subpath"  (TS-style subpath; relevant for importers())
#
# We also accept the explicit ``python:`` / ``typescript:`` /
# ``language:`` prefix the extractor uses, but it's optional in the
# plan-language pattern (the model isn't required to write the prefix).
_PYTHON_SYMBOL_RE = re.compile(
    r"(?P<lang>(?:python|py|typescript|ts|java|csharp|cs|go|ruby|rb):)?"
    r"(?P<module>[A-Za-z_][\w.]*)"
    r"::"
    r"(?P<name>[A-Za-z_]\w*)"
)

_TS_SUBPATH_RE = re.compile(
    r"(?P<lang>(?:typescript|ts):)?"
    r"(?P<pkg>@[a-z0-9][\w.-]*/[a-z0-9][\w.-]*(?:/[\w./-]+)*)"
)


@dataclass(frozen=True)
class ModifyTarget:
    """A symbol the plan proposes to modify.

    ``symbol_id`` is the canonical ID used by the symbol graph (always
    language-prefixed; we normalize on the way out of
    ``extract_modify_targets``).

    ``signature_change`` is the model's best signal: if the plan text
    around the symbol mentions "signature", "parameter", "argument", or
    "return type", we assume the change is signature-level (callers
    likely need updates). Otherwise body-only.

    ``raw_mentions`` is the count of times the symbol appeared in the
    plan; surfaced for logging/diagnostics only.
    """

    symbol_id: str
    signature_change: bool
    raw_mentions: int


def _normalize_symbol_id(
    *, lang: Optional[str], module: Optional[str], name: Optional[str], pkg: Optional[str]
) -> Optional[str]:
    """Build a canonical symbol_id from regex groups.

    The chunk-01 extractor emits ``python:lambda.shared.foo::bar``. We
    default to ``python:`` when no language prefix is present and a
    Python-shape symbol matched, because the OutcomeOps repo is
    Python-dominant; this matches the proof case in the plan
    (``report_repos_snapshot``). When a TS subpath matches, default to
    ``typescript:`` for the same reason.
    """

    if pkg:
        prefix = "typescript:" if not lang else lang
        if prefix == "ts:":
            prefix = "typescript:"
        return f"{prefix}{pkg}"

    if module and name:
        prefix = "python:" if not lang else lang
        if prefix == "py:":
            prefix = "python:"
        return f"{prefix}{module}::{name}"

    return None


def _detect_signature_change(plan_text: str, symbol_id: str) -> bool:
    """Heuristic: is the change to ``symbol_id`` a signature change?

    We look at the 200 characters of plan text around each occurrence of
    the symbol's short name (the part after ``::``) for keywords that
    suggest the signature is changing. False positives here are cheap
    (the model gets a slightly more conservative prompt); false
    negatives miss the "you must also update callers" prompt, which is
    the failure mode we're trying to avoid — so we lean toward marking
    as signature-change when in doubt.
    """

    # Pull the symbol's short name out so we can locate occurrences.
    short: str
    if "::" in symbol_id:
        short = symbol_id.split("::", 1)[1]
    elif "/" in symbol_id:
        short = symbol_id.rsplit("/", 1)[1]
    else:
        short = symbol_id

    if not short or not plan_text:
        return False

    plan_lower = plan_text.lower()
    short_lower = short.lower()

    # Signature-change keywords. "parameter" matches "parameters", etc.
    sig_keywords = (
        "signature",
        "parameter",
        "argument",
        "return type",
        "return value",
        "rename",
        "new param",
        "new argument",
        "breaking change",
        "api change",
    )

    idx = 0
    while True:
        found = plan_lower.find(short_lower, idx)
        if found < 0:
            return False
        # 200-char window around the mention.
        lo = max(0, found - 200)
        hi = min(len(plan_lower), found + len(short_lower) + 200)
        window = plan_lower[lo:hi]
        if any(kw in window for kw in sig_keywords):
            return True
        idx = found + len(short_lower)


def extract_modify_targets(
    plan_text: str,
    *,
    issue_title: str = "",
    issue_description: str = "",
) -> List[ModifyTarget]:
    """Parse plan text and return the symbols it proposes to modify.

    The plan is human-language Markdown with embedded symbol IDs (the
    backend's plan-generation prompt asks the model to write them
    in ``module.path::name`` form, and the extractor emits the same
    shape). We grep for both Python-style symbols (``foo.bar::baz``) and
    TS-style subpath imports (``@scope/pkg/subpath``) — covering the
    two failure cases the plan documents
    (``report_repos_snapshot`` and ``@hyatt-hpe/hpe-utils/http2``).

    Duplicate mentions are collapsed; ``raw_mentions`` carries the
    count for logging.

    The issue title and description are folded into the search corpus
    because real issues sometimes name the symbol once in the body and
    never in the plan output.
    """

    if not plan_text and not issue_title and not issue_description:
        return []

    corpus = "\n".join(p for p in (plan_text, issue_title, issue_description) if p)

    counts: dict[str, int] = {}
    for match in _PYTHON_SYMBOL_RE.finditer(corpus):
        sid = _normalize_symbol_id(
            lang=match.group("lang"),
            module=match.group("module"),
            name=match.group("name"),
            pkg=None,
        )
        if sid:
            counts[sid] = counts.get(sid, 0) + 1

    for match in _TS_SUBPATH_RE.finditer(corpus):
        sid = _normalize_symbol_id(
            lang=match.group("lang"),
            module=None,
            name=None,
            pkg=match.group("pkg"),
        )
        if sid:
            counts[sid] = counts.get(sid, 0) + 1

    targets: List[ModifyTarget] = []
    for sid, count in counts.items():
        targets.append(
            ModifyTarget(
                symbol_id=sid,
                signature_change=_detect_signature_change(corpus, sid),
                raw_mentions=count,
            )
        )

    # Stable ordering for deterministic prompts/tests.
    targets.sort(key=lambda t: t.symbol_id)
    return targets


# ---------------------------------------------------------------------------
# Graph query orchestration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SymbolImpact:
    """Aggregated caller-impact for a single modified symbol.

    ``callers`` is the list of CallSite (from the reader) describing
    each known invocation site. ``truncated`` mirrors the reader's
    QueryResult.truncated when the cap was hit. ``error`` carries the
    short error string when the graph was unavailable for this symbol;
    callers further up should degrade gracefully when ``error`` is set.
    """

    target: ModifyTarget
    callers: List[Any]  # list[CallSite] -- imported lazily to avoid hard dep
    truncated: bool
    error: Optional[str]


# Per plan-gkg/07 "Failure modes" — when a single symbol's caller list
# would exceed the prompt budget we truncate at this many entries and
# annotate the prompt with the truncation. 200 lines up with the chunk-04
# default for callers(); for importers() we go higher.
_CALLERS_LIMIT = 200
_IMPORTERS_LIMIT = 500


def _is_subpath_symbol(symbol_id: str) -> bool:
    """True when the symbol ID looks like an npm subpath rather than a
    function/method ID. Subpaths are queried via ``importers()``;
    function/method IDs via ``callers()``."""

    # Strip language prefix, then look for ``::`` (function-style) vs ``/``
    body = symbol_id.split(":", 1)[1] if ":" in symbol_id else symbol_id
    return "::" not in body and "/" in body


def analyze_caller_impact(
    *,
    workspace_id: str,
    targets: Iterable[ModifyTarget],
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> List[SymbolImpact]:
    """Query the symbol graph for each modify-target.

    The function is **resilient**: a ``SymbolGraphUnavailable`` (or any
    other reader exception) for one target produces a ``SymbolImpact``
    with ``error`` set and the analysis continues with the rest. The
    failure-modes table in plan-gkg/07 calls this out explicitly.

    Lazy-imports the reader so module load doesn't drag in
    ``boto3``/``moto`` until impact analysis is actually exercised.
    """

    if not workspace_id:
        # Per failure-modes table: graph unavailable -> empty result,
        # caller decides how to surface this. Returning [] lets the
        # codegen path proceed without an impact block.
        return []

    # Lazy import so tests that don't exercise impact analysis don't
    # pay the boto3 import cost (and don't collide with the
    # cross-suite sys.modules['boto3'] MagicMock — see
    # test_plan_generator.py module-load patch).
    from shared.symbol_graph_reader import (  # type: ignore
        SymbolGraphUnavailable,
        callers as q_callers,
        importers as q_importers,
    )

    results: List[SymbolImpact] = []
    for target in targets:
        try:
            if _is_subpath_symbol(target.symbol_id):
                qr = q_importers(
                    workspace_id,
                    target.symbol_id,
                    limit=_IMPORTERS_LIMIT,
                    symbol_graph_table_name=symbol_graph_table_name,
                    code_maps_table_name=code_maps_table_name,
                )
            else:
                qr = q_callers(
                    workspace_id,
                    target.symbol_id,
                    limit=_CALLERS_LIMIT,
                    symbol_graph_table_name=symbol_graph_table_name,
                    code_maps_table_name=code_maps_table_name,
                )
            results.append(
                SymbolImpact(
                    target=target,
                    callers=list(qr.sites),
                    truncated=bool(qr.truncated),
                    error=None,
                )
            )
        except SymbolGraphUnavailable as exc:
            logger.warning(
                "[impact-analysis] graph unavailable for %s: %s",
                target.symbol_id,
                exc,
            )
            results.append(
                SymbolImpact(
                    target=target,
                    callers=[],
                    truncated=False,
                    error="graph_unavailable",
                )
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning(
                "[impact-analysis] unexpected error querying %s: %s",
                target.symbol_id,
                exc,
            )
            results.append(
                SymbolImpact(
                    target=target,
                    callers=[],
                    truncated=False,
                    error=f"unexpected:{type(exc).__name__}",
                )
            )

    return results


# ---------------------------------------------------------------------------
# Prompt formatting
# ---------------------------------------------------------------------------


def build_impact_prompt_section(impacts: Sequence[SymbolImpact]) -> str:
    """Render an impact-analysis prompt fragment for the codegen model.

    Returns an empty string when there's nothing to say — the existing
    plan-generation prompt is not modified in that case (greenfield
    parity).

    The output format mirrors the example in plan-gkg/07
    "Impact-analysis prompt":

      Plan analysis: your proposed changes will modify the following symbols:

      1. `<symbol_id>`
         - signature change|body change
         - N callers will need invocation updates|depend on the current contract:
           - <file>:<line>
           - ...

    Truncated lists end with an explicit note so the model knows the
    impact is broader than the printed list (per the chunk-07
    failure-modes table).
    """

    real_impacts = [i for i in impacts if i.callers or i.error]
    if not real_impacts:
        return ""

    lines: List[str] = []
    lines.append(
        "Plan analysis: your proposed changes will modify the following symbols:"
    )
    lines.append("")

    for idx, impact in enumerate(real_impacts, start=1):
        lines.append(f"{idx}. `{impact.target.symbol_id}`")

        if impact.error == "graph_unavailable":
            lines.append(
                "   - symbol graph unavailable; impact analysis skipped for this symbol"
            )
            lines.append("")
            continue
        if impact.error:
            lines.append(
                f"   - impact analysis error: {impact.error}; treat as best-effort"
            )
            lines.append("")
            continue

        if impact.target.signature_change:
            lines.append("   - signature change planned")
            kind_blurb = "callers will need invocation updates"
        else:
            lines.append("   - body change, no signature change planned")
            kind_blurb = "callers depend on the current behavior"

        count = len(impact.callers)
        if count == 0:
            lines.append(
                "   - no callers found in this workspace; safe to proceed"
            )
            lines.append("")
            continue

        suffix = " (truncated; more exist)" if impact.truncated else ""
        lines.append(f"   - {count} {kind_blurb}{suffix}:")
        for site in impact.callers:
            # CallSite is a frozen dataclass with file_path/line attrs.
            lines.append(f"     - {site.file_path}:{site.line}")
        lines.append("")

    lines.append(
        "Rewrite the plan to include the necessary caller updates. If the "
        "breaking change is intentional and unavoidable, explicitly list the "
        "callers that need migration as part of the PR's task list — do not "
        "silently ship a breaking change."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public top-level helper
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ImpactAnalysisResult:
    """One-shot return type for the top-level helper.

    Consumers in ``plan_generator``/``step_executor`` only need three
    things: did we run, what's the prompt fragment, what should we log?
    This dataclass collapses the lower-level types into that shape so
    the integration site is one call.
    """

    task_type: str
    reason: str
    impacts: List[SymbolImpact]
    prompt_section: str
    skipped: bool


def run_impact_analysis(
    *,
    issue_title: str,
    issue_description: Optional[str],
    plan_text: str,
    workspace_id: Optional[str],
    files_to_create: Sequence[str] = (),
    files_to_modify: Sequence[str] = (),
    symbol_graph_table_name: Optional[str] = None,
    code_maps_table_name: Optional[str] = None,
) -> ImpactAnalysisResult:
    """End-to-end orchestrator: classify -> extract -> query -> format.

    Per plan-gkg/07, this is a no-op on the greenfield path. We still
    classify so the caller can log the decision (and so the
    new-public-symbol capture in the greenfield branch — out of scope
    for this chunk's prompt section — has a place to hook in later).

    Returns an ``ImpactAnalysisResult`` whose ``prompt_section`` is the
    empty string when nothing should be injected. Consumers concatenate
    the section to their existing prompt only when it's truthy, so
    greenfield prompts are unchanged (latency parity per chunk-07
    Definition of Done).
    """

    classification = classify_task_type(
        issue_title=issue_title,
        issue_description=issue_description,
        files_to_create=files_to_create,
        files_to_modify=files_to_modify,
    )

    if classification.task_type == "greenfield":
        logger.info(
            "[impact-analysis] greenfield path; skipping graph query (%s)",
            classification.reason,
        )
        return ImpactAnalysisResult(
            task_type="greenfield",
            reason=classification.reason,
            impacts=[],
            prompt_section="",
            skipped=True,
        )

    if not workspace_id:
        logger.info(
            "[impact-analysis] modify path detected but workspace_id "
            "unavailable; skipping graph query"
        )
        return ImpactAnalysisResult(
            task_type="modify",
            reason=classification.reason + "; no workspace_id available",
            impacts=[],
            prompt_section="",
            skipped=True,
        )

    targets = extract_modify_targets(
        plan_text,
        issue_title=issue_title,
        issue_description=issue_description or "",
    )
    if not targets:
        logger.info(
            "[impact-analysis] modify path but no resolvable symbols in plan; "
            "skipping graph query"
        )
        return ImpactAnalysisResult(
            task_type="modify",
            reason=classification.reason + "; no symbols resolved",
            impacts=[],
            prompt_section="",
            skipped=True,
        )

    impacts = analyze_caller_impact(
        workspace_id=workspace_id,
        targets=targets,
        symbol_graph_table_name=symbol_graph_table_name,
        code_maps_table_name=code_maps_table_name,
    )
    prompt_section = build_impact_prompt_section(impacts)
    logger.info(
        "[impact-analysis] modify path: %d symbols, %d total callers",
        len(impacts),
        sum(len(i.callers) for i in impacts),
    )
    return ImpactAnalysisResult(
        task_type="modify",
        reason=classification.reason,
        impacts=impacts,
        prompt_section=prompt_section,
        skipped=False,
    )


__all__ = (
    "TaskClassification",
    "ModifyTarget",
    "SymbolImpact",
    "ImpactAnalysisResult",
    "classify_task_type",
    "extract_modify_targets",
    "analyze_caller_impact",
    "build_impact_prompt_section",
    "run_impact_analysis",
)
