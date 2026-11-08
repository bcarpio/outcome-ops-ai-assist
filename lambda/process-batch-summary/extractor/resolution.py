"""
Language-agnostic reference resolution (ADR-055).

The per-language extractors emit `calls`/`imports`/`extends` facts whose
``target_symbol_id`` is the reference *as written in the caller's namespace* —
never bound to the callee's definition. That makes
``symbol_graph_reader.callers(definition_id)`` / ``importers(definition_id)``
return nothing across files, which is what ADR-054 Plane 1 needs.

This module is the LANGUAGE-AGNOSTIC half of ADR-055: it builds a repo
definitions index from the ``defines`` facts and, for each file, asks the
registered per-language *resolver* (``base.register_resolver``) for a remap of
that file's reference targets, then rewrites them to the definition id. The
language-specific binding rules (how imports/packages map to files) live in the
per-language resolver; everything here is shared.

Scope v1 (ADR-055): WITHIN a repo. Never guess — a reference that does not bind
unambiguously to a known definition keeps its raw target.
"""

from __future__ import annotations

import dataclasses
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .base import Fact, get_resolver

# Reference fact kinds whose target may be rewritten to a definition id. A
# ``defines`` fact is itself the definition, so it is never remapped.
_REFERENCE_KINDS = {"calls", "imports", "extends", "implements", "references"}


def split_symbol_id(symbol_id: str) -> Tuple[str, str, Optional[str], str]:
    """Split a canonical symbol id into (language, module, namespace, name).

    Mirrors ``base.make_symbol_id``:
        ``<lang>:<module>::<name>``              -> namespace None
        ``<lang>:<module>::<namespace>::<name>`` -> namespace set
    Returns ``("", symbol_id, None, symbol_id)`` for an unparseable id so the
    caller can simply skip it rather than raise.
    """

    if ":" not in symbol_id:
        return ("", symbol_id, None, symbol_id)
    lang, rest = symbol_id.split(":", 1)
    parts = rest.split("::")
    if len(parts) == 2:
        return (lang, parts[0], None, parts[1])
    if len(parts) >= 3:
        # module :: namespace :: name  (extra "::" segments fold into namespace)
        return (lang, parts[0], "::".join(parts[1:-1]), parts[-1])
    # No "::" at all — not a definable symbol id we can resolve against.
    return (lang, parts[0], None, parts[0])


@dataclasses.dataclass(frozen=True)
class DefinitionsIndex:
    """Repo-wide index of definition symbol ids, built from ``defines`` facts.

    - ``by_qualified``: (module, bare_name) -> definition_id. Unique per
      (module, name); methods in different classes share (module, name) only if
      same class name, so this stays a best-effort fast path and ``by_name``
      carries the disambiguation.
    - ``by_name``: bare_name -> {definition_id}. Used for the unique-name
      fallback (resolve only when exactly one definition repo-wide bears the
      name — ADR-055 never-guess).
    - ``modules``: every defining module string, for import-root suffix matching.
    """

    by_qualified: Dict[Tuple[str, str], str] = dataclasses.field(default_factory=dict)
    by_name: Dict[str, Set[str]] = dataclasses.field(default_factory=dict)
    modules: Set[str] = dataclasses.field(default_factory=set)


def build_definitions_index(facts: Iterable[Fact]) -> DefinitionsIndex:
    """Build a :class:`DefinitionsIndex` from the ``defines`` facts in ``facts``."""

    idx = DefinitionsIndex()
    for fact in facts:
        if fact.kind != "defines":
            continue
        _lang, module, _ns, name = split_symbol_id(fact.target_symbol_id)
        if not module or not name:
            continue
        idx.modules.add(module)
        idx.by_qualified.setdefault((module, name), fact.target_symbol_id)
        idx.by_name.setdefault(name, set()).add(fact.target_symbol_id)
    return idx


def resolve_module_suffix(module_text: str, modules: Set[str]) -> Optional[str]:
    """Map an import module *as written* (e.g. ``shared.pr_metadata``) to a
    repo definition module (e.g. ``lambda.shared.pr_metadata``) by UNIQUE suffix
    match. Zero or multiple matches -> None (never guess)."""

    if module_text in modules:
        return module_text
    needle = "." + module_text
    hits = [m for m in modules if m.endswith(needle)]
    return hits[0] if len(hits) == 1 else None


def resolve_repo_facts(
    facts: Iterable[Fact],
    extra_definitions: Optional[Iterable[Fact]] = None,
) -> List[Fact]:
    """Rewrite reference targets to definition ids across one repo's facts.

    Pure function: takes the full fact stream for a repo (every file), returns a
    new list with each resolvable ``calls``/``imports``/``extends`` target bound
    to its definition id. ``defines`` facts pass through unchanged. Files whose
    language has no registered resolver pass through unchanged (no regression).

    ``extra_definitions`` (ADR-056) widens the definitions index with the
    ``defines`` of OTHER repos in the same workspace, so a cross-repo reference
    can bind to a producer repo's definition. They are **lookup-only**: they are
    never grouped by file, resolved, rewritten, or emitted -- only ``facts`` (this
    repo's own stream) is resolved and returned. When ``extra_definitions`` is
    None/empty, behavior is identical to the within-repo (ADR-055) pass, and
    never-guess still holds -- a name defined in more than one repo is ambiguous
    workspace-wide (``by_name`` length > 1) and the unique-name fallback declines.
    """

    materialized: List[Fact] = list(facts)
    if extra_definitions:
        sibling_defines = [f for f in extra_definitions if f.kind == "defines"]
        defs = build_definitions_index(materialized + sibling_defines)
    else:
        defs = build_definitions_index(materialized)

    # Group every file's facts so a resolver sees the file's imports alongside
    # its references.
    by_file: Dict[str, List[Fact]] = {}
    for fact in materialized:
        by_file.setdefault(fact.file_path, []).append(fact)

    # file_path -> {raw_target_symbol_id: resolved_target_symbol_id}
    remap_by_file: Dict[str, Dict[str, str]] = {}
    for file_path, file_facts in by_file.items():
        language = file_facts[0].language if file_facts else ""
        resolver = get_resolver(language)
        if resolver is None:
            continue
        try:
            remap = resolver(file_facts, defs)
        except Exception:  # pragma: no cover - a resolver bug must not drop facts
            remap = {}
        if remap:
            remap_by_file[file_path] = remap

    if not remap_by_file:
        return materialized

    out: List[Fact] = []
    for fact in materialized:
        remap = remap_by_file.get(fact.file_path)
        if (
            remap
            and fact.kind in _REFERENCE_KINDS
            and fact.target_symbol_id in remap
        ):
            new_target = remap[fact.target_symbol_id]
            if new_target and new_target != fact.target_symbol_id:
                out.append(dataclasses.replace(fact, target_symbol_id=new_target))
                continue
        out.append(fact)
    return out
