"""
Go reference resolver (ADR-055).

Binds a Go file's cross-PACKAGE ``calls`` targets to the callee's DEFINITION
symbol id, using the file's own ``imports`` facts plus the repo definitions index.
The language-agnostic framing (defs index, remap application, never-guess) lives
in ``extractor.resolution``; this module supplies only the Go binding rules.

Go fact shapes it binds against (from ``go_extractor.py``):
  * ``defines``: ``go:<module>::<name>`` (func) / ``go:<module>::<Receiver>::<name>``
    (method). ``<module>`` is the file's DIRECTORY path (the Go package's location).
  * ``imports``: ``go:<import_path>::*`` -- the import string (e.g.
    ``example.com/proj/binding``); the imported name is always ``*``.
  * ``calls``: ``go:<caller module>::<callee>`` where ``callee`` is a bare
    identifier (same-package call) or ``pkg.Func`` (cross-package selector).

Why this resolver only rewrites selector calls:
  * A SAME-package call is already correct -- the extractor emits the call target
    in the caller's module, which equals the callee function's definition module
    (same directory), so ``raw target == definition id`` with no rewrite needed.
  * A CROSS-package call is written ``pkg.Func`` -- emitted in the caller's module
    namespace, never bound to the callee package. That is the gap.

Binding rule (v1), ADR-055 per-language decision:
  * For a selector call ``pkg.Name``: require ``pkg`` to be the last path segment
    of one of this file's imported packages (so it is a package, not a receiver
    variable or a standard-library symbol the repo does not define). Then bind to
    the definition ``Name`` in the UNIQUE repo package whose directory basename is
    ``pkg``. Zero or multiple candidate packages/definitions -> left raw.

Never-guess (ADR-055 decision #4): over-resolution yields a false
"you broke this consumer" WARN, worse than under-resolution.

Not handled in v1 (left raw): receiver-method calls (``c.JSON()`` -- the operand
is a value whose type needs inference, and it is not an imported package);
``imports`` facts (a package import names no single definition -- importers() stays
package-granular); nested selectors (``a.b.C``); aliased/dot imports (the extractor
collapses the alias, so the selector operand cannot be matched to the import).
"""

from __future__ import annotations

from typing import Dict, List, Optional, Set

from .base import Fact, register_resolver
from .resolution import DefinitionsIndex, split_symbol_id


def _basename(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def _imported_pkg_basenames(file_facts: List[Fact]) -> Set[str]:
    """Last path segments of this file's imported packages.

    ``import "example.com/proj/binding"`` -> ``{"binding"}``. These are the
    selector operands that denote a package (vs a receiver variable).
    """
    out: Set[str] = set()
    for f in file_facts:
        if f.kind != "imports":
            continue
        _lang, path, _ns, _sym = split_symbol_id(f.target_symbol_id)
        if path:
            out.add(_basename(path))
    return out


def resolve_go_file(file_facts: List[Fact], defs: DefinitionsIndex) -> Dict[str, str]:
    """Return ``{raw_target_symbol_id: resolved_definition_id}`` for one file."""
    imported_basenames = _imported_pkg_basenames(file_facts)
    remap: Dict[str, str] = {}

    for f in file_facts:
        if f.kind != "calls":
            continue
        _lang, _mod, _ns, callee = split_symbol_id(f.target_symbol_id)
        if not callee or "." not in callee:
            continue  # bare call: same-package (already correct) or builtin -> leave
        pkg, _, name = callee.partition(".")
        if not pkg or not name or "." in name:
            continue  # nested selector (a.b.c) -> out of scope
        if pkg not in imported_basenames:
            continue  # receiver variable / non-imported package -> never guess

        # Unique repo package whose directory basename is `pkg` and that defines
        # `name`.
        candidates: Set[str] = set()
        for module in defs.modules:
            if _basename(module) == pkg:
                hit = defs.by_qualified.get((module, name))
                if hit:
                    candidates.add(hit)
        if len(candidates) == 1:
            bound = next(iter(candidates))
            if bound != f.target_symbol_id:
                remap[f.target_symbol_id] = bound

    return remap


register_resolver("go", resolve_go_file)
