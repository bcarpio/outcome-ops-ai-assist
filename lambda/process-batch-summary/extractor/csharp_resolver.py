# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
C# reference resolver (ADR-055).

Binds a C# file's cross-namespace ``calls`` / ``imports`` (``using``) /
``extends`` / ``implements`` targets to the callee's DEFINITION symbol id, using
the file's own ``using`` facts plus the repo definitions index. The
language-agnostic framing (defs index, remap application, never-guess) lives in
``extractor.resolution``; this module supplies only the C# binding rules.

C# fact shapes it binds against (from ``csharp_extractor.py``):
  * ``defines``: ``csharp:<namespace>::<Type>`` and
    ``csharp:<namespace>::<Type>::<method>`` (module slot = the file's
    ``namespace`` declaration).
  * ``imports`` (one per ``using`` directive): ``csharp:<pkg>::<sym>`` where the
    extractor splits ``using A.B.C;`` on the last dot -> pkg=``A.B`` sym=``C``,
    and ``using A;`` -> pkg=``A`` sym=``*``. A plain namespace ``using`` therefore
    has no single definition to bind (sym is a namespace segment, not a type) and
    stays raw; a ``using static A.B.C;`` names the type ``C`` in namespace ``A.B``
    and DOES bind.
  * ``calls``: ``csharp:<caller namespace>::<Name>`` -- the bare method name (the
    extractor's regex captures ``Name(`` / ``obj.Name(`` without the receiver).
  * ``extends`` / ``implements``: ``csharp:<BaseName>::*`` (base NAME in the
    module slot).

Binding rules (v1):
  * A bare call / base name binds to a definition of that name in the file's own
    namespace or in a ``using``-imported namespace, when that is UNIQUE; else to
    the unique repo-wide definition of the name; else left raw.
  * A ``using static A.B.C;`` binds to the definition ``(A.B, C)`` when present.

Never-guess (ADR-055 decision #4): a reference that does not bind to exactly one
known definition keeps its raw target. Over-resolution produces a false
"you broke this consumer" WARN, which is worse than under-resolution.

Not handled in v1 (left raw): overloaded methods whose name is non-unique across
ambiguous namespaces; receiver-typed calls where the type is only known through
inference; partial classes whose members split across files when the name is
ambiguous; plain namespace ``using`` directives (no single definition).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from .base import Fact, register_resolver
from .resolution import DefinitionsIndex, split_symbol_id


def _file_namespace(file_facts: List[Fact]) -> Optional[str]:
    """The namespace (module slot) of the file under resolution."""
    for f in file_facts:
        if f.kind == "defines":
            return split_symbol_id(f.target_symbol_id)[1]
    for f in file_facts:
        if f.kind == "calls":
            return split_symbol_id(f.source_symbol_id)[1]
    return None


def _using_namespaces(file_facts: List[Fact]) -> List[str]:
    """Reconstruct the full namespaces brought in by this file's ``using`` facts.

    ``using A.B.C;`` -> extractor emits pkg=``A.B`` sym=``C`` -> namespace
    ``A.B.C`` (and also ``A.B`` as the parent). ``using A;`` -> pkg=``A`` sym=``*``
    -> namespace ``A``.
    """
    out: List[str] = []
    for f in file_facts:
        if f.kind != "imports":
            continue
        _lang, pkg, _ns, sym = split_symbol_id(f.target_symbol_id)
        if not pkg:
            continue
        if sym and sym != "*":
            out.append(f"{pkg}.{sym}")
        out.append(pkg)
    return out


def _bind_name(
    name: str,
    *,
    file_ns: Optional[str],
    using_ns: List[str],
    defs: DefinitionsIndex,
) -> Optional[str]:
    """Resolve a bare type/method ``name`` to a definition id, or None."""
    # 1. Unique definition across the file's own namespace + imported namespaces.
    candidates: Set[str] = set()
    for ns in ([file_ns] if file_ns else []) + using_ns:
        hit = defs.by_qualified.get((ns, name))
        if hit:
            candidates.add(hit)
    if len(candidates) == 1:
        return next(iter(candidates))
    if len(candidates) > 1:
        return None  # ambiguous across imported namespaces -> never guess
    # 2. Unique repo-wide definition of the name (not the file's own symbol).
    by_name = defs.by_name.get(name)
    if by_name and len(by_name) == 1:
        only = next(iter(by_name))
        if split_symbol_id(only)[1] != file_ns:
            return only
    return None


def resolve_csharp_file(
    file_facts: List[Fact], defs: DefinitionsIndex
) -> Dict[str, str]:
    """Return ``{raw_target_symbol_id: resolved_definition_id}`` for one file."""
    file_ns = _file_namespace(file_facts)
    using_ns = _using_namespaces(file_facts)
    remap: Dict[str, str] = {}

    for f in file_facts:
        if f.kind == "calls":
            _lang, _mod, _ns, callee = split_symbol_id(f.target_symbol_id)
            if not callee or "." in callee:
                continue
            bound = _bind_name(
                callee, file_ns=file_ns, using_ns=using_ns, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind == "imports":
            # Only a `using static A.B.C;` names a bindable type (A.B, C).
            # A plain namespace using has sym == a namespace segment with no
            # matching definition -> by_qualified miss -> stays raw.
            _lang, pkg, _ns, sym = split_symbol_id(f.target_symbol_id)
            if not sym or sym == "*":
                continue
            bound = defs.by_qualified.get((pkg, sym))
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind in ("extends", "implements"):
            _lang, base_name, _ns, _sym = split_symbol_id(f.target_symbol_id)
            if not base_name:
                continue
            bound = _bind_name(
                base_name, file_ns=file_ns, using_ns=using_ns, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

    return remap


register_resolver("csharp", resolve_csharp_file)
