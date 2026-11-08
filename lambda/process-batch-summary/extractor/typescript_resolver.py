"""
TypeScript reference resolver (ADR-055).

Binds a TS/TSX file's cross-module ``calls`` / ``imports`` / ``extends`` /
``implements`` targets to the callee's DEFINITION symbol id, using the file's own
``import`` facts plus the repo definitions index. The language-agnostic framing
(defs index, remap application, never-guess) lives in ``extractor.resolution``;
this module supplies only the TypeScript binding rules.

TS fact shapes it binds against (from ``typescript_extractor.py``):
  * ``defines``: ``typescript:<module>::<name>`` and
    ``typescript:<module>::<Class>::<method>``. ``<module>`` is the file path
    with its extension stripped (``_module_path``), e.g. ``src/router/trie``.
  * ``imports``: one fact per imported name --
    ``typescript:<specifier>::<name>`` where ``<specifier>`` is the raw import
    string (``./trie``, ``../utils/url``, ``@acme/pkg``), and ``<name>`` is the
    imported binding (``*`` for ``import * as ns`` / side-effect imports).
  * ``calls``: ``typescript:<caller module>::<callee>`` -- bare identifier, or
    ``obj.method`` for a member call (``this.method`` collapses to ``method``).
  * ``extends`` / ``implements``: ``typescript:<BaseName>::*``.

Binding rules (v1):
  * A named import of a relative module resolves the specifier against the
    importing file's directory, then binds the imported name to the definition
    in that module. This is the clean, common case.
  * Unique repo-wide name fallback (exactly one definition of the name anywhere).

Never-guess (ADR-055 decision #4): a reference that does not bind to exactly one
known definition keeps its raw target. Over-resolution produces a false
"you broke this consumer" WARN, which is worse than under-resolution.

Not handled in v1 (left raw): namespace imports (``import * as ns``) and the
``ns.member`` calls they gate; default imports whose local alias differs from the
exported name; bare-package / path-aliased specifiers (``@acme/pkg``, ``@/lib``)
which are external or need tsconfig path resolution; re-export barrels
(``export * from``); dynamic ``import()``.
"""

from __future__ import annotations

import posixpath
from typing import Dict, List, Optional

from .base import Fact, register_resolver
from .resolution import DefinitionsIndex, split_symbol_id


def _file_module(file_facts: List[Fact]) -> Optional[str]:
    """The module string (path without extension) of the file under resolution."""
    for f in file_facts:
        if f.kind == "defines":
            return split_symbol_id(f.target_symbol_id)[1]
    for f in file_facts:
        if f.kind == "calls":
            return split_symbol_id(f.source_symbol_id)[1]
    return None


def _imported_names(file_facts: List[Fact]) -> Dict[str, str]:
    """Map imported ``name -> specifier`` from this file's named-import facts.

    Skips ``*`` (namespace / side-effect imports -- no single binding).
    """
    out: Dict[str, str] = {}
    for f in file_facts:
        if f.kind != "imports":
            continue
        _lang, specifier, _ns, name = split_symbol_id(f.target_symbol_id)
        if name and name != "*":
            out[name] = specifier
    return out


def _resolve_specifier(
    specifier: str, file_module: Optional[str], modules
) -> Optional[str]:
    """Resolve a relative import specifier to a repo definition module.

    ``./trie`` from ``src/router/index`` -> ``src/router/trie``. Only relative
    specifiers are resolved; bare / path-aliased specifiers return None (external
    or needs tsconfig path mapping, out of v1 scope).
    """
    if not specifier.startswith(".") or file_module is None:
        return None
    base_dir = posixpath.dirname(file_module)
    target = posixpath.normpath(posixpath.join(base_dir, specifier))
    if target in modules:
        return target
    # Tolerate a leading-path mismatch (e.g. abs vs repo-relative roots): accept a
    # unique module ending in the resolved tail.
    cands = [m for m in modules if m == target or m.endswith("/" + target)]
    return cands[0] if len(cands) == 1 else None


def _bind_name(
    name: str,
    *,
    file_module: Optional[str],
    imported: Dict[str, str],
    defs: DefinitionsIndex,
) -> Optional[str]:
    """Resolve a bare symbol ``name`` to a definition id, or None (never-guess)."""
    # 1. Explicit named import of a relative module.
    specifier = imported.get(name)
    if specifier is not None:
        resolved_mod = _resolve_specifier(specifier, file_module, defs.modules)
        if resolved_mod is not None:
            return defs.by_qualified.get((resolved_mod, name))
        return None  # imported from an external / unresolved module -> raw
    # 2. Unique repo-wide definition of the name (not the file's own symbol).
    by_name = defs.by_name.get(name)
    if by_name and len(by_name) == 1:
        only = next(iter(by_name))
        if split_symbol_id(only)[1] != file_module:
            return only
    return None


def resolve_typescript_file(
    file_facts: List[Fact], defs: DefinitionsIndex
) -> Dict[str, str]:
    """Return ``{raw_target_symbol_id: resolved_definition_id}`` for one file."""
    file_module = _file_module(file_facts)
    imported = _imported_names(file_facts)
    remap: Dict[str, str] = {}

    for f in file_facts:
        if f.kind == "calls":
            _lang, _mod, _ns, callee = split_symbol_id(f.target_symbol_id)
            if not callee or "." in callee:
                continue  # member call (obj.method): receiver type unknown -> raw
            bound = _bind_name(
                callee, file_module=file_module, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

        elif f.kind == "imports":
            _lang, specifier, _ns, name = split_symbol_id(f.target_symbol_id)
            if not name or name == "*":
                continue
            resolved_mod = _resolve_specifier(specifier, file_module, defs.modules)
            if resolved_mod is not None:
                bound = defs.by_qualified.get((resolved_mod, name))
                if bound and bound != f.target_symbol_id:
                    remap[f.target_symbol_id] = bound

        elif f.kind in ("extends", "implements"):
            _lang, base_name, _ns, _sym = split_symbol_id(f.target_symbol_id)
            if not base_name:
                continue
            bound = _bind_name(
                base_name, file_module=file_module, imported=imported, defs=defs
            )
            if bound and bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

    return remap


register_resolver("typescript", resolve_typescript_file)
