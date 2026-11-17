# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Java reference resolver (ADR-055).

Binds a Java file's cross-package ``calls`` / ``imports`` / ``extends`` /
``implements`` targets to the callee's DEFINITION symbol id, using the file's own
``import`` facts plus the repo definitions index. The language-agnostic framing
(defs index, remap application, never-guess) lives in ``extractor.resolution``;
this module supplies only the Java binding rules.

Java is the cleanest binding of any language: ``import a.b.C;`` names the exact
fully-qualified type, so a reference to ``C`` binds directly to the definition in
package ``a.b``.

Java fact shapes it binds against (from ``java_extractor.py``):
  * ``defines``: ``java:<package>::<Class>`` and
    ``java:<package>::<Class>::<method>`` (module slot = the ``package``).
  * ``imports``: ``import a.b.C;`` -> ``java:<a.b>::<C>`` (FQN, single type);
    wildcard ``import a.b.*;`` -> ``java:<a.b>::*`` (no single type -> raw).
  * ``calls``: ``java:<caller package>::<Name>`` -- the bare method/constructor
    name (the extractor captures the invocation name without the receiver).
  * ``extends`` / ``implements``: ``java:<BaseName>::*`` (base NAME in module slot).

Binding rules (v1):
  * Explicit single-type import wins: a reference whose name equals an imported
    simple type name binds to that type's definition (constructors, static calls,
    and extends/implements of an imported type).
  * Same-package definition: a reference to a type/method defined in the file's
    own package binds to it.
  * Unique repo-wide name fallback: a name with exactly one definition anywhere
    binds (covers cross-package method calls whose name is unique).

Never-guess (ADR-055 decision #4): a reference that does not bind to exactly one
known definition keeps its raw target. Over-resolution produces a false
"you broke this consumer" WARN, which is worse than under-resolution.

Not handled in v1 (left raw): wildcard-imported ambiguous names; overloaded
methods whose name is non-unique across ambiguous packages; static imports of a
member (the extractor does not distinguish them from type imports); fully-
qualified inline references (``a.b.C.foo()``) where the extractor captured only
``foo``.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

from .base import Fact, register_resolver
from .resolution import DefinitionsIndex, split_symbol_id


def _file_package(file_facts: List[Fact]) -> Optional[str]:
    """The package (module slot) of the file under resolution."""
    for f in file_facts:
        if f.kind == "defines":
            return split_symbol_id(f.target_symbol_id)[1]
    for f in file_facts:
        if f.kind == "calls":
            return split_symbol_id(f.source_symbol_id)[1]
    return None


def _imported_types(file_facts: List[Fact]) -> Dict[str, Tuple[str, str]]:
    """Map imported simple type name -> (package, type) from single-type imports.

    ``import a.b.C;`` -> {``C``: (``a.b``, ``C``)}. Wildcard imports (sym ``*``)
    are skipped -- they name no single type.
    """
    out: Dict[str, Tuple[str, str]] = {}
    for f in file_facts:
        if f.kind != "imports":
            continue
        _lang, pkg, _ns, sym = split_symbol_id(f.target_symbol_id)
        if sym and sym != "*":
            out[sym] = (pkg, sym)
    return out


def _bind_name(
    name: str,
    *,
    file_pkg: Optional[str],
    imported: Dict[str, Tuple[str, str]],
    defs: DefinitionsIndex,
) -> Optional[str]:
    """Resolve a bare type/method ``name`` to a definition id, or None."""
    # 1. Explicit single-type import of this name.
    qual = imported.get(name)
    if qual is not None:
        return defs.by_qualified.get(qual)  # None if the type is external -> raw
    # 2. Same-package definition (type or method in the file's own package).
    if file_pkg is not None:
        hit = defs.by_qualified.get((file_pkg, name))
        if hit:
            return hit
    # 3. Unique repo-wide definition of the name.
    by_name = defs.by_name.get(name)
    if by_name and len(by_name) == 1:
        only = next(iter(by_name))
        if split_symbol_id(only)[1] != file_pkg:
            return only
    return None


def resolve_java_file(file_facts: List[Fact], defs: DefinitionsIndex) -> Dict[str, str]:
    """Return ``{raw_target_symbol_id: resolved_definition_id}`` for one file."""
    file_pkg = _file_package(file_facts)
    imported = _imported_types(file_facts)
    remap: Dict[str, str] = {}

    for f in file_facts:
        if f.kind == "calls":
            _lang, _mod, _ns, callee = split_symbol_id(f.target_symbol_id)
            if not callee or "." in callee:
                continue
            bound = _bind_name(
                callee, file_pkg=file_pkg, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind == "imports":
            _lang, pkg, _ns, sym = split_symbol_id(f.target_symbol_id)
            if not sym or sym == "*":
                continue  # wildcard import: no single type to bind
            bound = defs.by_qualified.get((pkg, sym))
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind in ("extends", "implements"):
            _lang, base_name, _ns, _sym = split_symbol_id(f.target_symbol_id)
            if not base_name:
                continue
            bound = _bind_name(
                base_name, file_pkg=file_pkg, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

    return remap


register_resolver("java", resolve_java_file)
