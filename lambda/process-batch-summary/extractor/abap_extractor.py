"""
ABAP symbol/edge extractor — hybrid Tree-sitter + regex (ADR-020).

The 2026-05-14 spike showed:

  * The FrankRogalski tree-sitter-abap grammar (commit ``7dcb098``)
    parses OO ABAP cleanly — class definitions, method bodies, calls.
  * The same grammar errors on procedural ABAP (``FUNCTION-POOL``,
    ``START-OF-SELECTION``, event blocks).

So this extractor sniffs the source header and dispatches:

  1. If the file contains ``CLASS ... DEFINITION`` or
     ``CLASS ... IMPLEMENTATION`` -> tree-sitter backend.
  2. If the file contains procedural markers (``FUNCTION-POOL``,
     ``START-OF-SELECTION``, ``FORM``, ``REPORT``) -> regex backend.
  3. Default -> regex backend (safe fallback).

The tree-sitter backend is imported lazily so the regex path (and the
test suite) work even when the FrankRogalski wheel isn't available in
the build environment. If the grammar import fails at runtime, this
module logs a warning and falls back to the regex backend.

Reference: docs/adr/ADR-020-abap-parser-strategy.md.
"""

from __future__ import annotations

import logging
import re
from typing import Iterator, List, Optional

from .base import Fact, make_symbol_id, register_language

logger = logging.getLogger(__name__)


_OO_HEADER_RE = re.compile(
    r"\bCLASS\s+[A-Z_/][A-Z0-9_/]*\s+(DEFINITION|IMPLEMENTATION)",
    re.IGNORECASE,
)
_PROCEDURAL_RE = re.compile(
    r"\b(FUNCTION-POOL|START-OF-SELECTION|REPORT\s+|FORM\s+)",
    re.IGNORECASE,
)

_CLASS_DEF_RE = re.compile(
    r"^\s*CLASS\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s+(DEFINITION|IMPLEMENTATION)",
    re.IGNORECASE | re.MULTILINE,
)
_METHOD_DEF_RE = re.compile(
    r"^\s*METHOD\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*(?:\.|\s)",
    re.IGNORECASE | re.MULTILINE,
)
_FORM_RE = re.compile(
    r"^\s*FORM\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*(?:\.|\s)",
    re.IGNORECASE | re.MULTILINE,
)
_FUNCTION_RE = re.compile(
    r"^\s*FUNCTION\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*(?:\.|\s)",
    re.IGNORECASE | re.MULTILINE,
)
_INCLUDE_RE = re.compile(
    r"^\s*INCLUDE\s+(?P<name>[A-Z_/][A-Z0-9_/]*)\s*\.",
    re.IGNORECASE | re.MULTILINE,
)
_CALL_FORM_RE = re.compile(
    r"\bPERFORM\s+(?P<name>[A-Z_/][A-Z0-9_/]*)",
    re.IGNORECASE,
)
_CALL_METHOD_RE = re.compile(
    r"\bCALL\s+METHOD\s+(?P<name>[A-Z_/][A-Z0-9_/=>-]+)",
    re.IGNORECASE,
)
_CALL_FUNCTION_RE = re.compile(
    r"\bCALL\s+FUNCTION\s+'(?P<name>[A-Z_/][A-Z0-9_/]*)'",
    re.IGNORECASE,
)


def _module_path(file_path: str) -> str:
    cleaned = file_path.replace("\\", "/")
    if cleaned.startswith("./"):
        cleaned = cleaned[2:]
    return cleaned


def _line_for_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _looks_oo(text: str) -> bool:
    return bool(_OO_HEADER_RE.search(text))


def _looks_procedural(text: str) -> bool:
    return bool(_PROCEDURAL_RE.search(text))


def _extract_via_regex(file_path: str, source: bytes) -> Iterator[Fact]:
    """Pure regex backend — procedural ABAP and the safe fallback."""

    text = source.decode("utf-8", errors="replace")
    module = _module_path(file_path)
    module_symbol_id = make_symbol_id("abap", module, "<module>")

    # INCLUDE -> imports
    for m in _INCLUDE_RE.finditer(text):
        name = m.group("name").upper()
        yield Fact(
            kind="imports",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", name, "*"),
            file_path=file_path,
            line=_line_for_offset(text, m.start()),
            language="abap",
        )

    # Class spans (for namespace assignment to METHODs)
    class_spans: List[tuple[int, int, str]] = []
    matches = list(_CLASS_DEF_RE.finditer(text))
    for i, cm in enumerate(matches):
        # End the span at the next class header (or EOF).
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        class_spans.append((cm.start(), end, cm.group("name").upper()))
        cls_name = cm.group("name").upper()
        cls_id = make_symbol_id("abap", module, cls_name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=cls_id,
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )

    seen_methods: set[str] = set()
    for mm in _METHOD_DEF_RE.finditer(text):
        method_name = mm.group("name").upper()
        # Find enclosing class
        enclosing: Optional[str] = None
        for span_start, span_end, cls in class_spans:
            if span_start <= mm.start() <= span_end:
                enclosing = cls
                break
        if enclosing is None:
            continue
        key = f"{enclosing}::{method_name}"
        if key in seen_methods:
            continue
        seen_methods.add(key)
        method_id = make_symbol_id("abap", module, method_name, namespace=enclosing)
        cls_id = make_symbol_id("abap", module, enclosing)
        yield Fact(
            kind="defines",
            source_symbol_id=cls_id,
            target_symbol_id=method_id,
            file_path=file_path,
            line=_line_for_offset(text, mm.start("name")),
            language="abap",
        )

    # FORM and FUNCTION definitions (procedural)
    for fm in _FORM_RE.finditer(text):
        name = fm.group("name").upper()
        form_id = make_symbol_id("abap", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=form_id,
            file_path=file_path,
            line=_line_for_offset(text, fm.start("name")),
            language="abap",
        )

    for fnm in _FUNCTION_RE.finditer(text):
        name = fnm.group("name").upper()
        fn_id = make_symbol_id("abap", module, name)
        yield Fact(
            kind="defines",
            source_symbol_id=module_symbol_id,
            target_symbol_id=fn_id,
            file_path=file_path,
            line=_line_for_offset(text, fnm.start("name")),
            language="abap",
        )

    # Calls: PERFORM (FORM), CALL METHOD, CALL FUNCTION
    for cm in _CALL_FORM_RE.finditer(text):
        name = cm.group("name").upper()
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", module, name),
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )
    for cm in _CALL_METHOD_RE.finditer(text):
        raw = cm.group("name").upper()
        # Strip class qualifier (e.g. ``ZCL_X=>METHOD`` -> ``METHOD`` on ``ZCL_X``)
        if "=>" in raw:
            cls, sym = raw.split("=>", 1)
            target = make_symbol_id("abap", cls, sym)
        elif "->" in raw:
            _, sym = raw.split("->", 1)
            target = make_symbol_id("abap", module, sym)
        else:
            target = make_symbol_id("abap", module, raw)
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=target,
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )
    for cm in _CALL_FUNCTION_RE.finditer(text):
        name = cm.group("name").upper()
        yield Fact(
            kind="calls",
            source_symbol_id=module_symbol_id,
            target_symbol_id=make_symbol_id("abap", name, "*"),
            file_path=file_path,
            line=_line_for_offset(text, cm.start()),
            language="abap",
        )


def _extract_via_tree_sitter(file_path: str, source: bytes) -> Iterator[Fact]:
    """Tree-sitter backend (lazy import).

    The FrankRogalski grammar is not on PyPI and is only available via
    git + compilation. In environments where it isn't installed we fall
    back to the regex backend rather than fail the whole run. ADR-020
    documents the dependency contract.
    """

    try:
        import tree_sitter_abap  # type: ignore  # noqa: F401
        from tree_sitter import Language, Parser  # noqa: F401
    except Exception:
        logger.info(
            "tree-sitter-abap grammar not available; falling back to regex for %s",
            file_path,
        )
        yield from _extract_via_regex(file_path, source)
        return

    # When the grammar IS available, the OO ABAP edges this extractor
    # would emit are a strict superset of what the regex backend can
    # see today: namespace-correct method bodies and accurate call
    # arguments. v1 ships the regex result here too (matching what
    # ADR-020 documents as the OO-ABAP coverage baseline) so the wire
    # contract stays stable; the richer tree-sitter walk lands in a
    # follow-up chunk that wires the grammar at build time.
    yield from _extract_via_regex(file_path, source)


def extract(file_path: str, source: bytes) -> Iterator[Fact]:
    """Hybrid dispatcher — header-sniff for OO vs procedural."""

    text = source.decode("utf-8", errors="replace")
    if _looks_oo(text):
        yield from _extract_via_tree_sitter(file_path, source)
    else:
        yield from _extract_via_regex(file_path, source)


register_language("abap", extract)
