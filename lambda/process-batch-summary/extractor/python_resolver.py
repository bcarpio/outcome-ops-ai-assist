# Copyright 2025-2026 OutcomeOps
# SPDX-License-Identifier: Apache-2.0
"""
Python reference resolver (ADR-055, first language plugin).

Binds a Python file's cross-module ``calls`` / ``imports`` / ``extends`` targets
to the callee's DEFINITION symbol id, using the file's own ``imports`` facts plus
the repo definitions index. The language-agnostic framing (defs index, remap
application, never-guess) lives in ``extractor.resolution``; this module supplies
only the Python binding rules.

Binding rules (v1):
  * ``from pkg.mod import name`` -> ``name`` resolves to the definition of
    ``name`` in the file whose module suffix-matches ``pkg.mod``.
  * A bare reference with no import but a UNIQUE repo-wide definition of that
    name resolves to it (never-guess: ambiguous or zero -> left raw).
  * ``imports`` facts are themselves linked to the imported definition so
    ``importers(definition)`` works.
Not handled in v1 (left raw): aliased imports (``import x as y`` / ``from m
import n as a`` — the extractor records the real name, losing the alias),
dotted-module calls (``import mod; mod.f()``), ``from m import *``, relative
imports. These degrade to the current unresolved behavior, never a wrong link.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from .base import Fact, register_resolver
from .resolution import DefinitionsIndex, resolve_module_suffix, split_symbol_id


def _file_module(file_facts: List[Fact]) -> Optional[str]:
    """The module string of the file under resolution (from its own defines/calls)."""
    for f in file_facts:
        if f.kind == "defines":
            return split_symbol_id(f.target_symbol_id)[1]
    for f in file_facts:
        if f.kind == "calls":
            return split_symbol_id(f.source_symbol_id)[1]
    return None


def _imported_names(file_facts: List[Fact]) -> Dict[str, str]:
    """Map ``name -> module_text`` from this file's ``from mod import name`` facts."""
    out: Dict[str, str] = {}
    for f in file_facts:
        if f.kind != "imports":
            continue
        _lang, module_text, _ns, name = split_symbol_id(f.target_symbol_id)
        if name and name != "*":
            out[name] = module_text
    return out


def _bind_name(
    name: str,
    *,
    file_module: Optional[str],
    imported: Dict[str, str],
    defs: DefinitionsIndex,
) -> Optional[str]:
    """Resolve a bare symbol ``name`` to a definition id, or None (never-guess)."""
    # 1. Explicit import wins.
    module_text = imported.get(name)
    if module_text is not None:
        resolved_mod = resolve_module_suffix(module_text, defs.modules)
        if resolved_mod is not None:
            return defs.by_qualified.get((resolved_mod, name))
        return None
    # 2. Unique repo-wide definition of the name (and not the file's own local
    #    symbol, which is already resolved). Ambiguous -> None.
    candidates = defs.by_name.get(name)
    if candidates and len(candidates) == 1:
        only = next(iter(candidates))
        if split_symbol_id(only)[1] != file_module:
            return only
    return None


def resolve_python_file(file_facts: List[Fact], defs: DefinitionsIndex) -> Dict[str, str]:
    """Return ``{raw_target_symbol_id: resolved_definition_id}`` for one file."""
    file_module = _file_module(file_facts)
    imported = _imported_names(file_facts)
    remap: Dict[str, str] = {}

    for f in file_facts:
        if f.kind == "calls":
            _lang, _mod, _ns, callee = split_symbol_id(f.target_symbol_id)
            if not callee or "." in callee:
                continue  # dotted-module call: not handled in v1
            bound = _bind_name(
                callee, file_module=file_module, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind == "imports":
            _lang, module_text, _ns, name = split_symbol_id(f.target_symbol_id)
            if not name or name == "*":
                continue  # module-level import: no single definition to bind
            resolved_mod = resolve_module_suffix(module_text, defs.modules)
            if resolved_mod is not None:
                bound = defs.by_qualified.get((resolved_mod, name))
                if bound and bound != f.target_symbol_id:
                    remap[f.target_symbol_id] = bound

        elif f.kind in ("extends", "implements"):
            # _emit_class_extends encodes the base class NAME in the module slot
            # and "*" as the symbol (python:Base::*).
            _lang, base_name, _ns, _sym = split_symbol_id(f.target_symbol_id)
            if not base_name:
                continue
            bound = _bind_name(
                base_name, file_module=file_module, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

    return remap


register_resolver("python", resolve_python_file)
